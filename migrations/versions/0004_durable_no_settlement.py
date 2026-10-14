"""Persist one exact NO settlement for a canceled partial-fill Position.

Revision ID: 0004_durable_no_settlement
Revises: 0003_partial_fill_and_position
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import DECIMAL_TYPE, immutable_trigger_ddl

revision: str = "0004_durable_no_settlement"
down_revision: str | None = "0003_partial_fill_and_position"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


POSITION_SETTLEMENT_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_position_settlement_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    durable_position record;
    durable_order record;
    durable_reservation record;
    durable_fact record;
    durable_journal record;
    canceled_at timestamptz;
    posting_count integer;
    realized_debit numeric(38,18);
    position_credit numeric(38,18);
BEGIN
    SELECT * INTO durable_position FROM position WHERE position_id = NEW.position_id;
    SELECT * INTO durable_order FROM "order" WHERE order_id = durable_position.originating_order_id;
    SELECT * INTO durable_reservation FROM risk_reservation WHERE order_id = durable_order.order_id;
    SELECT * INTO durable_fact FROM settlement_fact
      WHERE settlement_fact_id = NEW.settlement_fact_id;
    SELECT * INTO durable_journal FROM ledger_journal WHERE journal_id = NEW.settlement_journal_id;
    SELECT occurred_at INTO canceled_at FROM execution_event
      WHERE order_id = durable_order.order_id AND event_type = 'CANCELED' AND sequence = 3;
    IF durable_position.position_id IS NULL
       OR durable_order.order_id IS NULL
       OR durable_reservation.reservation_id IS NULL
       OR durable_fact.settlement_fact_id IS NULL
       OR durable_journal.journal_id IS NULL
       OR durable_position.campaign_id <> NEW.campaign_id
       OR durable_order.campaign_id <> NEW.campaign_id
       OR durable_fact.campaign_id <> NEW.campaign_id
       OR durable_order.state <> 'CANCELED'
       OR durable_order.filled_quantity <= 0
       OR durable_reservation.state <> 'RELEASED'
       OR durable_reservation.current_amount <> 0
       OR durable_position.settlement_state <> 'OPEN'
       OR durable_position.open_quantity <= 0
       OR durable_fact.instrument_id <> durable_position.instrument_id
       OR durable_fact.rule_version_id <> durable_position.rule_version_id
       OR durable_fact.rule_content_hash <> (
           SELECT rule_content_hash FROM opportunity_proposal
             WHERE proposal_id = durable_order.proposal_id
       )
       OR durable_fact.resolution_source <> (
           SELECT resolution_source FROM opportunity_proposal
             WHERE proposal_id = durable_order.proposal_id
       )
       OR durable_fact.classification <> 'NO'
       OR durable_fact.settlement_value <> 0
       OR durable_fact.determination_at <> (
           SELECT rule_determination_at FROM opportunity_proposal
             WHERE proposal_id = durable_order.proposal_id
       )
       OR canceled_at IS NULL
       OR durable_fact.source_event_at <= canceled_at
       OR durable_fact.available_at <= canceled_at
       OR durable_fact.source_event_at < durable_fact.determination_at
       OR durable_fact.available_at < durable_fact.determination_at
       OR NEW.settled_quantity <> durable_position.open_quantity
       OR NEW.settlement_value <> durable_fact.settlement_value
       OR NEW.settlement_cash <> 0
       OR NEW.realized_contract_pnl <> -durable_position.cost_basis
       OR durable_journal.campaign_id <> NEW.campaign_id
       OR durable_journal.source_kind <> 'settlement'
       OR durable_journal.source_id <> NEW.position_settlement_id
       OR durable_journal.journal_type <> 'SETTLEMENT'
       OR durable_journal.status <> 'POSTED'
    THEN
        RAISE EXCEPTION 'position settlement requires exact canceled-position evidence';
    END IF;
    SELECT count(*),
           coalesce(sum(CASE WHEN account.account_code = 'REALIZED_PNL'
                              AND posting.side = 'DEBIT' THEN posting.amount ELSE 0 END), 0),
           coalesce(sum(CASE WHEN account.account_code = 'POSITION_COST'
                              AND posting.side = 'CREDIT' THEN posting.amount ELSE 0 END), 0)
      INTO posting_count, realized_debit, position_credit
      FROM ledger_posting posting
      JOIN ledger_account account ON account.account_id = posting.account_id
     WHERE posting.journal_id = NEW.settlement_journal_id;
    IF posting_count <> 2
       OR realized_debit <> -NEW.realized_contract_pnl
       OR position_credit <> -NEW.realized_contract_pnl THEN
        RAISE EXCEPTION 'position settlement journal does not derecognize exact cost';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER apex_guard_position_settlement_insert_trigger
BEFORE INSERT OR UPDATE OR DELETE ON position_settlement
FOR EACH ROW EXECUTE FUNCTION apex_guard_position_settlement_insert();
"""


