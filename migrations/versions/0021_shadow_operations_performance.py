"""Persist finite local SHADOW operations and public forward-performance evidence.

Revision ID: 0021_shadow_operations
Revises: 0020_multi_strategy_shadow
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    SHADOW_OPERATIONS_IMMUTABLE_TABLES,
    SHADOW_OPERATIONS_TABLES,
    immutable_trigger_ddl,
    shadow_operations_metadata,
)

revision: str = "0021_shadow_operations"
down_revision: str | None = "0020_multi_strategy_shadow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CURRENT_CAPABILITY_CHECK = (
    "capability IN ('PUBLIC_REST_MARKET_DISCOVERY', 'PUBLIC_REST_EVENT_DISCOVERY', "
    "'PUBLIC_REST_ORDERBOOK_CAPTURE', 'PUBLIC_REST_THRESHOLD_LADDER_SCAN')"
)
SHADOW_OPERATIONS_CAPABILITY_CHECK = (
    "capability IN ('PUBLIC_REST_MARKET_DISCOVERY', 'PUBLIC_REST_EVENT_DISCOVERY', "
    "'PUBLIC_REST_ORDERBOOK_CAPTURE', 'PUBLIC_REST_THRESHOLD_LADDER_SCAN', "
    "'PUBLIC_REST_OUTCOME_COLLECTION')"
)


# Revision 0017 used the live SQLAlchemy metadata to create ``shadow_prediction``.
# Consequently, a retained database that applied that historical revision before
# the metadata had reached its current shape can be at the recorded 0020 revision
# while physically missing fields that current strategy-shadow loading requires.
# These markers let this repair remain reversible only for the exact legacy
# columns it had to introduce; correctly provisioned 0020 schemas are untouched.
COMPATIBILITY_COMMENT_PREFIX = "apex:0021-shadow-prediction-compatibility:"
THESIS_SUMMARY_COMPATIBILITY_COMMENT = COMPATIBILITY_COMMENT_PREFIX + "thesis_summary"
CORRELATION_BUCKETS_COMPATIBILITY_COMMENT = COMPATIBILITY_COMMENT_PREFIX + "correlation_buckets"
OPERATOR_NOTE_COMPATIBILITY_COMMENT = COMPATIBILITY_COMMENT_PREFIX + "operator_note"


SHADOW_PREDICTION_CORRELATION_BUCKETS_DDL = r"""
CREATE OR REPLACE FUNCTION apex_shadow_prediction_correlation_buckets_are_canonical(
    buckets text[]
) RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    bucket text;
    previous_bucket text := NULL;
BEGIN
    IF buckets IS NULL THEN
        RETURN FALSE;
    END IF;
    IF cardinality(buckets) = 0 THEN
        RETURN TRUE;
    END IF;
    IF cardinality(buckets) > 8 OR array_ndims(buckets) <> 1 THEN
        RETURN FALSE;
    END IF;
    FOREACH bucket IN ARRAY buckets LOOP
        IF bucket IS NULL OR bucket = '' OR length(bucket) > 80 THEN
            RETURN FALSE;
        END IF;
        IF previous_bucket IS NOT NULL AND previous_bucket >= bucket THEN
            RETURN FALSE;
        END IF;
        previous_bucket := bucket;
    END LOOP;
    RETURN TRUE;
END;
$$;
"""


def _shadow_prediction_column_comment(column_name: str) -> str:
    return f"""
