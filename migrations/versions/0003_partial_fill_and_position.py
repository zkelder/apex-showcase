"""Persist one strict partial fill, cancellation, accounting, and open position.

Revision ID: 0003_partial_fill_and_position
Revises: 0002_durable_execution_events
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import DECIMAL_TYPE, immutable_trigger_ddl

revision: str = "0003_partial_fill_and_position"
down_revision: str | None = "0002_durable_execution_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ORDER_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_order_execution_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    transition_event_type text;
    transition_occurred_at timestamptz;
    transition_fill_quantity numeric(38,18);
    transition_fill_price numeric(38,18);
    authorization_time timestamptz;
    loss_per_unit numeric(38,18);
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'durable order cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
            'filled_quantity', 'reserved_maximum_loss', 'state',
            'last_execution_sequence', 'row_version', 'canonical_payload_hash'
        ])
       <>
       (to_jsonb(NEW) - ARRAY[
            'filled_quantity', 'reserved_maximum_loss', 'state',
            'last_execution_sequence', 'row_version', 'canonical_payload_hash'
        ]) THEN
        RAISE EXCEPTION 'immutable order terms cannot change';
    END IF;
    IF NOT (
        (OLD.state = 'READY'
         AND NEW.state = 'OPEN'
         AND OLD.last_execution_sequence = 0
         AND NEW.last_execution_sequence = 1
         AND NEW.row_version = OLD.row_version + 1
         AND NEW.filled_quantity = 0
         AND NEW.reserved_maximum_loss = OLD.reserved_maximum_loss)
        OR
        (OLD.state = 'OPEN'
         AND NEW.state = 'EXPIRED'
         AND OLD.last_execution_sequence = 1
         AND NEW.last_execution_sequence = 2
         AND NEW.row_version = OLD.row_version + 1
         AND OLD.filled_quantity = 0
         AND NEW.filled_quantity = 0
         AND OLD.reserved_maximum_loss > 0
         AND NEW.reserved_maximum_loss = 0)
        OR
        (OLD.state = 'OPEN'
         AND NEW.state = 'PARTIALLY_FILLED'
         AND OLD.last_execution_sequence = 1
         AND NEW.last_execution_sequence = 2
         AND NEW.row_version = OLD.row_version + 1
         AND OLD.filled_quantity = 0
         AND NEW.filled_quantity > 0
         AND NEW.filled_quantity < NEW.quantity
         AND OLD.reserved_maximum_loss > NEW.reserved_maximum_loss
         AND NEW.reserved_maximum_loss > 0)
        OR
        (OLD.state = 'PARTIALLY_FILLED'
         AND NEW.state = 'CANCELED'
         AND OLD.last_execution_sequence = 2
         AND NEW.last_execution_sequence = 3
         AND NEW.row_version = OLD.row_version + 1
         AND NEW.filled_quantity = OLD.filled_quantity
         AND OLD.reserved_maximum_loss > 0
         AND NEW.reserved_maximum_loss = 0)
    ) THEN
        RAISE EXCEPTION 'invalid durable order transition';
    END IF;
    SELECT event.event_type,
           event.occurred_at,
           event.fill_quantity,
           event.fill_price,
           decision.authorization_at,
           proposal.maximum_loss_per_unit
      INTO transition_event_type,
           transition_occurred_at,
           transition_fill_quantity,
           transition_fill_price,
           authorization_time,
           loss_per_unit
      FROM execution_event event
      JOIN risk_decision decision
        ON decision.risk_decision_id = NEW.risk_decision_id
      JOIN opportunity_proposal proposal
        ON proposal.proposal_id = NEW.proposal_id
     WHERE event.campaign_id = NEW.campaign_id
       AND event.order_id = NEW.order_id
       AND event.sequence = NEW.last_execution_sequence;
    IF transition_event_type IS NULL
       OR (NEW.state = 'OPEN'
           AND (transition_event_type <> 'OPENED'
                OR transition_occurred_at < authorization_time
                OR transition_occurred_at >= NEW.expires_at))
       OR (NEW.state = 'EXPIRED'
           AND (transition_event_type <> 'EXPIRED'
                OR transition_occurred_at <> NEW.expires_at))
       OR (NEW.state = 'PARTIALLY_FILLED'
           AND (transition_event_type <> 'FILL'
                OR transition_occurred_at >= NEW.expires_at
                OR transition_fill_quantity <> NEW.filled_quantity - OLD.filled_quantity
                OR transition_fill_price > NEW.limit_price
                OR OLD.reserved_maximum_loss <> loss_per_unit * OLD.quantity
                OR NEW.reserved_maximum_loss
                   <> loss_per_unit * (NEW.quantity - NEW.filled_quantity)))
       OR (NEW.state = 'CANCELED'
           AND (transition_event_type <> 'CANCELED'
                OR transition_occurred_at >= NEW.expires_at
                OR OLD.reserved_maximum_loss
                   <> loss_per_unit * (OLD.quantity - OLD.filled_quantity))) THEN
        RAISE EXCEPTION 'order transition requires its immutable execution event';
    END IF;
    RETURN NEW;
END;
$$;
"""


