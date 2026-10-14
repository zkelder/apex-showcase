"""Persist bounded, research-only Strategy Lab evidence.

Revision ID: 0023_strategy_lab_foundation
Revises: 0022_evidence_review_trials
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    STRATEGY_LAB_IMMUTABLE_TABLES,
    STRATEGY_LAB_TABLES,
    immutable_trigger_ddl,
    strategy_lab_metadata,
)

revision: str = "0023_strategy_lab_foundation"
down_revision: str | None = "0022_evidence_review_trials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DATASET_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_strategy_lab_dataset() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'strategy lab dataset cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'state', 'artifact_relative_location', 'artifact_sha256', 'artifact_byte_size',
        'row_count', 'entity_count', 'grouping_count', 'feature_first_at', 'feature_last_at',
        'label_first_at', 'label_last_at', 'integrity_warning_codes', 'canonical_result_hash',
        'completed_at', 'failure_code'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'state', 'artifact_relative_location', 'artifact_sha256', 'artifact_byte_size',
        'row_count', 'entity_count', 'grouping_count', 'feature_first_at', 'feature_last_at',
        'label_first_at', 'label_last_at', 'integrity_warning_codes', 'canonical_result_hash',
        'completed_at', 'failure_code'
    ]) THEN
        RAISE EXCEPTION 'strategy lab dataset inputs are immutable';
    END IF;
    IF OLD.state = 'BUILDING' AND NEW.state IN ('READY', 'INVALID') THEN RETURN NEW; END IF;
    IF OLD.state = 'READY' AND NEW.state = 'SUPERSEDED' THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'strategy lab dataset transition is invalid';
END;
$$;
CREATE TRIGGER apex_guard_strategy_lab_dataset_trigger
BEFORE UPDATE OR DELETE ON strategy_lab_dataset_snapshot
FOR EACH ROW EXECUTE FUNCTION apex_guard_strategy_lab_dataset();
"""

TRAINING_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_strategy_lab_training() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'strategy lab training run cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'state', 'completed_at', 'model_artifact_relative_location', 'model_artifact_sha256',
        'model_artifact_byte_size', 'canonical_result_hash', 'failure_code'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'state', 'completed_at', 'model_artifact_relative_location', 'model_artifact_sha256',
        'model_artifact_byte_size', 'canonical_result_hash', 'failure_code'
    ]) THEN
        RAISE EXCEPTION 'strategy lab training inputs are immutable';
    END IF;
    IF OLD.state = 'REQUESTED' AND NEW.state IN ('RUNNING', 'REJECTED_INTEGRITY', 'FAILED') THEN RETURN NEW; END IF;
    IF OLD.state = 'RUNNING' AND NEW.state IN ('COMPLETED', 'FAILED', 'REJECTED_INTEGRITY') THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'strategy lab training transition is invalid';
END;
$$;
CREATE TRIGGER apex_guard_strategy_lab_training_trigger
BEFORE UPDATE OR DELETE ON strategy_lab_training_run
FOR EACH ROW EXECUTE FUNCTION apex_guard_strategy_lab_training();
"""


def upgrade() -> None:
    strategy_lab_metadata.create_all(
        bind=op.get_bind(), tables=STRATEGY_LAB_TABLES, checkfirst=False
    )
    for table_name in STRATEGY_LAB_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(DATASET_GUARD_DDL))
    op.execute(sa.text(TRAINING_GUARD_DDL))


def downgrade() -> None:
    """Do not discard retained research evidence during a downgrade."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM strategy_lab_feature_schema) "
            "OR EXISTS (SELECT 1 FROM strategy_lab_label_definition) "
            "OR EXISTS (SELECT 1 FROM strategy_lab_dataset_snapshot) "
            "OR EXISTS (SELECT 1 FROM strategy_lab_training_run) "
            "OR EXISTS (SELECT 1 FROM strategy_lab_walk_forward_evaluation) "
            "OR EXISTS (SELECT 1 FROM strategy_lab_model_candidate) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0023 with retained Strategy Lab research evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_strategy_lab_training_trigger ON strategy_lab_training_run"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_strategy_lab_training()"))
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_strategy_lab_dataset_trigger ON strategy_lab_dataset_snapshot"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_strategy_lab_dataset()"))
    for table_name in STRATEGY_LAB_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    strategy_lab_metadata.drop_all(bind=op.get_bind(), tables=STRATEGY_LAB_TABLES)