SELECT col_description(
    'shadow_prediction'::regclass,
    (SELECT attnum FROM pg_attribute
     WHERE attrelid = 'shadow_prediction'::regclass
       AND attname = '{column_name}'
       AND NOT attisdropped)
)
"""


SHADOW_EVIDENCE_PLAN_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_shadow_evidence_plan() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'shadow evidence plan cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'state', 'started_at', 'paused_at', 'completed_at', 'next_due_at',
        'completed_iterations', 'partial_iterations', 'failed_iterations',
        'last_completed_stage', 'last_error_code', 'last_action_reason',
        'canonical_result_hash'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'state', 'started_at', 'paused_at', 'completed_at', 'next_due_at',
        'completed_iterations', 'partial_iterations', 'failed_iterations',
        'last_completed_stage', 'last_error_code', 'last_action_reason',
        'canonical_result_hash'
    ]) THEN
        RAISE EXCEPTION 'shadow evidence plan configuration is immutable';
    END IF;
    IF OLD.state = 'CREATED' THEN
        IF NEW.state = 'RUNNING' THEN
            IF NEW.started_at IS NULL THEN
                RAISE EXCEPTION 'shadow evidence plan requires a start';
            END IF;
            RETURN NEW;
        END IF;
        IF NEW.state = 'CANCELED' AND NEW.completed_at IS NOT NULL THEN
            RETURN NEW;
        END IF;
    END IF;
    IF OLD.state = 'RUNNING'
       AND NEW.state IN (
           'RUNNING', 'PAUSED', 'COMPLETED', 'PARTIAL',
           'FAILED', 'CANCELED', 'INTERRUPTED'
       ) THEN
        IF NEW.state IN ('COMPLETED', 'PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED')
           AND NEW.completed_at IS NULL THEN
            RAISE EXCEPTION 'terminal shadow evidence plan requires completion time';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'PAUSED' AND NEW.state IN ('RUNNING', 'CANCELED') THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'shadow evidence plan transition is invalid';
END;
$$;

CREATE TRIGGER apex_guard_shadow_evidence_plan_trigger
BEFORE UPDATE OR DELETE ON shadow_evidence_plan
FOR EACH ROW EXECUTE FUNCTION apex_guard_shadow_evidence_plan();
"""

SHADOW_EVIDENCE_ITERATION_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_shadow_evidence_iteration() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'shadow evidence iteration cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'state', 'completed_at', 'cycle_id', 'cohort_size', 'market_refresh_count',
        'orderbook_success_count', 'orderbook_failure_count', 'prediction_count',
        'comparison_count', 'conflict_count', 'hypothetical_admission_count',
        'hypothetical_rejection_count', 'provider_request_count',
        'last_completed_stage', 'reason_codes',
        'canonical_result_hash'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'state', 'completed_at', 'cycle_id', 'cohort_size', 'market_refresh_count',
        'orderbook_success_count', 'orderbook_failure_count', 'prediction_count',
        'comparison_count', 'conflict_count', 'hypothetical_admission_count',
        'hypothetical_rejection_count', 'provider_request_count',
        'last_completed_stage', 'reason_codes',
        'canonical_result_hash'
    ]) THEN
        RAISE EXCEPTION 'shadow evidence iteration terms are immutable';
    END IF;
    IF OLD.state <> 'RUNNING' THEN RAISE EXCEPTION 'handled shadow iteration is immutable'; END IF;
    IF NEW.state IN ('COMPLETED', 'PARTIAL', 'FAILED', 'INTERRUPTED')
       AND NEW.completed_at IS NOT NULL THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'shadow evidence iteration transition is invalid';
END;
$$;