RESERVATION_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_reservation_execution_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    durable_order_state text;
    durable_order_reserved numeric(38,18);
    transition_event_type text;
    transition_occurred_at timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'durable reservation cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
            'current_amount', 'state', 'released_at', 'canonical_payload_hash'
        ])
       <>
       (to_jsonb(NEW) - ARRAY[
            'current_amount', 'state', 'released_at', 'canonical_payload_hash'
        ]) THEN
        RAISE EXCEPTION 'immutable reservation terms cannot change';
    END IF;
    IF NOT (
        (OLD.state = 'HELD'
         AND NEW.state = 'OPEN'
         AND NEW.current_amount = OLD.current_amount
         AND NEW.released_at IS NULL)
        OR
        (OLD.state = 'OPEN'
         AND NEW.state = 'OPEN'
         AND OLD.current_amount > NEW.current_amount
         AND NEW.current_amount > 0
         AND NEW.released_at IS NULL)
        OR
        (OLD.state = 'OPEN'
         AND NEW.state = 'RELEASED'
         AND OLD.current_amount > 0
         AND NEW.current_amount = 0
         AND NEW.released_at IS NOT NULL)
    ) THEN
        RAISE EXCEPTION 'invalid durable reservation transition';
    END IF;
    SELECT durable_order.state,
           durable_order.reserved_maximum_loss,
           event.event_type,
           event.occurred_at
      INTO durable_order_state,
           durable_order_reserved,
           transition_event_type,
           transition_occurred_at
      FROM "order" durable_order
      JOIN execution_event event
        ON event.campaign_id = durable_order.campaign_id
       AND event.order_id = durable_order.order_id
       AND event.sequence = durable_order.last_execution_sequence
     WHERE durable_order.order_id = NEW.order_id;
    IF transition_event_type IS NULL
       OR (OLD.state = 'HELD'
           AND (durable_order_state <> 'OPEN'
                OR transition_event_type <> 'OPENED'
                OR NEW.current_amount <> durable_order_reserved))
       OR (NEW.state = 'OPEN' AND OLD.state = 'OPEN'
           AND (durable_order_state <> 'PARTIALLY_FILLED'
                OR transition_event_type <> 'FILL'
                OR NEW.current_amount <> durable_order_reserved))
       OR (NEW.state = 'RELEASED'
           AND (durable_order_state NOT IN ('EXPIRED', 'CANCELED')
                OR transition_event_type
                   <> CASE durable_order_state
                          WHEN 'EXPIRED' THEN 'EXPIRED'
                          ELSE 'CANCELED'
                      END
                OR NEW.released_at <> transition_occurred_at)) THEN
        RAISE EXCEPTION 'reservation transition requires the matching order event';
    END IF;
    RETURN NEW;
END;
$$;
"""


POSITION_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_position_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    fill_type text;
    fill_campaign_id text;
    fill_order_id text;
    fill_sequence integer;
    fill_quantity numeric(38,18);
    fill_price numeric(38,18);
    durable_order_state text;
    durable_order_sequence integer;
    durable_order_filled numeric(38,18);
    durable_order_side text;
    proposal_instrument_id text;
    proposal_rule_version_id text;
    proposal_loss_per_unit numeric(38,18);
BEGIN
    SELECT event.event_type,
           event.campaign_id,
           event.order_id,
           event.sequence,
           event.fill_quantity,
           event.fill_price,
           durable_order.state,
           durable_order.last_execution_sequence,
           durable_order.filled_quantity,
           durable_order.side,
           proposal.instrument_id,
           proposal.rule_version_id,
           proposal.maximum_loss_per_unit
      INTO fill_type,
           fill_campaign_id,
           fill_order_id,
           fill_sequence,
           fill_quantity,
           fill_price,
           durable_order_state,
           durable_order_sequence,
           durable_order_filled,
           durable_order_side,
           proposal_instrument_id,
           proposal_rule_version_id,
           proposal_loss_per_unit
      FROM execution_event event
      JOIN "order" durable_order
        ON durable_order.order_id = event.order_id
       AND durable_order.campaign_id = event.campaign_id
      JOIN opportunity_proposal proposal
        ON proposal.proposal_id = durable_order.proposal_id
     WHERE event.event_id = NEW.originating_fill_event_id;
    IF fill_type IS NULL
       OR fill_type <> 'FILL'
       OR fill_campaign_id <> NEW.campaign_id
       OR fill_order_id <> NEW.originating_order_id
       OR durable_order_state <> 'PARTIALLY_FILLED'
       OR durable_order_sequence <> fill_sequence
       OR durable_order_filled <> fill_quantity
       OR durable_order_side <> NEW.side
       OR proposal_instrument_id <> NEW.instrument_id
       OR proposal_rule_version_id <> NEW.rule_version_id
       OR NEW.open_quantity <> fill_quantity
       OR NEW.cost_basis <> fill_quantity * fill_price
       OR NEW.maximum_remaining_loss <> fill_quantity * proposal_loss_per_unit
       OR NEW.settlement_state <> 'OPEN' THEN
        RAISE EXCEPTION 'position requires the exact durable partial fill';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER apex_guard_position_insert_trigger
BEFORE INSERT ON position
FOR EACH ROW EXECUTE FUNCTION apex_guard_position_insert();
"""


