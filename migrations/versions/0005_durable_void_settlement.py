"""Extend durable settlement with the exact hash-bound VOID rule term.

Revision ID: 0005_durable_void_settlement
Revises: 0004_durable_no_settlement
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import DECIMAL_TYPE

revision: str = "0005_durable_void_settlement"
down_revision: str | None = "0004_durable_no_settlement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


VOID_POSITION_SETTLEMENT_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_position_settlement_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    durable_position record;
    durable_order record;
    durable_reservation record;
    durable_proposal record;
    durable_fact record;
    durable_journal record;
    canceled_at timestamptz;
    posting_count integer;
    cash_debit numeric(38,18);
    realized_debit numeric(38,18);
    realized_credit numeric(38,18);
    position_credit numeric(38,18);
BEGIN
    SELECT * INTO durable_position FROM position WHERE position_id = NEW.position_id;
    SELECT * INTO durable_order FROM "order" WHERE order_id = durable_position.originating_order_id;
    SELECT * INTO durable_reservation FROM risk_reservation WHERE order_id = durable_order.order_id;
    SELECT * INTO durable_proposal FROM opportunity_proposal
      WHERE proposal_id = durable_order.proposal_id;
    SELECT * INTO durable_fact FROM settlement_fact
      WHERE settlement_fact_id = NEW.settlement_fact_id;
    SELECT * INTO durable_journal FROM ledger_journal WHERE journal_id = NEW.settlement_journal_id;
    SELECT occurred_at INTO canceled_at FROM execution_event
      WHERE order_id = durable_order.order_id AND event_type = 'CANCELED' AND sequence = 3;
    IF durable_position.position_id IS NULL
       OR durable_order.order_id IS NULL
       OR durable_reservation.reservation_id IS NULL
       OR durable_proposal.proposal_id IS NULL
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
       OR durable_fact.rule_content_hash <> durable_proposal.rule_content_hash
       OR durable_fact.resolution_source <> durable_proposal.resolution_source
       OR durable_fact.determination_at <> durable_proposal.rule_determination_at
       OR canceled_at IS NULL
       OR durable_fact.source_event_at <= canceled_at
       OR durable_fact.available_at <= canceled_at
       OR durable_fact.source_event_at < durable_fact.determination_at
       OR durable_fact.available_at < durable_fact.determination_at
       OR (durable_fact.classification = 'NO' AND (
             durable_fact.settlement_value <> 0
             OR durable_fact.void_term_content_hash IS NOT NULL
          ))
       OR (durable_fact.classification = 'VOID' AND (
             durable_fact.settlement_value <= 0
             OR durable_fact.void_term_content_hash IS NULL
             OR durable_fact.void_term_content_hash <> durable_proposal.void_term_content_hash
             OR durable_fact.settlement_value <> durable_proposal.void_term_settlement_value
             OR durable_fact.settlement_value_text
                <> durable_proposal.void_term_settlement_value_text
          ))
       OR durable_fact.classification NOT IN ('NO', 'VOID')
       OR NEW.settled_quantity <> durable_position.open_quantity
       OR NEW.settlement_value <> durable_fact.settlement_value
       OR NEW.settlement_cash <> NEW.settled_quantity * NEW.settlement_value
       OR NEW.realized_contract_pnl <> NEW.settlement_cash - durable_position.cost_basis
       OR durable_journal.campaign_id <> NEW.campaign_id
       OR durable_journal.source_kind <> 'settlement'
       OR durable_journal.source_id <> NEW.position_settlement_id
       OR durable_journal.journal_type <> 'SETTLEMENT'
       OR durable_journal.status <> 'POSTED'
    THEN
        RAISE EXCEPTION 'position settlement requires exact canceled-position evidence';
    END IF;
    SELECT count(*),
           coalesce(sum(CASE WHEN account.account_code = 'CASH'
                              AND posting.side = 'DEBIT' THEN posting.amount ELSE 0 END), 0),
           coalesce(sum(CASE WHEN account.account_code = 'REALIZED_PNL'
                              AND posting.side = 'DEBIT' THEN posting.amount ELSE 0 END), 0),
           coalesce(sum(CASE WHEN account.account_code = 'REALIZED_PNL'
                              AND posting.side = 'CREDIT' THEN posting.amount ELSE 0 END), 0),
           coalesce(sum(CASE WHEN account.account_code = 'POSITION_COST'
                              AND posting.side = 'CREDIT' THEN posting.amount ELSE 0 END), 0)
      INTO posting_count, cash_debit, realized_debit, realized_credit, position_credit
      FROM ledger_posting posting
      JOIN ledger_account account ON account.account_id = posting.account_id
     WHERE posting.journal_id = NEW.settlement_journal_id;
    IF position_credit <> durable_position.cost_basis
       OR cash_debit <> NEW.settlement_cash
       OR (NEW.realized_contract_pnl < 0 AND (
             posting_count <> 2
             OR realized_debit <> -NEW.realized_contract_pnl
             OR realized_credit <> 0
          ))
       OR (NEW.realized_contract_pnl > 0 AND (
             posting_count <> 3
             OR realized_debit <> 0
             OR realized_credit <> NEW.realized_contract_pnl
          ))
    THEN
        RAISE EXCEPTION 'position settlement journal does not match exact proceeds and cost';
    END IF;
    RETURN NEW;
END;
$$;
"""


VOID_POSITION_GUARD_DDL = r"""
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
       OR settlement_row.settlement_cash < 0
       OR settlement_row.realized_contract_pnl
          <> settlement_row.settlement_cash - OLD.cost_basis THEN
        RAISE EXCEPTION 'position settlement transition requires matching immutable settlement';
    END IF;
    RETURN NEW;
END;
$$;
"""