POSITION_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_position_settlement_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    settlement_row record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'durable position cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
            'open_quantity', 'cost_basis', 'maximum_remaining_loss',
            'settlement_state', 'canonical_payload_hash'
        ])
       <> (to_jsonb(NEW) - ARRAY[
            'open_quantity', 'cost_basis', 'maximum_remaining_loss',
            'settlement_state', 'canonical_payload_hash'
        ]) THEN
        RAISE EXCEPTION 'immutable position terms cannot change';
    END IF;
    IF NOT (OLD.settlement_state = 'OPEN'
            AND NEW.settlement_state = 'SETTLED'
            AND OLD.open_quantity > 0
            AND OLD.cost_basis > 0
            AND OLD.maximum_remaining_loss > 0
            AND NEW.open_quantity = 0
            AND NEW.cost_basis = 0
            AND NEW.maximum_remaining_loss = 0) THEN
        RAISE EXCEPTION 'invalid durable position settlement transition';
    END IF;
    SELECT * INTO settlement_row FROM position_settlement
      WHERE position_id = NEW.position_id;
    IF settlement_row.position_settlement_id IS NULL
       OR settlement_row.settled_quantity <> OLD.open_quantity
       OR settlement_row.settlement_cash <> 0
       OR settlement_row.realized_contract_pnl <> -OLD.cost_basis THEN
        RAISE EXCEPTION 'position settlement transition requires matching immutable settlement';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER apex_guard_position_settlement_transition_trigger