CREATE TRIGGER apex_guard_shadow_evidence_iteration_trigger
BEFORE UPDATE OR DELETE ON shadow_evidence_iteration
FOR EACH ROW EXECUTE FUNCTION apex_guard_shadow_evidence_iteration();
"""


def upgrade() -> None:
    """Add finite non-financial evidence plans, leases, and score evidence only."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'shadow_prediction' AND column_name = 'thesis_summary') "
            "THEN ALTER TABLE shadow_prediction ADD COLUMN thesis_summary TEXT; "
            "COMMENT ON COLUMN shadow_prediction.thesis_summary IS "
            f"'{THESIS_SUMMARY_COMPATIBILITY_COMMENT}'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'shadow_prediction' AND column_name = 'correlation_buckets') "
            "THEN ALTER TABLE shadow_prediction ADD COLUMN correlation_buckets "
            "TEXT[] NOT NULL DEFAULT ARRAY[]::text[]; "
            "COMMENT ON COLUMN shadow_prediction.correlation_buckets IS "
            f"'{CORRELATION_BUCKETS_COMPATIBILITY_COMMENT}'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text("ALTER TABLE shadow_prediction ALTER COLUMN correlation_buckets DROP DEFAULT")
    )
    op.execute(
        sa.text("ALTER TABLE shadow_prediction ALTER COLUMN correlation_buckets SET NOT NULL")
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'shadow_prediction' AND column_name = 'operator_note') "
            "THEN ALTER TABLE shadow_prediction ADD COLUMN operator_note VARCHAR(500); "
            "COMMENT ON COLUMN shadow_prediction.operator_note IS "
            f"'{OPERATOR_NOTE_COMPATIBILITY_COMMENT}'; "
            "END IF; END $$"
        )
    )
    op.execute(sa.text(SHADOW_PREDICTION_CORRELATION_BUCKETS_DDL))
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conrelid = 'shadow_prediction'::regclass "
            "AND conname = 'ck_shadow_prediction_correlation_buckets') "
            "THEN ALTER TABLE shadow_prediction "
            "ADD CONSTRAINT ck_shadow_prediction_correlation_buckets "
            "CHECK (apex_shadow_prediction_correlation_buckets_are_canonical("
            "correlation_buckets)); "
            "END IF; END $$"
        )
    )
    op.drop_constraint("ck_operator_action_type", "operator_action", type_="check")
    op.create_check_constraint(
        "ck_operator_action_type",
        "operator_action",
        "action_type IN ('CREATE_CAMPAIGN', 'RUN_SHADOW_CYCLE', 'RUN_PAPER_CYCLE', "
        "'PAUSE_CAMPAIGN', 'RESUME_CAMPAIGN', 'KILL_CAMPAIGN', 'MARK_POSITIONS', "
        "'SETTLE_POSITION', 'SUBMIT_THESIS', 'EVALUATE_PREDICTION', "
        "'CHANGE_STRATEGY_AUTHORITY', 'RUN_LIVE_SHADOW_SESSION', "
        "'CREATE_SHADOW_EVIDENCE_PLAN', 'PAUSE_SHADOW_EVIDENCE_PLAN', "
        "'RESUME_SHADOW_EVIDENCE_PLAN', 'CANCEL_SHADOW_EVIDENCE_PLAN', "
        "'RUN_SHADOW_EVIDENCE_ITERATION', 'COLLECT_PUBLIC_OUTCOME', "
        "'EVALUATE_FORWARD_PERFORMANCE')",
    )
    op.drop_constraint(
        "ck_provider_ingestion_run_capability", "provider_ingestion_run", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_ingestion_run_capability",
        "provider_ingestion_run",
        SHADOW_OPERATIONS_CAPABILITY_CHECK,
    )
    shadow_operations_metadata.create_all(
        bind=op.get_bind(), tables=SHADOW_OPERATIONS_TABLES, checkfirst=True
    )
    for table_name in SHADOW_OPERATIONS_IMMUTABLE_TABLES:
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS apex_immutable_{table_name}_trigger ON {table_name}")
        )
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS apex_guard_shadow_evidence_plan_trigger ON shadow_evidence_plan"
        )
    )
    op.execute(sa.text(SHADOW_EVIDENCE_PLAN_GUARD_DDL))
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS apex_guard_shadow_evidence_iteration_trigger "
            "ON shadow_evidence_iteration"
        )
    )
    op.execute(sa.text(SHADOW_EVIDENCE_ITERATION_GUARD_DDL))


