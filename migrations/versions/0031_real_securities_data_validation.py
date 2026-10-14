"""Persist bounded real Securities provider and validation evidence.

Revision ID: 0031_real_securities_data
Revises: 0030_batch_learning
"""

from collections.abc import Sequence

# ruff: noqa: E501
import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    REAL_SECURITIES_DATA_IMMUTABLE_TABLES,
    REAL_SECURITIES_DATA_TABLES,
    immutable_trigger_ddl,
    real_securities_data_metadata,
)

revision: str = "0031_real_securities_data"
down_revision: str | None = "0030_batch_learning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FETCH_GUARD_DDL = r"""
CREATE FUNCTION apex_guard_securities_provider_fetch_operation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'provider fetch operation cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'request_count','page_count','raw_byte_count','received_record_count',
        'completed_at','state','retry_count','rate_limit_evidence',
        'provider_request_ids','raw_artifact_relative_path','raw_artifact_set_hash',
        'normalized_batch_id','normalized_batch_hash','failure_code','canonical_result_hash'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'request_count','page_count','raw_byte_count','received_record_count',
        'completed_at','state','retry_count','rate_limit_evidence',
        'provider_request_ids','raw_artifact_relative_path','raw_artifact_set_hash',
        'normalized_batch_id','normalized_batch_hash','failure_code','canonical_result_hash'
    ]) THEN
        RAISE EXCEPTION 'provider fetch operation inputs are immutable';
    END IF;
    IF OLD.state = 'REQUESTED' AND NEW.state IN ('FETCHING','CREDENTIALS_REQUIRED','FAILED','CANCELED') THEN RETURN NEW; END IF;
    IF OLD.state = 'FETCHING' AND NEW.state IN ('COMPLETED','PARTIAL','FAILED','REJECTED_INTEGRITY','CANCELED') THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'provider fetch operation transition is invalid';
END;
$$;
CREATE TRIGGER apex_guard_securities_provider_fetch_operation_trigger
BEFORE UPDATE OR DELETE ON securities_provider_fetch_operation
FOR EACH ROW EXECUTE FUNCTION apex_guard_securities_provider_fetch_operation();
"""

VALIDATION_GUARD_DDL = r"""
CREATE FUNCTION apex_guard_securities_historical_validation_run() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'historical validation run cannot be deleted'; END IF;
    IF (to_jsonb(OLD) - ARRAY['state','baseline_run_ids','scorecard_ids','simulator_conformance_ids','dataset_id','dataset_hash','experiment_id','experiment_hash','candidate_search_run_id','multiple_testing_evaluation_id','recommendation','reason_codes','completed_at','canonical_result_hash'])
       <> (to_jsonb(NEW) - ARRAY['state','baseline_run_ids','scorecard_ids','simulator_conformance_ids','dataset_id','dataset_hash','experiment_id','experiment_hash','candidate_search_run_id','multiple_testing_evaluation_id','recommendation','reason_codes','completed_at','canonical_result_hash']) THEN
        RAISE EXCEPTION 'historical validation inputs are immutable';
    END IF;
    IF OLD.state = 'REQUESTED' AND NEW.state IN ('RUNNING','FAILED','BLOCKED_INTEGRITY') THEN RETURN NEW; END IF;
    IF OLD.state = 'RUNNING' AND NEW.state IN ('COMPLETED','FAILED','BLOCKED_INTEGRITY') THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'historical validation transition is invalid';
END;
$$;
CREATE TRIGGER apex_guard_securities_historical_validation_run_trigger
BEFORE UPDATE OR DELETE ON securities_historical_validation_run
FOR EACH ROW EXECUTE FUNCTION apex_guard_securities_historical_validation_run();
"""

FORWARD_PLAN_GUARD_DDL = r"""
CREATE FUNCTION apex_guard_securities_forward_validation_plan() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'forward validation plan cannot be deleted'; END IF;
    IF (to_jsonb(OLD) - ARRAY['state','current_watermark','activated_at','paused_at','revoked_at','canonical_result_hash'])
       <> (to_jsonb(NEW) - ARRAY['state','current_watermark','activated_at','paused_at','revoked_at','canonical_result_hash']) THEN
        RAISE EXCEPTION 'forward validation plan inputs are immutable';
    END IF;
    IF OLD.state = 'DRAFT' AND NEW.state IN ('ACTIVE','REVOKED','BLOCKED_INTEGRITY') THEN RETURN NEW; END IF;
    IF OLD.state = 'ACTIVE' AND NEW.state IN ('PAUSED','REVOKED','COMPLETED','BLOCKED_INTEGRITY') THEN RETURN NEW; END IF;
    IF OLD.state = 'PAUSED' AND NEW.state IN ('ACTIVE','REVOKED','BLOCKED_INTEGRITY') THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'forward validation plan transition is invalid';
END;
$$;
CREATE TRIGGER apex_guard_securities_forward_validation_plan_trigger
BEFORE UPDATE OR DELETE ON securities_forward_validation_plan
FOR EACH ROW EXECUTE FUNCTION apex_guard_securities_forward_validation_plan();
"""


def upgrade() -> None:
    real_securities_data_metadata.create_all(
        bind=op.get_bind(), tables=REAL_SECURITIES_DATA_TABLES, checkfirst=False
    )
    for table_name in REAL_SECURITIES_DATA_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(FETCH_GUARD_DDL))
    op.execute(sa.text(VALIDATION_GUARD_DDL))
    op.execute(sa.text(FORWARD_PLAN_GUARD_DDL))


def downgrade() -> None:
    """Refuse to discard retained provider or real-validation evidence."""

    checks = " OR ".join(
        f'EXISTS (SELECT 1 FROM "{table.name}")' for table in REAL_SECURITIES_DATA_TABLES
    )
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0031 with retained real Securities provider or validation evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_securities_forward_validation_plan_trigger ON securities_forward_validation_plan"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_securities_forward_validation_plan()"))
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_securities_historical_validation_run_trigger ON securities_historical_validation_run"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_securities_historical_validation_run()"))
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_securities_provider_fetch_operation_trigger ON securities_provider_fetch_operation"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_securities_provider_fetch_operation()"))
    for table_name in REAL_SECURITIES_DATA_IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table_name}_trigger ON "{table_name}"'))
    real_securities_data_metadata.drop_all(bind=op.get_bind(), tables=REAL_SECURITIES_DATA_TABLES)