BEFORE UPDATE OR DELETE ON position
FOR EACH ROW EXECUTE FUNCTION apex_guard_position_settlement_transition();
"""


def upgrade() -> None:
    """Add the narrow durable NO settlement boundary only."""

    op.create_table(
        "settlement_fact",
        sa.Column("settlement_fact_id", sa.String(160), primary_key=True),
        sa.Column(
            "campaign_id", sa.String(160), sa.ForeignKey("campaign.campaign_id"), nullable=False
        ),
        sa.Column("fixture_id", sa.String(160), nullable=False),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("source_id", sa.String(160), nullable=False),
        sa.Column("instrument_id", sa.String(160), nullable=False),
        sa.Column("rule_version_id", sa.String(160), nullable=False),
        sa.Column("rule_content_hash", sa.String(64), nullable=False),
        sa.Column("resolution_source", sa.String(200), nullable=False),
        sa.Column("classification", sa.String(10), nullable=False),
        sa.Column("settlement_value", DECIMAL_TYPE, nullable=False),
        sa.Column("settlement_value_text", sa.String(40), nullable=False),
        sa.Column("determination_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload_hash", sa.String(64), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("canonical_evidence_hash", sa.String(64), nullable=False),
        sa.Column("canonical_payload_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("campaign_id", "canonical_evidence_hash"),
        sa.CheckConstraint("classification = 'NO'", name="ck_settlement_fact_no_only"),
        sa.CheckConstraint("schema_version = '2'", name="ck_settlement_fact_schema"),
        sa.CheckConstraint(
            "settlement_value >= 0 AND settlement_value <= 1",
            name="ck_settlement_fact_value_bounds",
        ),
        sa.CheckConstraint("source_event_at <= available_at", name="ck_settlement_fact_timing"),
        sa.CheckConstraint("length(rule_content_hash) = 64", name="ck_settlement_fact_rule_hash"),
        sa.CheckConstraint("length(raw_payload_hash) = 64", name="ck_settlement_fact_raw_hash"),
        sa.CheckConstraint(
            "length(canonical_evidence_hash) = 64",
            name="ck_settlement_fact_evidence_hash",
        ),
        sa.CheckConstraint(
            "length(canonical_payload_hash) = 64",
            name="ck_settlement_fact_payload_hash",
        ),
    )
    op.create_table(
        "position_settlement",
        sa.Column("position_settlement_id", sa.String(160), primary_key=True),
        sa.Column(
            "campaign_id", sa.String(160), sa.ForeignKey("campaign.campaign_id"), nullable=False
        ),
        sa.Column(
            "position_id", sa.String(160), sa.ForeignKey("position.position_id"), nullable=False
        ),
        sa.Column(
            "settlement_fact_id",
            sa.String(160),
            sa.ForeignKey("settlement_fact.settlement_fact_id"),
            nullable=False,
        ),
        sa.Column(
            "settlement_journal_id",
            sa.String(160),
            sa.ForeignKey("ledger_journal.journal_id"),
            nullable=False,
        ),
        sa.Column("settled_quantity", DECIMAL_TYPE, nullable=False),
        sa.Column("settlement_value", DECIMAL_TYPE, nullable=False),
        sa.Column("settlement_cash", DECIMAL_TYPE, nullable=False),
        sa.Column("realized_contract_pnl", DECIMAL_TYPE, nullable=False),
        sa.Column("canonical_payload_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("position_id"),
        sa.UniqueConstraint("settlement_fact_id"),
        sa.UniqueConstraint("settlement_journal_id"),
        sa.CheckConstraint("settled_quantity > 0", name="ck_position_settlement_quantity"),
        sa.CheckConstraint("settlement_value = 0", name="ck_position_settlement_no_value"),
        sa.CheckConstraint("settlement_cash = 0", name="ck_position_settlement_zero_cash"),
        sa.CheckConstraint(
            "realized_contract_pnl < 0", name="ck_position_settlement_realized_loss"
        ),
        sa.CheckConstraint(
            "length(canonical_payload_hash) = 64",
            name="ck_position_settlement_payload_hash",
        ),
    )
    op.create_index("ix_settlement_fact_campaign", "settlement_fact", ("campaign_id",))
    op.create_index("ix_position_settlement_campaign", "position_settlement", ("campaign_id",))
    op.execute(sa.text(immutable_trigger_ddl("settlement_fact")))
    op.execute(sa.text(immutable_trigger_ddl("position_settlement")))

    op.drop_constraint("ck_position_open_only", "position", type_="check")
    op.drop_constraint("ck_position_open_quantity", "position", type_="check")
    op.drop_constraint("ck_position_cost_basis", "position", type_="check")
    op.drop_constraint("ck_position_maximum_remaining_loss", "position", type_="check")
    op.create_check_constraint("ck_position_cost_basis", "position", "cost_basis >= 0")
    op.create_check_constraint("ck_position_open_quantity", "position", "open_quantity >= 0")
    op.create_check_constraint(
        "ck_position_maximum_remaining_loss", "position", "maximum_remaining_loss >= 0"
    )
    op.create_check_constraint(
        "ck_position_settlement_lifecycle",
        "position",
        "(settlement_state = 'OPEN' AND open_quantity > 0 AND cost_basis > 0 "
        "AND maximum_remaining_loss > 0) OR "
        "(settlement_state = 'SETTLED' AND open_quantity = 0 AND cost_basis = 0 "
        "AND maximum_remaining_loss = 0)",
    )
    op.execute(sa.text("DROP TRIGGER apex_immutable_position_trigger ON position"))
    op.execute(sa.text(POSITION_SETTLEMENT_GUARD_DDL))
    op.execute(sa.text(POSITION_GUARD_DDL))


def downgrade() -> None:
    """Return to 0003 only when no durable NO settlement evidence exists."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM position_settlement) "
            "OR EXISTS (SELECT 1 FROM settlement_fact) "
            "OR EXISTS (SELECT 1 FROM position WHERE settlement_state = 'SETTLED') "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade durable NO settlement with retained settlement data'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text("DROP TRIGGER apex_guard_position_settlement_transition_trigger ON position")
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_position_settlement_transition()"))
    op.execute(
        sa.text("DROP TRIGGER apex_guard_position_settlement_insert_trigger ON position_settlement")
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_position_settlement_insert()"))
    op.execute(
        sa.text("DROP TRIGGER apex_immutable_position_settlement_trigger ON position_settlement")
    )
    op.execute(sa.text("DROP TRIGGER apex_immutable_settlement_fact_trigger ON settlement_fact"))
    op.drop_index("ix_position_settlement_campaign", table_name="position_settlement")
    op.drop_index("ix_settlement_fact_campaign", table_name="settlement_fact")
    op.drop_table("position_settlement")
    op.drop_table("settlement_fact")

    op.drop_constraint("ck_position_settlement_lifecycle", "position", type_="check")
    op.drop_constraint("ck_position_open_quantity", "position", type_="check")
    op.drop_constraint("ck_position_maximum_remaining_loss", "position", type_="check")
    op.drop_constraint("ck_position_cost_basis", "position", type_="check")
    op.create_check_constraint("ck_position_cost_basis", "position", "cost_basis > 0")
    op.create_check_constraint("ck_position_open_quantity", "position", "open_quantity > 0")
    op.create_check_constraint(
        "ck_position_maximum_remaining_loss", "position", "maximum_remaining_loss > 0"
    )
    op.create_check_constraint("ck_position_open_only", "position", "settlement_state = 'OPEN'")
    op.execute(sa.text(immutable_trigger_ddl("position")))
