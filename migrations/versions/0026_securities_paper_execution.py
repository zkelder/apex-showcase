"""Persist governed Securities PAPER execution and accounting evidence.

Revision ID: 0026_securities_paper_execution
Revises: 0025_securities_research_shadow
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    SECURITIES_PAPER_IMMUTABLE_TABLES,
    SECURITIES_PAPER_TABLES,
    immutable_trigger_ddl,
    securities_paper_metadata,
)

revision: str = "0026_securities_paper_execution"
down_revision: str | None = "0025_securities_research_shadow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FAMILY_CHECK_0025 = (
    "family IN ('EXTERNAL_PREDICTIVE', 'MARKET_STRUCTURE', 'MARKET_DYNAMICS', "
    "'CROSS_MARKET', 'OPERATOR_SUPPLIED', 'FIXTURE_ONLY')"
)
_FAMILY_CHECK_0026 = (
    "family IN ('EXTERNAL_PREDICTIVE', 'MARKET_STRUCTURE', 'MARKET_DYNAMICS', "
    "'CROSS_MARKET', 'OPERATOR_SUPPLIED', 'FIXTURE_ONLY', 'SECURITIES')"
)

_RUN_GUARD_DDL = """
CREATE OR REPLACE FUNCTION apex_guard_securities_paper_run() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Securities PAPER run evidence is immutable';
    END IF;
    IF ROW(
        NEW.run_id, NEW.campaign_id, NEW.trial_authorization_id,
        NEW.strategy_id, NEW.strategy_version, NEW.strategy_configuration_hash,
        NEW.data_batch_id, NEW.data_batch_hash, NEW.execution_policy_id,
        NEW.execution_policy_hash, NEW.start_date, NEW.end_date,
        NEW.starting_cash, NEW.idempotency_key, NEW.started_at,
        NEW.canonical_input_hash
    ) IS DISTINCT FROM ROW(
        OLD.run_id, OLD.campaign_id, OLD.trial_authorization_id,
        OLD.strategy_id, OLD.strategy_version, OLD.strategy_configuration_hash,
        OLD.data_batch_id, OLD.data_batch_hash, OLD.execution_policy_id,
        OLD.execution_policy_hash, OLD.start_date, OLD.end_date,
        OLD.starting_cash, OLD.idempotency_key, OLD.started_at,
        OLD.canonical_input_hash
    ) THEN
        RAISE EXCEPTION 'Securities PAPER run identity cannot change';
    END IF;
    IF NEW.signal_count < OLD.signal_count
       OR NEW.opportunity_count < OLD.opportunity_count
       OR NEW.accepted_opportunity_count < OLD.accepted_opportunity_count
       OR NEW.rejected_opportunity_count < OLD.rejected_opportunity_count
       OR NEW.order_count < OLD.order_count
       OR NEW.fill_count < OLD.fill_count
       OR NEW.position_count < OLD.position_count
       OR NEW.closed_position_count < OLD.closed_position_count
       OR NEW.mark_count < OLD.mark_count
       OR NEW.corporate_action_application_count
          < OLD.corporate_action_application_count THEN
        RAISE EXCEPTION 'Securities PAPER run counters cannot decrease';
    END IF;
    IF OLD.state IN ('COMPLETED', 'FAILED', 'CANCELED')
       AND NEW.state IS DISTINCT FROM OLD.state THEN
        RAISE EXCEPTION 'terminal Securities PAPER run cannot transition';
    END IF;
    IF OLD.state = 'RUNNING'
       AND NEW.state NOT IN ('RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELED') THEN
        RAISE EXCEPTION 'invalid Securities PAPER run transition';
    END IF;
    IF NEW.state IN ('COMPLETED', 'PARTIAL', 'FAILED', 'CANCELED')
       AND (NEW.completed_at IS NULL OR NEW.canonical_result_hash IS NULL) THEN
        RAISE EXCEPTION 'terminal Securities PAPER run requires completion evidence';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER apex_guard_securities_paper_run_trigger