def downgrade() -> None:
    """Refuse before DDL whenever retained forward evidence would be lost."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM shadow_evidence_plan) "
            "OR EXISTS (SELECT 1 FROM shadow_evidence_cohort_member) "
            "OR EXISTS (SELECT 1 FROM shadow_evidence_iteration) "
            "OR EXISTS (SELECT 1 FROM shadow_worker_lease) "
            "OR EXISTS (SELECT 1 FROM public_outcome_fact) "
            "OR EXISTS (SELECT 1 FROM shadow_forward_evaluation) "
            "OR EXISTS (SELECT 1 FROM performance_evaluation_run) "
            "OR EXISTS (SELECT 1 FROM provider_ingestion_run "
            "WHERE capability = 'PUBLIC_REST_OUTCOME_COLLECTION') "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0021 with retained shadow operations evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF (" + _shadow_prediction_column_comment("thesis_summary") + ") = "
            f"'{THESIS_SUMMARY_COMPATIBILITY_COMMENT}' "
            "AND EXISTS (SELECT 1 FROM shadow_prediction WHERE thesis_summary IS NOT NULL) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0021: retained thesis summaries would be lost'; "
            "END IF; "
            "IF (" + _shadow_prediction_column_comment("correlation_buckets") + ") = "
            f"'{CORRELATION_BUCKETS_COMPATIBILITY_COMMENT}' "
            "AND EXISTS (SELECT 1 FROM shadow_prediction "
            "WHERE correlation_buckets <> ARRAY[]::text[]) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0021: retained correlation buckets would be lost'; "
            "END IF; "
            "IF (" + _shadow_prediction_column_comment("operator_note") + ") = "
            f"'{OPERATOR_NOTE_COMPATIBILITY_COMMENT}' "
            "AND EXISTS (SELECT 1 FROM shadow_prediction WHERE operator_note IS NOT NULL) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0021: retained operator notes would be lost'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_shadow_evidence_iteration_trigger ON shadow_evidence_iteration"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_shadow_evidence_iteration()"))
    op.execute(
        sa.text("DROP TRIGGER apex_guard_shadow_evidence_plan_trigger ON shadow_evidence_plan")
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_shadow_evidence_plan()"))
    for table_name in SHADOW_OPERATIONS_IMMUTABLE_TABLES:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF to_regclass('{table_name}') IS NOT NULL THEN "
                f"DROP TRIGGER IF EXISTS apex_immutable_{table_name}_trigger ON {table_name}; "
                "END IF; END $$"
            )
        )
    shadow_operations_metadata.drop_all(bind=op.get_bind(), tables=SHADOW_OPERATIONS_TABLES)
    op.execute(
        sa.text(
            "ALTER TABLE shadow_prediction "
            "DROP CONSTRAINT IF EXISTS ck_shadow_prediction_correlation_buckets"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            "apex_shadow_prediction_correlation_buckets_are_canonical(text[])"
        )
    )
    for column_name, comment in (
        ("operator_note", OPERATOR_NOTE_COMPATIBILITY_COMMENT),
        ("correlation_buckets", CORRELATION_BUCKETS_COMPATIBILITY_COMMENT),
        ("thesis_summary", THESIS_SUMMARY_COMPATIBILITY_COMMENT),
    ):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                "IF (" + _shadow_prediction_column_comment(column_name) + ") = "
                f"'{comment}' "
                f"THEN ALTER TABLE shadow_prediction DROP COLUMN {column_name}; "
                "END IF; END $$"
            )
        )
    op.drop_constraint(
        "ck_provider_ingestion_run_capability", "provider_ingestion_run", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_ingestion_run_capability",
        "provider_ingestion_run",
        CURRENT_CAPABILITY_CHECK,
    )
    op.drop_constraint("ck_operator_action_type", "operator_action", type_="check")
    op.create_check_constraint(
        "ck_operator_action_type",
        "operator_action",
        "action_type IN ('CREATE_CAMPAIGN', 'RUN_SHADOW_CYCLE', 'RUN_PAPER_CYCLE', "
        "'PAUSE_CAMPAIGN', 'RESUME_CAMPAIGN', 'KILL_CAMPAIGN', 'MARK_POSITIONS', "
        "'SETTLE_POSITION', 'SUBMIT_THESIS', 'EVALUATE_PREDICTION', "
        "'CHANGE_STRATEGY_AUTHORITY', 'RUN_LIVE_SHADOW_SESSION')",
    )
