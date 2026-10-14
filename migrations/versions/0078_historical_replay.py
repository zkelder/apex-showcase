"""Point-in-time historical replay: durable/resumable replay runs, append-only
checkpoints, deterministic point-in-time feature frames (with an embedded
market-regime layer), research-method evaluations with a guarded, monotonic
outcome sub-state, a research train/validation/holdout period policy, and
lineage linking a replay run to the existing research-evidence hypothesis/
experiment lifecycle.

Revision ID: 0078_historical_replay
Revises: 0077_historical_market_data
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0078_historical_replay"
down_revision: str | None = "0077_historical_market_data"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "historical_replay_checkpoint",
    "historical_replay_feature_frame",
    "historical_replay_research_period_policy",
    "historical_replay_experiment_link",
)
ALL_TABLES = (
    "historical_replay_run",
    "historical_replay_checkpoint",
    "historical_replay_feature_frame",
    "historical_replay_evaluation",
    "historical_replay_research_period_policy",
    "historical_replay_experiment_link",
)

RUN_GUARD_DDL = r"""
CREATE FUNCTION apex_guard_historical_replay_run() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'historical replay run cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'cursor_as_of','cursor_session_date','completed_step_count',
        'completion_state','failure_reason','payload'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'cursor_as_of','cursor_session_date','completed_step_count',
        'completion_state','failure_reason','payload'
    ]) THEN
        RAISE EXCEPTION 'historical replay run identity fields are immutable';
    END IF;
    IF OLD.completion_state IN ('COMPLETE', 'FAILED')
        AND NEW.completion_state IS DISTINCT FROM OLD.completion_state THEN
        RAISE EXCEPTION 'historical replay run completion state is terminal';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER apex_guard_historical_replay_run_trigger
BEFORE UPDATE OR DELETE ON historical_replay_run
FOR EACH ROW EXECUTE FUNCTION apex_guard_historical_replay_run();
"""

EVALUATION_GUARD_DDL = r"""
CREATE FUNCTION apex_guard_historical_replay_evaluation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'historical replay evaluation cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY['outcome_completeness','payload']) <>
       (to_jsonb(NEW) - ARRAY['outcome_completeness','payload']) THEN
        RAISE EXCEPTION 'historical replay evaluation identity fields are immutable';
    END IF;
    IF OLD.outcome_completeness = 'COMPLETE'
        AND NEW.outcome_completeness IS DISTINCT FROM OLD.outcome_completeness THEN
        RAISE EXCEPTION 'a completed historical replay outcome cannot be altered';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER apex_guard_historical_replay_evaluation_trigger