ORDER_GUARD_0002_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_order_execution_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    transition_event_type text;
    transition_occurred_at timestamptz;
    authorization_time timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'durable order cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
            'reserved_maximum_loss', 'state', 'last_execution_sequence',
            'row_version', 'canonical_payload_hash'
        ])
       <>
       (to_jsonb(NEW) - ARRAY[
            'reserved_maximum_loss', 'state', 'last_execution_sequence',
            'row_version', 'canonical_payload_hash'
        ]) THEN
        RAISE EXCEPTION 'immutable order terms cannot change';
    END IF;
    IF NOT (
        (OLD.state = 'READY'
         AND NEW.state = 'OPEN'
         AND OLD.last_execution_sequence = 0
         AND NEW.last_execution_sequence = 1
         AND NEW.row_version = OLD.row_version + 1
         AND NEW.reserved_maximum_loss = OLD.reserved_maximum_loss)
        OR
        (OLD.state = 'OPEN'
         AND NEW.state = 'EXPIRED'
         AND OLD.last_execution_sequence = 1
         AND NEW.last_execution_sequence = 2
         AND NEW.row_version = OLD.row_version + 1
         AND OLD.reserved_maximum_loss > 0
         AND NEW.reserved_maximum_loss = 0)
    ) THEN
        RAISE EXCEPTION 'invalid durable order transition';
    END IF;
    SELECT event.event_type, event.occurred_at, decision.authorization_at
      INTO transition_event_type, transition_occurred_at, authorization_time
      FROM execution_event event
      JOIN risk_decision decision
        ON decision.risk_decision_id = NEW.risk_decision_id
     WHERE event.campaign_id = NEW.campaign_id
       AND event.order_id = NEW.order_id
       AND event.sequence = NEW.last_execution_sequence;
    IF transition_event_type IS NULL
       OR (NEW.state = 'OPEN'
           AND (transition_event_type <> 'OPENED'
                OR transition_occurred_at < authorization_time
                OR transition_occurred_at >= NEW.expires_at))
       OR (NEW.state = 'EXPIRED'
           AND (transition_event_type <> 'EXPIRED'
                OR transition_occurred_at <> NEW.expires_at)) THEN
        RAISE EXCEPTION 'order transition requires its immutable execution event';
    END IF;
    RETURN NEW;