NO_POSITION_SETTLEMENT_GUARD_DDL = r"""
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
"""


NO_POSITION_GUARD_DDL = r"""
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
"""


def upgrade() -> None:
    """Permit only the existing NO path and exact hash-bound VOID evidence."""

    op.add_column(
        "opportunity_proposal",
        sa.Column("void_term_outcome_classification", sa.String(10), nullable=True),
    )
    op.add_column(
        "opportunity_proposal",
        sa.Column("void_term_settlement_value", DECIMAL_TYPE, nullable=True),
    )
    op.add_column(
        "opportunity_proposal",
        sa.Column("void_term_settlement_value_text", sa.String(40), nullable=True),
    )
    op.add_column(
        "opportunity_proposal",
        sa.Column("void_term_content_hash", sa.String(64), nullable=True),
    )
    op.create_check_constraint(
        "ck_opportunity_proposal_void_rule_term",
        "opportunity_proposal",
        "(void_term_outcome_classification IS NULL "
        "AND void_term_settlement_value IS NULL "
        "AND void_term_settlement_value_text IS NULL "
        "AND void_term_content_hash IS NULL) OR "
        "(void_term_outcome_classification = 'VOID' "
        "AND void_term_settlement_value > 0 "
        "AND void_term_settlement_value <= 1 "
        "AND void_term_settlement_value_text IS NOT NULL "
        "AND length(void_term_content_hash) = 64)",
    )
    op.add_column(
        "settlement_fact",
        sa.Column("void_term_content_hash", sa.String(64), nullable=True),
    )
    op.drop_constraint("ck_settlement_fact_no_only", "settlement_fact", type_="check")
    op.create_check_constraint(
        "ck_settlement_fact_classification",
        "settlement_fact",
        "classification IN ('NO', 'VOID')",
    )
    op.create_check_constraint(
        "ck_settlement_fact_void_authority",
        "settlement_fact",
        "(classification = 'NO' AND settlement_value = 0 "
        "AND void_term_content_hash IS NULL) OR "
        "(classification = 'VOID' AND settlement_value > 0 "
        "AND length(void_term_content_hash) = 64)",
    )
    op.drop_constraint("ck_position_settlement_no_value", "position_settlement", type_="check")
    op.drop_constraint("ck_position_settlement_zero_cash", "position_settlement", type_="check")
    op.drop_constraint("ck_position_settlement_realized_loss", "position_settlement", type_="check")
    op.create_check_constraint(
        "ck_position_settlement_value_bounds",
        "position_settlement",
        "settlement_value >= 0 AND settlement_value <= 1",
    )
    op.create_check_constraint(
        "ck_position_settlement_cash_nonnegative",
        "position_settlement",
        "settlement_cash >= 0",
    )
    op.create_check_constraint(
        "ck_position_settlement_realized_nonzero",
        "position_settlement",
        "realized_contract_pnl <> 0",
    )
    op.execute(sa.text(VOID_POSITION_SETTLEMENT_GUARD_DDL))
    op.execute(sa.text(VOID_POSITION_GUARD_DDL))


def downgrade() -> None:
    """Return to 0004 only when no VOID evidence needs to be retained."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM settlement_fact "
            "           WHERE classification = 'VOID' "
            "              OR void_term_content_hash IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM position_settlement settlement "
            "           JOIN settlement_fact fact "
            "             ON fact.settlement_fact_id = settlement.settlement_fact_id "
            "           WHERE fact.classification = 'VOID' "
            "              OR fact.void_term_content_hash IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM opportunity_proposal "
            "           WHERE void_term_outcome_classification IS NOT NULL "
            "              OR void_term_settlement_value IS NOT NULL "
            "              OR void_term_settlement_value_text IS NOT NULL "
            "              OR void_term_content_hash IS NOT NULL) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0005 to 0004: retained durable VOID evidence would be lost'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint(
        "ck_position_settlement_realized_nonzero", "position_settlement", type_="check"
    )
    op.drop_constraint(
        "ck_position_settlement_cash_nonnegative", "position_settlement", type_="check"
    )
    op.drop_constraint("ck_position_settlement_value_bounds", "position_settlement", type_="check")
    op.create_check_constraint(
        "ck_position_settlement_no_value", "position_settlement", "settlement_value = 0"
    )
    op.create_check_constraint(
        "ck_position_settlement_zero_cash", "position_settlement", "settlement_cash = 0"
    )
    op.create_check_constraint(
        "ck_position_settlement_realized_loss", "position_settlement", "realized_contract_pnl < 0"
    )
    op.drop_constraint("ck_settlement_fact_void_authority", "settlement_fact", type_="check")
    op.drop_constraint("ck_settlement_fact_classification", "settlement_fact", type_="check")
    op.create_check_constraint(
        "ck_settlement_fact_no_only", "settlement_fact", "classification = 'NO'"
    )
    op.drop_column("settlement_fact", "void_term_content_hash")
    op.drop_constraint(
        "ck_opportunity_proposal_void_rule_term", "opportunity_proposal", type_="check"
    )
    op.drop_column("opportunity_proposal", "void_term_content_hash")
    op.drop_column("opportunity_proposal", "void_term_settlement_value_text")
    op.drop_column("opportunity_proposal", "void_term_settlement_value")
    op.drop_column("opportunity_proposal", "void_term_outcome_classification")
    op.execute(sa.text(NO_POSITION_SETTLEMENT_GUARD_DDL))
    op.execute(sa.text(NO_POSITION_GUARD_DDL))
