"""Persist durable synthetic OPENED and EXPIRED execution events.

Revision ID: 0002_durable_execution_events
Revises: 0001_atomic_authorization
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import DECIMAL_TYPE, immutable_trigger_ddl

revision: str = "0002_durable_execution_events"
down_revision: str | None = "0001_atomic_authorization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ORDER_GUARD_DDL = r"""
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

CREATE TRIGGER apex_guard_order_execution_transition_trigger
BEFORE UPDATE OR DELETE ON "order"
FOR EACH ROW EXECUTE FUNCTION apex_guard_order_execution_transition();
"""


RESERVATION_GUARD_DDL = r"""
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
    SELECT o.state, e.event_type, e.occurred_at
      INTO durable_order_state, transition_event_type, transition_occurred_at
      FROM "order" o
      JOIN execution_event e
        ON e.campaign_id = o.campaign_id
       AND e.order_id = o.order_id
       AND e.sequence = o.last_execution_sequence
     WHERE o.order_id = NEW.order_id;
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

CREATE TRIGGER apex_guard_reservation_execution_transition_trigger
BEFORE UPDATE OR DELETE ON risk_reservation
FOR EACH ROW EXECUTE FUNCTION apex_guard_reservation_execution_transition();
"""


def upgrade() -> None:
    """Add only the durable unfilled-expiration execution boundary."""

    op.add_column(
        "opportunity_proposal",
        sa.Column("resting_limit_price", DECIMAL_TYPE, nullable=True),
    )
    op.add_column(
        "opportunity_proposal",
        sa.Column("order_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "opportunity_proposal_proposal_type_check",
        "opportunity_proposal",
        type_="check",
    )
    op.create_check_constraint(
        "ck_opportunity_proposal_durable_type",
        "opportunity_proposal",
        "proposal_type IN ('MARKETABLE', 'RESTING_LIMIT')",
    )
    op.create_check_constraint(
        "ck_opportunity_proposal_resting_terms",
        "opportunity_proposal",
        "(proposal_type = 'MARKETABLE' AND resting_limit_price IS NULL "
        "AND order_expires_at IS NULL) OR "
        "(proposal_type = 'RESTING_LIMIT' AND resting_limit_price > 0 "
        "AND order_expires_at IS NOT NULL)",
    )

    op.add_column("order", sa.Column("executable_ask", DECIMAL_TYPE, nullable=True))
    op.add_column(
        "order",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    for constraint_name in (
        "order_filled_quantity_check",
        "order_last_execution_sequence_check",
        "order_row_version_check",
        "order_state_check",
    ):
        op.drop_constraint(constraint_name, "order", type_="check")
    op.create_check_constraint(
        "ck_order_durable_filled_quantity",
        "order",
        "filled_quantity = 0 AND filled_quantity >= 0 AND filled_quantity <= quantity",
    )
    op.create_check_constraint(
        "ck_order_durable_expiring_terms",
        "order",
        "(executable_ask IS NULL AND expires_at IS NULL) OR "
        "(executable_ask > limit_price AND expires_at IS NOT NULL)",
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

    op.add_column(
        "risk_reservation",
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("risk_reservation_check", "risk_reservation", type_="check")
    op.drop_constraint(
        "risk_reservation_state_check",
        "risk_reservation",
        type_="check",
    )
    op.create_check_constraint(
        "ck_risk_reservation_current_amount",
        "risk_reservation",
        "current_amount >= 0 AND current_amount <= maximum_loss",
    )
    op.create_check_constraint(
        "ck_risk_reservation_lifecycle",
        "risk_reservation",
        "(state IN ('HELD', 'OPEN') AND current_amount = maximum_loss "
        "AND released_at IS NULL) OR "
        "(state = 'RELEASED' AND current_amount = 0 AND released_at IS NOT NULL)",
    )

    op.create_table(
        "execution_event",
        sa.Column("event_id", sa.String(160), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(160),
            sa.ForeignKey("campaign.campaign_id"),
            nullable=False,
        ),
        sa.Column(
            "order_id",
            sa.String(160),
            sa.ForeignKey("order.order_id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical_payload_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("order_id", "sequence", name="uq_execution_event_order_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_execution_event_positive_sequence"),
        sa.CheckConstraint(
            "event_type IN ('OPENED', 'EXPIRED')",
            name="ck_execution_event_supported_type",
        ),
        sa.CheckConstraint(
            "length(canonical_payload_hash) = 64",
            name="ck_execution_event_payload_hash",
        ),
    )
    op.create_index(
        "ix_execution_event_order_sequence",
        "execution_event",
        ("order_id", "sequence"),
    )
    op.execute(sa.text(immutable_trigger_ddl("execution_event")))

    op.execute(sa.text('DROP TRIGGER apex_immutable_order_trigger ON "order"'))
    op.execute(sa.text("DROP TRIGGER apex_immutable_risk_reservation_trigger ON risk_reservation"))
    op.execute(sa.text(ORDER_GUARD_DDL))
    op.execute(sa.text(RESERVATION_GUARD_DDL))


def downgrade() -> None:
    """Return to revision 0001 only when no durable lifecycle data exists."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM execution_event) "
            "OR EXISTS (SELECT 1 FROM opportunity_proposal "
            "WHERE proposal_type = 'RESTING_LIMIT') "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade durable execution with retained lifecycle data'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_reservation_execution_transition_trigger ON risk_reservation"
        )
    )
    op.execute(sa.text('DROP TRIGGER apex_guard_order_execution_transition_trigger ON "order"'))
    op.execute(sa.text("DROP FUNCTION apex_guard_reservation_execution_transition()"))
    op.execute(sa.text("DROP FUNCTION apex_guard_order_execution_transition()"))

    op.drop_index("ix_execution_event_order_sequence", table_name="execution_event")
    op.drop_table("execution_event")

    op.drop_constraint(
        "ck_risk_reservation_lifecycle",
        "risk_reservation",
        type_="check",
    )
    op.drop_constraint(
        "ck_risk_reservation_current_amount",
        "risk_reservation",
        type_="check",
    )
    op.create_check_constraint(
        "risk_reservation_check",
        "risk_reservation",
        "current_amount = maximum_loss",
    )
    op.create_check_constraint(
        "risk_reservation_state_check",
        "risk_reservation",
        "state = 'HELD'",
    )
    op.drop_column("risk_reservation", "released_at")

    op.drop_constraint("ck_order_durable_lifecycle", "order", type_="check")
    op.drop_constraint("ck_order_durable_expiring_terms", "order", type_="check")
    op.drop_constraint("ck_order_durable_filled_quantity", "order", type_="check")
    op.create_check_constraint("order_filled_quantity_check", "order", "filled_quantity = 0")
    op.create_check_constraint(
        "order_last_execution_sequence_check",
        "order",
        "last_execution_sequence = 0",
    )
    op.create_check_constraint("order_row_version_check", "order", "row_version = 1")
    op.create_check_constraint("order_state_check", "order", "state = 'READY'")
    op.drop_column("order", "expires_at")
    op.drop_column("order", "executable_ask")

    op.drop_constraint(
        "ck_opportunity_proposal_resting_terms",
        "opportunity_proposal",
        type_="check",
    )
    op.drop_constraint(
        "ck_opportunity_proposal_durable_type",
        "opportunity_proposal",
        type_="check",
    )
    op.create_check_constraint(
        "opportunity_proposal_proposal_type_check",
        "opportunity_proposal",
        "proposal_type = 'MARKETABLE'",
    )
    op.drop_column("opportunity_proposal", "order_expires_at")
    op.drop_column("opportunity_proposal", "resting_limit_price")

    op.execute(sa.text(immutable_trigger_ddl("order")))
    op.execute(sa.text(immutable_trigger_ddl("risk_reservation")))
