"""Persist bounded PAPER portfolio evidence and full-fill lifecycle.

Revision ID: 0015_paper_portfolio
Revises: 0014_autopilot_core
"""

# The embedded PL/pgSQL is intentionally kept legible as database code.  Ruff
# cannot wrap its string contents without changing the migration payload.
# ruff: noqa: E501

from collections.abc import Sequence
from importlib import import_module

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import PAPER_IMMUTABLE_TABLES, PAPER_TABLES, immutable_trigger_ddl

revision: str = "0015_paper_portfolio"
down_revision: str | None = "0014_autopilot_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PAPER_ORDER_GUARD = r"""
CREATE OR REPLACE FUNCTION apex_guard_order_execution_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE e record; loss numeric(38,18); auth_at timestamptz;
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'durable order cannot be deleted'; END IF;
  IF (to_jsonb(OLD) - ARRAY['filled_quantity','reserved_maximum_loss','state','last_execution_sequence','row_version','execution_evidence','canonical_payload_hash'])
     <> (to_jsonb(NEW) - ARRAY['filled_quantity','reserved_maximum_loss','state','last_execution_sequence','row_version','execution_evidence','canonical_payload_hash'])
  THEN RAISE EXCEPTION 'immutable order terms cannot change'; END IF;
  IF OLD.state = 'READY' AND NEW.state = 'READY' AND OLD.execution_evidence = 'SYNTHETIC' AND NEW.execution_evidence = 'PAPER'
     AND OLD.last_execution_sequence = NEW.last_execution_sequence AND OLD.row_version = NEW.row_version
  THEN RETURN NEW; END IF;
  SELECT event_type, occurred_at, fill_quantity, fill_price INTO e FROM execution_event
    WHERE order_id = NEW.order_id AND sequence = NEW.last_execution_sequence;
  SELECT maximum_loss_per_unit, authorization_at INTO loss, auth_at FROM opportunity_proposal p
    JOIN risk_decision d ON d.risk_decision_id = NEW.risk_decision_id WHERE p.proposal_id = NEW.proposal_id;
  IF e.event_type IS NULL THEN RAISE EXCEPTION 'order transition requires immutable execution event'; END IF;
  IF NEW.state = 'OPEN' THEN
    IF NOT (OLD.state='READY' AND NEW.last_execution_sequence=1 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity=0 AND NEW.reserved_maximum_loss=OLD.reserved_maximum_loss
      AND e.event_type='OPENED' AND e.occurred_at >= auth_at) THEN RAISE EXCEPTION 'invalid durable order open'; END IF;
  ELSIF NEW.state = 'EXPIRED' THEN
    IF NOT (OLD.state='OPEN' AND NEW.last_execution_sequence=2 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity=0 AND NEW.reserved_maximum_loss=0 AND e.event_type='EXPIRED') THEN RAISE EXCEPTION 'invalid durable order expiration'; END IF;
  ELSIF NEW.state = 'PARTIALLY_FILLED' THEN
    IF NOT (OLD.state='OPEN' AND NEW.last_execution_sequence=2 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity>0 AND NEW.filled_quantity<NEW.quantity AND NEW.reserved_maximum_loss=loss*(NEW.quantity-NEW.filled_quantity)
      AND e.event_type='FILL' AND e.fill_quantity=NEW.filled_quantity AND e.fill_price<=NEW.limit_price) THEN RAISE EXCEPTION 'invalid durable partial fill'; END IF;
  ELSIF NEW.state = 'CANCELED' THEN
    IF NOT (OLD.state='PARTIALLY_FILLED' AND NEW.last_execution_sequence=3 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity=OLD.filled_quantity AND NEW.reserved_maximum_loss=0 AND e.event_type='CANCELED') THEN RAISE EXCEPTION 'invalid durable cancellation'; END IF;
  ELSIF NEW.state = 'FILLED' THEN
    IF NOT (OLD.state='OPEN' AND NEW.last_execution_sequence=2 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity=NEW.quantity AND NEW.reserved_maximum_loss=0 AND e.event_type='FILL'
      AND e.fill_quantity=NEW.quantity AND e.fill_price<=NEW.limit_price) THEN RAISE EXCEPTION 'invalid durable full fill'; END IF;
  ELSE RAISE EXCEPTION 'invalid durable order transition'; END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER apex_guard_order_execution_transition_trigger BEFORE UPDATE OR DELETE ON "order"
