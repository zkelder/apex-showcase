"""Persist the bounded, shadow-only Apex Autopilot control plane.

Revision ID: 0014_autopilot_core
Revises: 0013_threshold_ladder
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    AUTOPILOT_IMMUTABLE_TABLES,
    AUTOPILOT_TABLES,
    autopilot_metadata,
    immutable_trigger_ddl,
)

revision: str = "0014_autopilot_core"
down_revision: str | None = "0013_threshold_ladder"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


AUTOPILOT_CYCLE_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_autopilot_cycle() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'autopilot cycle cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'completed_at', 'state', 'invocation_count', 'assessment_count',
        'approved_count', 'rejected_count', 'result_hash', 'last_error_code'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'completed_at', 'state', 'invocation_count', 'assessment_count',
        'approved_count', 'rejected_count', 'result_hash', 'last_error_code'
    ]) THEN
        RAISE EXCEPTION 'autopilot cycle configuration is immutable';
    END IF;
    IF OLD.state <> 'RUNNING' THEN
        RAISE EXCEPTION 'handled autopilot cycle is immutable';
    END IF;
    IF NEW.state IN ('COMPLETED', 'PARTIAL') THEN
        IF NEW.completed_at IS NULL OR NEW.result_hash IS NULL
           OR NEW.approved_count + NEW.rejected_count <> NEW.assessment_count THEN
            RAISE EXCEPTION 'completed autopilot cycle requires a complete result';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.state IN ('FAILED', 'INTERRUPTED') THEN
        IF NEW.completed_at IS NULL OR NEW.result_hash IS NOT NULL
           OR NEW.last_error_code IS NULL THEN
            RAISE EXCEPTION 'failed autopilot cycle requires a handled error';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'autopilot cycle transition is invalid';
END;
$$;

CREATE TRIGGER apex_guard_autopilot_cycle_trigger
BEFORE UPDATE OR DELETE ON autopilot_cycle
FOR EACH ROW EXECUTE FUNCTION apex_guard_autopilot_cycle();
"""


def upgrade() -> None:
    """Add only non-financial Autopilot governance and shadow evidence."""

    autopilot_metadata.create_all(bind=op.get_bind(), tables=AUTOPILOT_TABLES, checkfirst=False)
    for table_name in AUTOPILOT_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(AUTOPILOT_CYCLE_GUARD_DDL))


def downgrade() -> None:
    """Refuse before DDL if retained Autopilot evidence would be discarded."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM campaign_operating_state) "
            "OR EXISTS (SELECT 1 FROM campaign_control_event) "
            "OR EXISTS (SELECT 1 FROM campaign_portfolio_policy) "
            "OR EXISTS (SELECT 1 FROM strategy_governance) "
            "OR EXISTS (SELECT 1 FROM autopilot_cycle) "
            "OR EXISTS (SELECT 1 FROM autopilot_strategy_invocation) "
            "OR EXISTS (SELECT 1 FROM opportunity_economics) "
            "OR EXISTS (SELECT 1 FROM autopilot_opportunity_assessment) "
            "OR EXISTS (SELECT 1 FROM shadow_portfolio_decision) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0014 with retained Autopilot shadow evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(sa.text("DROP TRIGGER apex_guard_autopilot_cycle_trigger ON autopilot_cycle"))
    op.execute(sa.text("DROP FUNCTION apex_guard_autopilot_cycle()"))
    for table_name in AUTOPILOT_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    autopilot_metadata.drop_all(bind=op.get_bind(), tables=AUTOPILOT_TABLES, checkfirst=False)