BEFORE UPDATE OR DELETE ON securities_paper_run
FOR EACH ROW EXECUTE FUNCTION apex_guard_securities_paper_run();
"""

_POSITION_GUARD_DDL = """
CREATE OR REPLACE FUNCTION apex_guard_securities_position_state() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Securities Position state cannot be deleted';
    END IF;
    IF ROW(
        NEW.position_id, NEW.run_id, NEW.campaign_id, NEW.instrument_id,
        NEW.quantity_increment, NEW.cumulative_purchase_cost,
        NEW.originating_fill_id
    ) IS DISTINCT FROM ROW(
        OLD.position_id, OLD.run_id, OLD.campaign_id, OLD.instrument_id,
        OLD.quantity_increment, OLD.cumulative_purchase_cost,
        OLD.originating_fill_id
    ) THEN
        RAISE EXCEPTION 'Securities Position identity or original cost cannot change';
    END IF;
    IF NEW.row_version <> OLD.row_version + 1 THEN
        RAISE EXCEPTION 'Securities Position row version must advance exactly once';
    END IF;
    IF OLD.state = 'CLOSED' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'closed Securities Position cannot change';
    END IF;
    IF NEW.open_quantity > OLD.open_quantity
       AND NOT EXISTS (
           SELECT 1
           FROM securities_corporate_action_application application
           JOIN securities_corporate_action_evidence evidence
             ON evidence.action_id = application.action_id
           WHERE application.position_id = OLD.position_id
             AND application.state = 'APPLIED'
             AND evidence.action_type = 'SPLIT'
             AND application.prior_quantity = OLD.open_quantity
             AND application.resulting_quantity = NEW.open_quantity
       ) THEN
        RAISE EXCEPTION 'Securities Position increase requires split evidence';
    END IF;
    IF NEW.current_symbol IS DISTINCT FROM OLD.current_symbol
       AND NOT EXISTS (
           SELECT 1
           FROM securities_corporate_action_application application
           JOIN securities_corporate_action_evidence evidence
             ON evidence.action_id = application.action_id
           WHERE application.position_id = OLD.position_id
             AND application.state = 'APPLIED'
             AND evidence.action_type = 'SYMBOL_CHANGE'
       ) THEN
        RAISE EXCEPTION 'Securities Position symbol change requires action evidence';
    END IF;
    IF NEW.last_mark_at IS DISTINCT FROM OLD.last_mark_at
       AND NEW.last_mark_at IS NOT NULL
       AND OLD.last_mark_at IS NOT NULL
       AND NEW.last_mark_at <= OLD.last_mark_at THEN
        RAISE EXCEPTION 'Securities Position marks must advance in time';
    END IF;
    IF NEW.state = 'CLOSED' AND NEW.open_quantity <> 0 THEN
        RAISE EXCEPTION 'closed Securities Position must have zero quantity';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER apex_guard_securities_position_state_trigger
BEFORE UPDATE OR DELETE ON securities_position_state
FOR EACH ROW EXECUTE FUNCTION apex_guard_securities_position_state();
"""


def upgrade() -> None:
    op.drop_constraint(
        "ck_strategy_governance_family",
        "strategy_governance",
        type_="check",
    )
    op.create_check_constraint(
        "ck_strategy_governance_family",
        "strategy_governance",
        _FAMILY_CHECK_0026,
    )
    securities_paper_metadata.create_all(
        bind=op.get_bind(),
        tables=SECURITIES_PAPER_TABLES,
        checkfirst=False,
    )
    for table_name in SECURITIES_PAPER_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(_RUN_GUARD_DDL))
    op.execute(sa.text(_POSITION_GUARD_DDL))


def downgrade() -> None:
    """Refuse before DDL whenever retained Securities PAPER evidence exists."""

    checks = " OR ".join(
        f'EXISTS (SELECT 1 FROM "{table.name}")' for table in SECURITIES_PAPER_TABLES
    )
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0026 with retained Securities PAPER execution evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_securities_position_state_trigger ON securities_position_state"
        )
    )
    op.execute(
        sa.text("DROP TRIGGER apex_guard_securities_paper_run_trigger ON securities_paper_run")
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_securities_position_state()"))
    op.execute(sa.text("DROP FUNCTION apex_guard_securities_paper_run()"))
    for table_name in SECURITIES_PAPER_IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table_name}_trigger ON "{table_name}"'))
    securities_paper_metadata.drop_all(
        bind=op.get_bind(),
        tables=SECURITIES_PAPER_TABLES,
    )
    op.drop_constraint(
        "ck_strategy_governance_family",
        "strategy_governance",
        type_="check",
    )
    op.create_check_constraint(
        "ck_strategy_governance_family",
        "strategy_governance",
        _FAMILY_CHECK_0025,
    )