FOR EACH ROW EXECUTE FUNCTION apex_guard_order_execution_transition();
"""


PAPER_RESERVATION_GUARD = r"""
CREATE OR REPLACE FUNCTION apex_guard_reservation_execution_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE s text; t text; at timestamptz; reserved numeric(38,18);
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'durable reservation cannot be deleted'; END IF;
  IF (to_jsonb(OLD)-ARRAY['current_amount','state','released_at','canonical_payload_hash'])
     <> (to_jsonb(NEW)-ARRAY['current_amount','state','released_at','canonical_payload_hash']) THEN RAISE EXCEPTION 'immutable reservation terms cannot change'; END IF;
  SELECT o.state,o.reserved_maximum_loss,e.event_type,e.occurred_at INTO s,reserved,t,at FROM "order" o
    JOIN execution_event e ON e.order_id=o.order_id AND e.sequence=o.last_execution_sequence WHERE o.order_id=NEW.order_id;
  IF t IS NULL THEN RAISE EXCEPTION 'reservation transition requires order event'; END IF;
  IF OLD.state='HELD' AND NEW.state='OPEN' AND NEW.current_amount=OLD.current_amount AND NEW.released_at IS NULL AND s='OPEN' AND t='OPENED' THEN RETURN NEW; END IF;
  IF OLD.state='OPEN' AND NEW.state='OPEN' AND NEW.current_amount>0 AND NEW.current_amount<OLD.current_amount AND NEW.current_amount=reserved AND s='PARTIALLY_FILLED' AND t='FILL' THEN RETURN NEW; END IF;
  IF OLD.state='OPEN' AND NEW.state='RELEASED' AND NEW.current_amount=0 AND NEW.released_at=at AND s IN ('EXPIRED','CANCELED','FILLED') AND t IN ('EXPIRED','CANCELED','FILL') THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'invalid durable reservation transition';
END; $$;
CREATE TRIGGER apex_guard_reservation_execution_transition_trigger BEFORE UPDATE OR DELETE ON risk_reservation
FOR EACH ROW EXECUTE FUNCTION apex_guard_reservation_execution_transition();
"""


PAPER_POSITION_GUARD = r"""
CREATE OR REPLACE FUNCTION apex_guard_position_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE e record; o record; loss numeric(38,18); inst text; rule text;
BEGIN
 SELECT event_type,campaign_id,order_id,sequence,fill_quantity,fill_price INTO e FROM execution_event WHERE event_id=NEW.originating_fill_event_id;
 SELECT * INTO o FROM "order" WHERE order_id=NEW.originating_order_id;
 SELECT maximum_loss_per_unit,instrument_id,rule_version_id INTO loss,inst,rule FROM opportunity_proposal WHERE proposal_id=o.proposal_id;
 IF e.event_type IS NULL OR e.event_type <> 'FILL' OR e.campaign_id <> NEW.campaign_id OR e.order_id <> NEW.originating_order_id
    OR o.state NOT IN ('PARTIALLY_FILLED','FILLED') OR o.last_execution_sequence <> e.sequence
    OR o.filled_quantity <> e.fill_quantity OR NEW.open_quantity <> e.fill_quantity OR NEW.cost_basis <> e.fill_quantity*e.fill_price
    OR NEW.maximum_remaining_loss <> e.fill_quantity*loss OR NEW.instrument_id <> inst OR NEW.rule_version_id <> rule OR NEW.side <> o.side
    OR NEW.settlement_state <> 'OPEN' THEN RAISE EXCEPTION 'position requires exact durable fill'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER apex_guard_position_insert_trigger BEFORE INSERT ON position FOR EACH ROW EXECUTE FUNCTION apex_guard_position_insert();