END;
$$;
"""


RESERVATION_GUARD_0002_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_reservation_execution_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    durable_order_state text;
    transition_event_type text;
    transition_occurred_at timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'durable reservation cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
            'current_amount', 'state', 'released_at', 'canonical_payload_hash'
        ])
       <>
       (to_jsonb(NEW) - ARRAY[
            'current_amount', 'state', 'released_at', 'canonical_payload_hash'
        ]) THEN
        RAISE EXCEPTION 'immutable reservation terms cannot change';
    END IF;
    IF NOT (
        (OLD.state = 'HELD'
         AND NEW.state = 'OPEN'
         AND NEW.current_amount = OLD.current_amount
         AND NEW.released_at IS NULL)
        OR
        (OLD.state = 'OPEN'
         AND NEW.state = 'RELEASED'
         AND OLD.current_amount = OLD.maximum_loss
         AND NEW.current_amount = 0
         AND NEW.released_at IS NOT NULL)
    ) THEN
        RAISE EXCEPTION 'invalid durable reservation transition';
    END IF;
    SELECT durable_order.state, event.event_type, event.occurred_at
      INTO durable_order_state, transition_event_type, transition_occurred_at
      FROM "order" durable_order
      JOIN execution_event event
        ON event.campaign_id = durable_order.campaign_id
       AND event.order_id = durable_order.order_id
       AND event.sequence = durable_order.last_execution_sequence
     WHERE durable_order.order_id = NEW.order_id;
    IF transition_event_type IS NULL
       OR (NEW.state = 'OPEN'
           AND (durable_order_state <> 'OPEN' OR transition_event_type <> 'OPENED'))
       OR (NEW.state = 'RELEASED'
           AND (durable_order_state <> 'EXPIRED'
                OR transition_event_type <> 'EXPIRED'
                OR NEW.released_at <> transition_occurred_at)) THEN
        RAISE EXCEPTION 'reservation transition requires the matching order event';
    END IF;
    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    """Add only the durable strict-partial-fill/cancellation boundary."""

    op.add_column("execution_event", sa.Column("fill_quantity", DECIMAL_TYPE, nullable=True))
    op.add_column("execution_event", sa.Column("fill_price", DECIMAL_TYPE, nullable=True))
    op.add_column("execution_event", sa.Column("execution_fee", DECIMAL_TYPE, nullable=True))
    op.drop_constraint("ck_execution_event_supported_type", "execution_event", type_="check")
    op.create_check_constraint(
        "ck_execution_event_supported_type",
        "execution_event",
        "event_type IN ('OPENED', 'EXPIRED', 'FILL', 'CANCELED')",
    )
    op.create_check_constraint(
        "ck_execution_event_shape",
        "execution_event",
        "(event_type = 'FILL' AND fill_quantity IS NOT NULL AND fill_quantity > 0 "
        "AND fill_price IS NOT NULL AND fill_price >= 0 "
        "AND execution_fee IS NOT NULL AND execution_fee >= 0) OR "
        "(event_type IN ('OPENED', 'EXPIRED', 'CANCELED') "
        "AND fill_quantity IS NULL AND fill_price IS NULL AND execution_fee IS NULL)",
    )

    op.drop_constraint("ck_order_durable_filled_quantity", "order", type_="check")
    op.drop_constraint("ck_order_durable_lifecycle", "order", type_="check")
    op.create_check_constraint(
        "ck_order_durable_filled_quantity",
        "order",
        "filled_quantity >= 0 AND filled_quantity <= quantity",
    )
    op.create_check_constraint(
        "ck_order_durable_lifecycle",
        "order",
        "(state = 'READY' AND last_execution_sequence = 0 AND row_version = 1 "
        "AND filled_quantity = 0 AND reserved_maximum_loss > 0) OR "
        "(state = 'OPEN' AND last_execution_sequence = 1 AND row_version = 2 "
        "AND executable_ask IS NOT NULL AND expires_at IS NOT NULL "
        "AND filled_quantity = 0 AND reserved_maximum_loss > 0) OR "
        "(state = 'EXPIRED' AND last_execution_sequence = 2 AND row_version = 3 "
        "AND executable_ask IS NOT NULL AND expires_at IS NOT NULL "
        "AND filled_quantity = 0 AND reserved_maximum_loss = 0) OR "
        "(state = 'PARTIALLY_FILLED' AND last_execution_sequence = 2 AND row_version = 3 "
        "AND executable_ask IS NOT NULL AND expires_at IS NOT NULL "
        "AND filled_quantity > 0 AND filled_quantity < quantity "
        "AND reserved_maximum_loss > 0) OR "
        "(state = 'CANCELED' AND last_execution_sequence = 3 AND row_version = 4 "
        "AND executable_ask IS NOT NULL AND expires_at IS NOT NULL "
        "AND filled_quantity > 0 AND filled_quantity < quantity "
        "AND reserved_maximum_loss = 0)",
    )

    op.drop_constraint("ck_risk_reservation_lifecycle", "risk_reservation", type_="check")
    op.create_check_constraint(
        "ck_risk_reservation_lifecycle",
        "risk_reservation",
        "(state = 'HELD' AND current_amount = maximum_loss AND released_at IS NULL) OR "
        "(state = 'OPEN' AND current_amount > 0 AND current_amount <= maximum_loss "
        "AND released_at IS NULL) OR "
        "(state = 'RELEASED' AND current_amount = 0 AND released_at IS NOT NULL)",
    )

    op.create_table(
        "position",
        sa.Column("position_id", sa.String(160), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(160),
            sa.ForeignKey("campaign.campaign_id"),
            nullable=False,
        ),
        sa.Column(
            "originating_order_id",
            sa.String(160),
            sa.ForeignKey("order.order_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "originating_fill_event_id",
            sa.String(160),
            sa.ForeignKey("execution_event.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("instrument_id", sa.String(160), nullable=False),
        sa.Column("rule_version_id", sa.String(160), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("open_quantity", DECIMAL_TYPE, nullable=False),
        sa.Column("quantity_increment", DECIMAL_TYPE, nullable=False),
        sa.Column("cost_basis", DECIMAL_TYPE, nullable=False),
        sa.Column("maximum_remaining_loss", DECIMAL_TYPE, nullable=False),
        sa.Column("settlement_state", sa.String(30), nullable=False),
        sa.Column("canonical_payload_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("side IN ('YES', 'NO')", name="ck_position_side"),
        sa.CheckConstraint("open_quantity > 0", name="ck_position_open_quantity"),
        sa.CheckConstraint("quantity_increment > 0", name="ck_position_quantity_increment"),
        sa.CheckConstraint("cost_basis > 0", name="ck_position_cost_basis"),
        sa.CheckConstraint(
            "maximum_remaining_loss > 0",
            name="ck_position_maximum_remaining_loss",
        ),
        sa.CheckConstraint("settlement_state = 'OPEN'", name="ck_position_open_only"),
        sa.CheckConstraint(
            "length(canonical_payload_hash) = 64",
            name="ck_position_payload_hash",
        ),
    )
    op.create_index("ix_position_campaign_open", "position", ("campaign_id", "settlement_state"))

    op.execute(sa.text(ORDER_GUARD_DDL))
    op.execute(sa.text(RESERVATION_GUARD_DDL))
    op.execute(sa.text(POSITION_GUARD_DDL))
    op.execute(sa.text(immutable_trigger_ddl("position")))


def downgrade() -> None:
    """Return to revision 0002 only when no partial-fill data exists."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM position) "
            "OR EXISTS (SELECT 1 FROM execution_event WHERE event_type IN ('FILL', 'CANCELED')) "
            "OR EXISTS (SELECT 1 FROM ledger_journal WHERE journal_type IN ('PURCHASE', 'FEE')) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade partial-fill persistence with retained lifecycle data'; "
            "END IF; END $$"
        )
    )
    op.execute(sa.text("DROP TRIGGER apex_immutable_position_trigger ON position"))
    op.execute(sa.text("DROP TRIGGER apex_guard_position_insert_trigger ON position"))
    op.execute(sa.text("DROP FUNCTION apex_guard_position_insert()"))
    op.drop_index("ix_position_campaign_open", table_name="position")
    op.drop_table("position")

    op.drop_constraint("ck_risk_reservation_lifecycle", "risk_reservation", type_="check")
    op.create_check_constraint(
        "ck_risk_reservation_lifecycle",
        "risk_reservation",
        "(state IN ('HELD', 'OPEN') AND current_amount = maximum_loss "
        "AND released_at IS NULL) OR "
        "(state = 'RELEASED' AND current_amount = 0 AND released_at IS NOT NULL)",
    )
    op.drop_constraint("ck_order_durable_lifecycle", "order", type_="check")
    op.drop_constraint("ck_order_durable_filled_quantity", "order", type_="check")
    op.create_check_constraint(
        "ck_order_durable_filled_quantity",
        "order",
        "filled_quantity = 0 AND filled_quantity >= 0 AND filled_quantity <= quantity",
    )
    op.create_check_constraint(
        "ck_order_durable_lifecycle",
        "order",
        "(state = 'READY' AND last_execution_sequence = 0 AND row_version = 1 "
        "AND reserved_maximum_loss > 0) OR "
        "(state = 'OPEN' AND last_execution_sequence = 1 AND row_version = 2 "
        "AND executable_ask IS NOT NULL AND expires_at IS NOT NULL "
        "AND reserved_maximum_loss > 0) OR "
        "(state = 'EXPIRED' AND last_execution_sequence = 2 AND row_version = 3 "
        "AND executable_ask IS NOT NULL AND expires_at IS NOT NULL "
        "AND reserved_maximum_loss = 0)",
    )
    op.execute(sa.text(ORDER_GUARD_0002_DDL))
    op.execute(sa.text(RESERVATION_GUARD_0002_DDL))

    op.drop_constraint("ck_execution_event_shape", "execution_event", type_="check")
    op.drop_constraint("ck_execution_event_supported_type", "execution_event", type_="check")
    op.create_check_constraint(
        "ck_execution_event_supported_type",
        "execution_event",
        "event_type IN ('OPENED', 'EXPIRED')",
    )
    op.drop_column("execution_event", "execution_fee")
    op.drop_column("execution_event", "fill_price")
    op.drop_column("execution_event", "fill_quantity")
