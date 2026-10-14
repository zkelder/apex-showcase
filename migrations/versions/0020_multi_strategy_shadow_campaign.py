"""Persist shared-cohort multi-strategy SHADOW campaign evidence.

Revision ID: 0020_multi_strategy_shadow
Revises: 0019_market_dynamics
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    MULTI_STRATEGY_IMMUTABLE_TABLES,
    MULTI_STRATEGY_TABLES,
    immutable_trigger_ddl,
    multi_strategy_metadata,
)

revision: str = "0020_multi_strategy_shadow"
down_revision: str | None = "0019_market_dynamics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MULTI_STRATEGY_CYCLE_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_multi_strategy_shadow_cycle() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'multi-strategy shadow cycle cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'completed_at', 'state', 'cohort_id', 'selected_market_count',
        'invocation_count', 'prediction_count', 'opportunity_count', 'conflict_count',
        'hypothetical_admission_count', 'hypothetical_rejection_count',
        'requested_risk_total', 'allocated_risk_total', 'remaining_shadow_capacity',
        'canonical_result_hash', 'last_error_code'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'completed_at', 'state', 'cohort_id', 'selected_market_count',
        'invocation_count', 'prediction_count', 'opportunity_count', 'conflict_count',
        'hypothetical_admission_count', 'hypothetical_rejection_count',
        'requested_risk_total', 'allocated_risk_total', 'remaining_shadow_capacity',
        'canonical_result_hash', 'last_error_code'
    ]) THEN
        RAISE EXCEPTION 'multi-strategy shadow cycle configuration is immutable';
    END IF;
    IF OLD.state <> 'RUNNING' THEN
        RAISE EXCEPTION 'handled multi-strategy shadow cycle is immutable';
    END IF;
    IF NEW.state IN ('COMPLETED', 'PARTIAL') THEN
        IF NEW.completed_at IS NULL OR NEW.canonical_result_hash IS NULL
           OR NEW.cohort_id IS NULL THEN
            RAISE EXCEPTION 'completed multi-strategy shadow cycle requires retained results';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.state IN ('FAILED', 'INTERRUPTED') THEN
        IF NEW.completed_at IS NULL OR NEW.last_error_code IS NULL THEN
            RAISE EXCEPTION 'failed multi-strategy shadow cycle requires a handled error';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'multi-strategy shadow cycle transition is invalid';
END;
$$;

CREATE TRIGGER apex_guard_multi_strategy_shadow_cycle_trigger
BEFORE UPDATE OR DELETE ON multi_strategy_shadow_cycle
FOR EACH ROW EXECUTE FUNCTION apex_guard_multi_strategy_shadow_cycle();
"""

MULTI_STRATEGY_SESSION_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_multi_strategy_shadow_session() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'multi-strategy shadow session cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'finalized_at', 'state', 'completed_iterations', 'failed_iterations',
        'last_completed_stage', 'interrupted_at', 'interruption_reason',
        'canonical_result_hash', 'last_error_code'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'finalized_at', 'state', 'completed_iterations', 'failed_iterations',
        'last_completed_stage', 'interrupted_at', 'interruption_reason',
        'canonical_result_hash', 'last_error_code'
    ]) THEN
        RAISE EXCEPTION 'multi-strategy shadow session configuration is immutable';
    END IF;
    IF OLD.state <> 'RUNNING' THEN
        RAISE EXCEPTION 'handled multi-strategy shadow session is immutable';
    END IF;
    IF NEW.state IN ('COMPLETED', 'PARTIAL') THEN
        IF NEW.finalized_at IS NULL OR NEW.canonical_result_hash IS NULL THEN
            RAISE EXCEPTION 'completed multi-strategy shadow session requires a retained result';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.state IN ('FAILED', 'INTERRUPTED') THEN
        IF NEW.finalized_at IS NULL OR NEW.last_error_code IS NULL THEN
            RAISE EXCEPTION 'interrupted multi-strategy shadow session requires a reason';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'multi-strategy shadow session transition is invalid';
END;
$$;

CREATE TRIGGER apex_guard_multi_strategy_shadow_session_trigger
BEFORE UPDATE OR DELETE ON multi_strategy_shadow_session
FOR EACH ROW EXECUTE FUNCTION apex_guard_multi_strategy_shadow_session();
"""


def upgrade() -> None:
    """Add only immutable SHADOW comparison and hypothetical-plan evidence."""

    multi_strategy_metadata.create_all(
        bind=op.get_bind(), tables=MULTI_STRATEGY_TABLES, checkfirst=False
    )
    for table_name in MULTI_STRATEGY_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(MULTI_STRATEGY_CYCLE_GUARD_DDL))
    op.execute(sa.text(MULTI_STRATEGY_SESSION_GUARD_DDL))


def downgrade() -> None:
    """Refuse before DDL whenever retained 0020 evidence would be discarded."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM multi_strategy_shadow_cycle) "
            "OR EXISTS (SELECT 1 FROM multi_strategy_shadow_session) "
            "OR EXISTS (SELECT 1 FROM multi_strategy_shadow_session_iteration) "
            "OR EXISTS (SELECT 1 FROM multi_strategy_shadow_cohort_member) "
            "OR EXISTS (SELECT 1 FROM cross_strategy_prediction_comparison) "
            "OR EXISTS (SELECT 1 FROM strategy_conflict) "
            "OR EXISTS (SELECT 1 FROM shadow_capital_allocation) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0020 with retained multi-strategy shadow evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_multi_strategy_shadow_cycle_trigger "
            "ON multi_strategy_shadow_cycle"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_multi_strategy_shadow_cycle()"))
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_multi_strategy_shadow_session_trigger "
            "ON multi_strategy_shadow_session"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_multi_strategy_shadow_session()"))
    for table_name in MULTI_STRATEGY_IMMUTABLE_TABLES:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF to_regclass('{table_name}') IS NOT NULL THEN "
                f"DROP TRIGGER IF EXISTS apex_immutable_{table_name}_trigger ON {table_name}; "
                "END IF; END $$"
            )
        )
    multi_strategy_metadata.drop_all(bind=op.get_bind(), tables=MULTI_STRATEGY_TABLES)