"""


_legacy_void_migration = import_module("migrations.versions.0005_durable_void_settlement")
_legacy_position_settlement_guard = _legacy_void_migration.VOID_POSITION_SETTLEMENT_GUARD_DDL
_legacy_execution_migration = import_module("migrations.versions.0003_partial_fill_and_position")
_paper_position_settlement_prefix = r"""
BEGIN
    SELECT * INTO durable_position FROM position WHERE position_id = NEW.position_id;
    IF EXISTS (
        SELECT 1 FROM paper_execution
         WHERE order_id = durable_position.originating_order_id
    ) THEN
        SELECT * INTO durable_order FROM "order"
          WHERE order_id = durable_position.originating_order_id;
        SELECT * INTO durable_reservation FROM risk_reservation
          WHERE order_id = durable_order.order_id;
        SELECT * INTO durable_proposal FROM opportunity_proposal
          WHERE proposal_id = durable_order.proposal_id;
        SELECT * INTO durable_fact FROM settlement_fact
          WHERE settlement_fact_id = NEW.settlement_fact_id;
        SELECT * INTO durable_journal FROM ledger_journal
          WHERE journal_id = NEW.settlement_journal_id;
        SELECT occurred_at INTO canceled_at FROM execution_event
          WHERE order_id = durable_order.order_id
            AND event_type = 'FILL' AND sequence = 2;
        IF durable_position.position_id IS NULL
           OR durable_order.order_id IS NULL
           OR durable_reservation.reservation_id IS NULL
           OR durable_proposal.proposal_id IS NULL
           OR durable_fact.settlement_fact_id IS NULL
           OR durable_journal.journal_id IS NULL
           OR durable_position.campaign_id <> NEW.campaign_id
           OR durable_order.campaign_id <> NEW.campaign_id
           OR durable_fact.campaign_id <> NEW.campaign_id
           OR durable_order.state NOT IN ('CANCELED', 'FILLED')
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
           OR (durable_fact.classification = 'YES' AND (
                 (durable_position.side = 'YES' AND durable_fact.settlement_value <> 1)
                 OR (durable_position.side = 'NO' AND durable_fact.settlement_value <> 0)
              ))
           OR (durable_fact.classification = 'NO' AND (
                 (durable_position.side = 'YES' AND durable_fact.settlement_value <> 0)
                 OR (durable_position.side = 'NO' AND durable_fact.settlement_value <> 1)
              ))
           OR (durable_fact.classification = 'VOID' AND (
                 durable_fact.void_term_content_hash IS NULL
                 OR durable_fact.void_term_content_hash <> durable_proposal.void_term_content_hash
                 OR durable_fact.settlement_value <> durable_proposal.void_term_settlement_value
                 OR durable_fact.settlement_value_text
                    <> durable_proposal.void_term_settlement_value_text
              ))
           OR durable_fact.classification NOT IN ('YES', 'NO', 'VOID')
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
            RAISE EXCEPTION 'paper position settlement requires exact fill-bound evidence';
        END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO durable_position FROM position WHERE position_id = NEW.position_id;
