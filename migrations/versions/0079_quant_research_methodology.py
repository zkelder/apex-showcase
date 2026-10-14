"""Quant research methodology and walk-forward validation: holdout-by-default
governance policies and append-only holdout-exposure events, phase-labeled
research trials with parameter/configuration provenance, multiple-testing-
aware trial families, a durable/resumable walk-forward run with append-only
fold results, reusable robustness-check runs, retained research conclusions,
and fail-closed promotion evaluations.

Revision ID: 0079_quant_research_methodology
Revises: 0078_historical_replay
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0079_quant_research_methodology"
down_revision: str | None = "0078_historical_replay"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "research_governance_policy",
    "research_governance_holdout_exposure",
    "research_trial",
    "research_trial_family",
    "walk_forward_fold",
    "robustness_check_run",
    "research_conclusion",
    "research_promotion_evaluation",
)
ALL_TABLES = (
    *IMMUTABLE_TABLES,
    "walk_forward_run",
)

WALK_FORWARD_RUN_GUARD_DDL = r"""
CREATE FUNCTION apex_guard_walk_forward_run() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'walk-forward run cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'cursor_fold_ordinal','completed_fold_count','completion_state',
        'failure_reason','payload'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'cursor_fold_ordinal','completed_fold_count','completion_state',
        'failure_reason','payload'
    ]) THEN
        RAISE EXCEPTION 'walk-forward run identity fields are immutable';
    END IF;
    IF OLD.completion_state IN ('COMPLETE', 'FAILED')
        AND NEW.completion_state IS DISTINCT FROM OLD.completion_state THEN
        RAISE EXCEPTION 'walk-forward run completion state is terminal';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER apex_guard_walk_forward_run_trigger