BEFORE UPDATE OR DELETE ON historical_replay_evaluation
FOR EACH ROW EXECUTE FUNCTION apex_guard_historical_replay_evaluation();
"""


def upgrade() -> None:
    op.create_table(
        "historical_replay_run",
        sa.Column("replay_run_id", sa.String(160), primary_key=True),
        sa.Column(
            "dataset_snapshot_id",
            sa.String(160),
            sa.ForeignKey("historical_dataset_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("start_date", sa.String(10), nullable=False),
        sa.Column("end_date", sa.String(10), nullable=False),
        sa.Column("cadence", sa.String(16), nullable=False),
        sa.Column("calendar_identity", sa.String(24), nullable=True),
        sa.Column("feature_policy_id", sa.String(160), nullable=False),
        sa.Column("feature_policy_hash", sa.String(64), nullable=False),
        sa.Column("research_policy_id", sa.String(160), nullable=False),
        sa.Column("research_policy_hash", sa.String(64), nullable=False),
        sa.Column("initial_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("final_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calendar_coverage_state", sa.String(40), nullable=False),
        sa.Column("cursor_as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor_session_date", sa.String(10), nullable=True),
        sa.Column("completed_step_count", sa.Integer(), nullable=False),
        sa.Column("completion_state", sa.String(16), nullable=False),
        sa.Column("failure_reason", sa.String(500), nullable=True),
        sa.Column("hypothesis_version_id", sa.String(160), nullable=True),
        sa.Column("operator_action_id", sa.String(160), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("idempotency_key", name="uq_historical_replay_run_idempotency"),
        sa.CheckConstraint("start_date <= end_date", name="ck_historical_replay_run_date_range"),
        sa.CheckConstraint("cadence IN ('DAILY')", name="ck_historical_replay_run_cadence"),
        sa.CheckConstraint("completed_step_count >= 0", name="ck_historical_replay_run_step_count"),
        sa.CheckConstraint(
            "completion_state IN ('NOT_STARTED','IN_PROGRESS','COMPLETE','FAILED')",
            name="ck_historical_replay_run_completion_state",
        ),
        sa.CheckConstraint(
            "calendar_coverage_state IN "
            "('CALENDAR_EVIDENCE_AVAILABLE','CALENDAR_EVIDENCE_UNAVAILABLE')",
            name="ck_historical_replay_run_calendar_coverage",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_historical_replay_run_authority"),
    )
    op.create_index(
        "ix_historical_replay_run_snapshot", "historical_replay_run", ["dataset_snapshot_id"]
    )
    op.create_index("ix_historical_replay_run_created_at", "historical_replay_run", ["created_at"])

    op.create_table(
        "historical_replay_checkpoint",
        sa.Column("checkpoint_id", sa.String(160), primary_key=True),
        sa.Column(
            "replay_run_id",
            sa.String(160),
            sa.ForeignKey("historical_replay_run.replay_run_id"),
            nullable=False,
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_date", sa.String(10), nullable=False),
        sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
        sa.Column("feature_policy_hash", sa.String(64), nullable=False),
        sa.Column("research_policy_hash", sa.String(64), nullable=False),
        sa.Column("completed_step_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "replay_run_id", "session_date", name="uq_historical_replay_checkpoint_run_date"
        ),
        sa.CheckConstraint(
            "completed_step_count >= 0", name="ck_historical_replay_checkpoint_step_count"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_historical_replay_checkpoint_authority"),
    )
    op.create_index(
        "ix_historical_replay_checkpoint_run", "historical_replay_checkpoint", ["replay_run_id"]
    )

    op.create_table(
        "historical_replay_feature_frame",
        sa.Column("frame_id", sa.String(160), primary_key=True),
        sa.Column(
            "replay_run_id",
            sa.String(160),
            sa.ForeignKey("historical_replay_run.replay_run_id"),
            nullable=False,
        ),
        sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_date", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "replay_run_id", "session_date", name="uq_historical_replay_frame_run_date"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_historical_replay_frame_authority"),
    )
    op.create_index(
        "ix_historical_replay_frame_run_as_of",
        "historical_replay_feature_frame",
        ["replay_run_id", "as_of"],
    )

    op.create_table(
        "historical_replay_evaluation",
        sa.Column("evaluation_id", sa.String(160), primary_key=True),
        sa.Column(
            "replay_run_id",
            sa.String(160),
            sa.ForeignKey("historical_replay_run.replay_run_id"),
            nullable=False,
        ),
        sa.Column(
            "frame_id",
            sa.String(160),
            sa.ForeignKey("historical_replay_feature_frame.frame_id"),
            nullable=False,
        ),
        sa.Column("method_id", sa.String(80), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_date", sa.String(10), nullable=False),
        sa.Column("outcome_completeness", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "replay_run_id",
            "frame_id",
            "method_id",
            name="uq_historical_replay_evaluation_identity",
        ),
        sa.CheckConstraint(
            "outcome_completeness IS NULL OR "
            "outcome_completeness IN ('PENDING','PARTIAL','COMPLETE','UNAVAILABLE')",
            name="ck_historical_replay_evaluation_outcome_state",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_historical_replay_evaluation_authority"),
    )
    op.create_index(
        "ix_historical_replay_evaluation_run_as_of",
        "historical_replay_evaluation",
        ["replay_run_id", "as_of"],
    )
    op.create_index(
        "ix_historical_replay_evaluation_pending",
        "historical_replay_evaluation",
        ["replay_run_id", "outcome_completeness"],
    )

    op.create_table(
        "historical_replay_research_period_policy",
        sa.Column("policy_id", sa.String(160), primary_key=True),
        sa.Column(
            "replay_run_id",
            sa.String(160),
            sa.ForeignKey("historical_replay_run.replay_run_id"),
            nullable=False,
        ),
        sa.Column("minimum_sample_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("replay_run_id", name="uq_historical_replay_period_policy_run"),
        sa.CheckConstraint(
            "minimum_sample_count >= 1", name="ck_historical_replay_period_policy_minimum"
        ),
        sa.CheckConstraint(
            "authority = 'NONE'", name="ck_historical_replay_period_policy_authority"
        ),
    )

    op.create_table(
        "historical_replay_experiment_link",
        sa.Column("link_id", sa.String(160), primary_key=True),
        sa.Column(
            "replay_run_id",
            sa.String(160),
            sa.ForeignKey("historical_replay_run.replay_run_id"),
            nullable=False,
        ),
        sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
        sa.Column("research_dataset_snapshot_id", sa.String(160), nullable=False),
        sa.Column("research_experiment_run_id", sa.String(160), nullable=False),
        sa.Column("research_experiment_outcome_id", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_historical_replay_link_authority"),
    )
    op.create_index(
        "ix_historical_replay_link_run", "historical_replay_experiment_link", ["replay_run_id"]
    )

    op.execute(sa.text(RUN_GUARD_DDL))
    op.execute(sa.text(EVALUATION_GUARD_DDL))
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0078 with retained historical replay evidence'; "
            "END IF; END $$"
        )
    )
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_historical_replay_evaluation_trigger "
            "ON historical_replay_evaluation"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_historical_replay_evaluation()"))
    op.execute(
        sa.text("DROP TRIGGER apex_guard_historical_replay_run_trigger ON historical_replay_run")
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_historical_replay_run()"))
    op.drop_table("historical_replay_experiment_link")
    op.drop_table("historical_replay_research_period_policy")
    op.drop_table("historical_replay_evaluation")
    op.drop_table("historical_replay_feature_frame")
    op.drop_table("historical_replay_checkpoint")
    op.drop_table("historical_replay_run")