"""
PAPER_POSITION_SETTLEMENT_GUARD = _legacy_position_settlement_guard.replace(
    "BEGIN\n    SELECT * INTO durable_position FROM position WHERE position_id = NEW.position_id;",
    _paper_position_settlement_prefix,
    1,
)


def upgrade() -> None:
    op.drop_constraint("ck_settlement_fact_classification", "settlement_fact", type_="check")
    op.drop_constraint("ck_settlement_fact_void_authority", "settlement_fact", type_="check")
    op.create_check_constraint(
        "ck_settlement_fact_classification",
        "settlement_fact",
        "classification IN ('YES', 'NO', 'VOID')",
    )
    op.create_check_constraint(
        "ck_settlement_fact_void_authority",
        "settlement_fact",
        "(classification IN ('YES', 'NO') AND void_term_content_hash IS NULL) OR "
        "(classification = 'VOID' AND settlement_value > 0 "
        "AND length(void_term_content_hash) = 64)",
    )
    op.drop_constraint("order_execution_evidence_check", "order", type_="check")
    op.create_check_constraint(
        "ck_order_execution_evidence", "order", "execution_evidence IN ('SYNTHETIC', 'PAPER')"
    )
    op.drop_constraint("ck_order_durable_lifecycle", "order", type_="check")
    op.create_check_constraint(
        "ck_order_durable_lifecycle",
        "order",
        "(state='READY' AND last_execution_sequence=0 AND row_version=1 AND filled_quantity=0 AND reserved_maximum_loss>0) OR (state='OPEN' AND last_execution_sequence=1 AND row_version=2 AND filled_quantity=0 AND reserved_maximum_loss>0) OR (state='EXPIRED' AND last_execution_sequence=2 AND row_version=3 AND filled_quantity=0 AND reserved_maximum_loss=0) OR (state='PARTIALLY_FILLED' AND last_execution_sequence=2 AND row_version=3 AND filled_quantity>0 AND filled_quantity<quantity AND reserved_maximum_loss>0) OR (state='CANCELED' AND last_execution_sequence=3 AND row_version=4 AND filled_quantity>0 AND filled_quantity<quantity AND reserved_maximum_loss=0) OR (state='FILLED' AND last_execution_sequence=2 AND row_version=3 AND filled_quantity=quantity AND reserved_maximum_loss=0)",
    )
    op.execute(
        sa.text('DROP TRIGGER IF EXISTS apex_guard_order_execution_transition_trigger ON "order"')
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS apex_guard_reservation_execution_transition_trigger "
            "ON risk_reservation"
        )
    )
    op.execute(sa.text("DROP TRIGGER IF EXISTS apex_guard_position_insert_trigger ON position"))
    op.execute(sa.text(PAPER_ORDER_GUARD))
    op.execute(sa.text(PAPER_RESERVATION_GUARD))
    op.execute(sa.text(PAPER_POSITION_GUARD))
    op.execute(sa.text(PAPER_POSITION_SETTLEMENT_GUARD))
    from apex.adapters.postgres import paper_metadata

    paper_metadata.create_all(bind=op.get_bind(), tables=PAPER_TABLES, checkfirst=False)
    for table_name in PAPER_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ DECLARE table_name text; retained boolean; BEGIN "
            "FOREACH table_name IN ARRAY ARRAY["
            "'paper_cycle_authority', 'paper_execution_policy', "
            "'paper_execution_evidence', 'paper_execution_level', "
            "'paper_execution', 'paper_position_lineage', 'paper_position_mark'] "
            "LOOP IF to_regclass(table_name) IS NOT NULL THEN "
            "EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I)', table_name) INTO retained; "
            "IF retained THEN RAISE EXCEPTION "
            "'cannot downgrade 0015 with retained paper portfolio evidence'; END IF; "
            "END IF; END LOOP; "
            "IF EXISTS (SELECT 1 FROM \"order\" WHERE execution_evidence='PAPER') "
            "THEN RAISE EXCEPTION 'cannot downgrade 0015 with retained paper portfolio evidence'; "
            "END IF; END $$"
        )
    )
    for table_name in PAPER_IMMUTABLE_TABLES:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF to_regclass('{table_name}') IS NOT NULL THEN "
                f"EXECUTE 'DROP TRIGGER IF EXISTS apex_immutable_{table_name}_trigger "
                f"ON {table_name}'; "
                "END IF; END $$"
            )
        )
    from apex.adapters.postgres import paper_metadata

    paper_metadata.drop_all(bind=op.get_bind(), tables=PAPER_TABLES, checkfirst=True)
    op.execute(sa.text("ALTER TABLE autopilot_cycle DROP COLUMN IF EXISTS authority"))
    op.execute(sa.text('DROP TRIGGER apex_guard_order_execution_transition_trigger ON "order"'))
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_reservation_execution_transition_trigger ON risk_reservation"
        )
    )
    op.execute(sa.text("DROP TRIGGER apex_guard_position_insert_trigger ON position"))
    op.execute(sa.text(_legacy_execution_migration.ORDER_GUARD_DDL))
    op.execute(sa.text(_legacy_execution_migration.RESERVATION_GUARD_DDL))
    op.execute(sa.text(_legacy_execution_migration.POSITION_GUARD_DDL))
    op.execute(
        sa.text(
            "CREATE TRIGGER apex_guard_order_execution_transition_trigger "
            'BEFORE UPDATE OR DELETE ON "order" FOR EACH ROW '
            "EXECUTE FUNCTION apex_guard_order_execution_transition()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER apex_guard_reservation_execution_transition_trigger "
            "BEFORE UPDATE OR DELETE ON risk_reservation FOR EACH ROW "
            "EXECUTE FUNCTION apex_guard_reservation_execution_transition()"
        )
    )
    op.drop_constraint("ck_settlement_fact_void_authority", "settlement_fact", type_="check")
    op.drop_constraint("ck_settlement_fact_classification", "settlement_fact", type_="check")
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
    op.execute(sa.text(_legacy_position_settlement_guard))
    op.drop_constraint("ck_order_execution_evidence", "order", type_="check")
    op.create_check_constraint(
        "order_execution_evidence_check", "order", "execution_evidence = 'SYNTHETIC'"
    )
    op.drop_constraint("ck_order_durable_lifecycle", "order", type_="check")
    op.create_check_constraint(
        "ck_order_durable_lifecycle",
        "order",
        "(state = 'READY' AND last_execution_sequence = 0 AND row_version = 1 AND filled_quantity = 0 AND reserved_maximum_loss > 0) OR (state = 'OPEN' AND last_execution_sequence = 1 AND row_version = 2 AND executable_ask IS NOT NULL AND expires_at IS NOT NULL AND filled_quantity = 0 AND reserved_maximum_loss > 0) OR (state = 'EXPIRED' AND last_execution_sequence = 2 AND row_version = 3 AND executable_ask IS NOT NULL AND expires_at IS NOT NULL AND filled_quantity = 0 AND reserved_maximum_loss = 0) OR (state = 'PARTIALLY_FILLED' AND last_execution_sequence = 2 AND row_version = 3 AND executable_ask IS NOT NULL AND expires_at IS NOT NULL AND filled_quantity > 0 AND filled_quantity < quantity AND reserved_maximum_loss > 0) OR (state = 'CANCELED' AND last_execution_sequence = 3 AND row_version = 4 AND executable_ask IS NOT NULL AND expires_at IS NOT NULL AND filled_quantity > 0 AND filled_quantity < quantity AND reserved_maximum_loss = 0)",
    )