BEFORE UPDATE OR DELETE ON walk_forward_run
FOR EACH ROW EXECUTE FUNCTION apex_guard_walk_forward_run();
"""


def upgrade() -> None:
    op.create_table(
        "research_governance_policy",
        sa.Column("governance_policy_id", sa.String(160), primary_key=True),
        sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
        sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
        sa.Column(
            "replay_run_id",
            sa.String(160),
            sa.ForeignKey("historical_replay_run.replay_run_id"),
            nullable=False,
        ),
        sa.Column("intent", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_governance_policy_id", sa.String(160), nullable=True),
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "intent IN ('EXPLORATORY_ONLY','PROMOTION_TRACK')",
            name="ck_research_governance_policy_intent",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_research_governance_policy_revision"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_research_governance_policy_authority"),
    )
    op.create_index(
        "ix_research_governance_policy_hypothesis",
        "research_governance_policy",
        ["hypothesis_version_id"],
    )
    op.create_index(
        "ix_research_governance_policy_replay_run", "research_governance_policy", ["replay_run_id"]
    )

    op.create_table(
        "research_governance_holdout_exposure",
        sa.Column("exposure_id", sa.String(160), primary_key=True),
        sa.Column(
            "governance_policy_id",
            sa.String(160),
            sa.ForeignKey("research_governance_policy.governance_policy_id"),
            nullable=False,
        ),
        sa.Column("exposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(160), nullable=False),
        sa.Column("purpose", sa.String(160), nullable=False),
        sa.Column("consumed_trial_id", sa.String(160), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "authority = 'NONE'", name="ck_research_governance_holdout_exposure_authority"
        ),
    )
    op.create_index(
        "ix_research_governance_holdout_exposure_policy",
        "research_governance_holdout_exposure",
        ["governance_policy_id"],
    )

    op.create_table(
        "research_trial",
        sa.Column("trial_id", sa.String(160), primary_key=True),
        sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
        sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
        sa.Column(
            "replay_run_id",
            sa.String(160),
            sa.ForeignKey("historical_replay_run.replay_run_id"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("method_id", sa.String(80), nullable=False),
        sa.Column("configuration_origin", sa.String(32), nullable=False),
        sa.Column("session_date_start", sa.String(10), nullable=False),
        sa.Column("session_date_end", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "phase IN ('EXPLORATORY','VALIDATION','LOCKED_HOLDOUT','WALK_FORWARD','FORWARD_PAPER')",
            name="ck_research_trial_phase",
        ),
        sa.CheckConstraint(
            "session_date_start <= session_date_end", name="ck_research_trial_date_range"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_research_trial_authority"),
    )
    op.create_index("ix_research_trial_hypothesis", "research_trial", ["hypothesis_version_id"])
    op.create_index("ix_research_trial_replay_run", "research_trial", ["replay_run_id"])
    op.create_index("ix_research_trial_phase", "research_trial", ["hypothesis_version_id", "phase"])

    op.create_table(
        "research_trial_family",
        sa.Column("trial_family_id", sa.String(160), primary_key=True),
        sa.Column("hypothesis_id", sa.String(160), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("test_count", sa.Integer(), nullable=False),
        sa.Column("selection_method", sa.String(32), nullable=False),
        sa.Column("correction_applied", sa.Boolean(), nullable=False),
        sa.Column("correction_method", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("candidate_count >= test_count", name="ck_research_trial_family_counts"),
        sa.CheckConstraint(
            "selection_method IN ('PREDECLARED_SINGLE','BEST_OF_FAMILY')",
            name="ck_research_trial_family_selection",
        ),
        sa.CheckConstraint(
            "correction_method IN ('NONE','BONFERRONI','BENJAMINI_HOCHBERG_FDR')",
            name="ck_research_trial_family_correction",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_research_trial_family_authority"),
    )
    op.create_index(
        "ix_research_trial_family_hypothesis", "research_trial_family", ["hypothesis_id"]
    )

    op.create_table(
        "walk_forward_run",
        sa.Column("walk_forward_run_id", sa.String(160), primary_key=True),
        sa.Column(
            "replay_run_id",
            sa.String(160),
            sa.ForeignKey("historical_replay_run.replay_run_id"),
            nullable=False,
        ),
        sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
        sa.Column("method_id", sa.String(80), nullable=False),
        sa.Column("window_mode", sa.String(16), nullable=False),
        sa.Column("training_window_sessions", sa.Integer(), nullable=False),
        sa.Column("validation_window_sessions", sa.Integer(), nullable=False),
        sa.Column("test_window_sessions", sa.Integer(), nullable=False),
        sa.Column("step_sessions", sa.Integer(), nullable=False),
        sa.Column("minimum_samples", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.String(10), nullable=False),
        sa.Column("end_date", sa.String(10), nullable=False),
        sa.Column("hypothesis_version_id", sa.String(160), nullable=True),
        sa.Column("cursor_fold_ordinal", sa.Integer(), nullable=False),
        sa.Column("completed_fold_count", sa.Integer(), nullable=False),
        sa.Column("completion_state", sa.String(16), nullable=False),
        sa.Column("failure_reason", sa.String(500), nullable=True),
        sa.Column("operator_action_id", sa.String(160), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("idempotency_key", name="uq_walk_forward_run_idempotency"),
        sa.CheckConstraint("start_date <= end_date", name="ck_walk_forward_run_date_range"),
        sa.CheckConstraint(
            "window_mode IN ('EXPANDING','ROLLING')", name="ck_walk_forward_run_window_mode"
        ),
        sa.CheckConstraint(
            "training_window_sessions >= 1 AND test_window_sessions >= 1 AND step_sessions >= 1",
            name="ck_walk_forward_run_window_lengths",
        ),
        sa.CheckConstraint(
            "validation_window_sessions >= 0", name="ck_walk_forward_run_validation_window"
        ),
        sa.CheckConstraint("cursor_fold_ordinal >= 0", name="ck_walk_forward_run_cursor"),
        sa.CheckConstraint(
            "completion_state IN ('NOT_STARTED','IN_PROGRESS','COMPLETE','FAILED')",
            name="ck_walk_forward_run_completion_state",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_walk_forward_run_authority"),
    )
    op.create_index("ix_walk_forward_run_replay_run", "walk_forward_run", ["replay_run_id"])

    op.create_table(
        "walk_forward_fold",
        sa.Column("fold_id", sa.String(160), primary_key=True),
        sa.Column(
            "walk_forward_run_id",
            sa.String(160),
            sa.ForeignKey("walk_forward_run.walk_forward_run_id"),
            nullable=False,
        ),
        sa.Column("fold_ordinal", sa.Integer(), nullable=False),
        sa.Column("train_start", sa.String(10), nullable=False),
        sa.Column("train_end", sa.String(10), nullable=False),
        sa.Column("test_start", sa.String(10), nullable=False),
        sa.Column("test_end", sa.String(10), nullable=False),
        sa.Column("outcome_state", sa.String(24), nullable=False),
        sa.Column(
            "test_trial_id",
            sa.String(160),
            sa.ForeignKey("research_trial.trial_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "walk_forward_run_id", "fold_ordinal", name="uq_walk_forward_fold_run_ordinal"
        ),
        sa.CheckConstraint("fold_ordinal >= 1", name="ck_walk_forward_fold_ordinal"),
        sa.CheckConstraint("test_start > train_end", name="ck_walk_forward_fold_no_leakage"),
        sa.CheckConstraint(
            "outcome_state IN ('READY','OUTCOME_PENDING','INSUFFICIENT_SAMPLE')",
            name="ck_walk_forward_fold_outcome_state",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_walk_forward_fold_authority"),
    )
    op.create_index("ix_walk_forward_fold_run", "walk_forward_fold", ["walk_forward_run_id"])

    op.create_table(
        "robustness_check_run",
        sa.Column("robustness_run_id", sa.String(160), primary_key=True),
        sa.Column(
            "base_trial_id",
            sa.String(160),
            sa.ForeignKey("research_trial.trial_id"),
            nullable=False,
        ),
        sa.Column("checks_run", sa.Integer(), nullable=False),
        sa.Column("checks_consistent", sa.Integer(), nullable=False),
        sa.Column("checks_inconsistent", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("checks_run >= 1", name="ck_robustness_check_run_checks"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_robustness_check_run_authority"),
    )
    op.create_index("ix_robustness_check_run_trial", "robustness_check_run", ["base_trial_id"])

    op.create_table(
        "research_conclusion",
        sa.Column("conclusion_id", sa.String(160), primary_key=True),
        sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
        sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
        sa.Column(
            "replay_run_id",
            sa.String(160),
            sa.ForeignKey("historical_replay_run.replay_run_id"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("sample_quality_state", sa.String(40), nullable=False),
        sa.Column("conclusion_state", sa.String(24), nullable=False),
        sa.Column(
            "trial_family_id",
            sa.String(160),
            sa.ForeignKey("research_trial_family.trial_family_id"),
            nullable=True,
        ),
        sa.Column(
            "robustness_run_id",
            sa.String(160),
            sa.ForeignKey("robustness_check_run.robustness_run_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "phase IN ('EXPLORATORY','VALIDATION','LOCKED_HOLDOUT','WALK_FORWARD','FORWARD_PAPER')",
            name="ck_research_conclusion_phase",
        ),
        sa.CheckConstraint(
            "sample_quality_state IN ('SUFFICIENT','INSUFFICIENT_EFFECTIVE_SAMPLE')",
            name="ck_research_conclusion_sample_quality",
        ),
        sa.CheckConstraint(
            "conclusion_state IN ('SUPPORTED','WEAK_SUPPORT','INCONCLUSIVE','REJECTED',"
            "'ROBUSTNESS_FAILED','HOLDOUT_FAILED','INSUFFICIENT_EVIDENCE')",
            name="ck_research_conclusion_state",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_research_conclusion_authority"),
    )
    op.create_index(
        "ix_research_conclusion_hypothesis", "research_conclusion", ["hypothesis_version_id"]
    )

    op.create_table(
        "research_promotion_evaluation",
        sa.Column("promotion_evaluation_id", sa.String(160), primary_key=True),
        sa.Column(
            "conclusion_id",
            sa.String(160),
            sa.ForeignKey("research_conclusion.conclusion_id"),
            nullable=False,
        ),
        sa.Column("policy_id", sa.String(80), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_research_promotion_evaluation_authority"),
    )
    op.create_index(
        "ix_research_promotion_evaluation_conclusion",
        "research_promotion_evaluation",
        ["conclusion_id"],
    )

    op.execute(sa.text(WALK_FORWARD_RUN_GUARD_DDL))
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0079 with retained research methodology evidence'; "
            "END IF; END $$"
        )
    )
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.execute(sa.text("DROP TRIGGER apex_guard_walk_forward_run_trigger ON walk_forward_run"))
    op.execute(sa.text("DROP FUNCTION apex_guard_walk_forward_run()"))
    op.drop_table("research_promotion_evaluation")
    op.drop_table("research_conclusion")
    op.drop_table("robustness_check_run")
    op.drop_table("walk_forward_fold")
    op.drop_table("walk_forward_run")
    op.drop_table("research_trial_family")
    op.drop_table("research_trial")
    op.drop_table("research_governance_holdout_exposure")
    op.drop_table("research_governance_policy")
