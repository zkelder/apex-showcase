"""Persist the bounded hypothesis-to-outcome research evidence lifecycle.

Revision ID: 0048_research_evidence
Revises: 0047_acceptance_isolation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0048_research_evidence"
down_revision: str | None = "0047_acceptance_isolation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_hypothesis_version",
        sa.Column("hypothesis_version_id", sa.String(160), primary_key=True),
        sa.Column("hypothesis_id", sa.String(160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "supersedes_version_id",
            sa.String(160),
            sa.ForeignKey("research_hypothesis_version.hypothesis_version_id"),
        ),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("hypothesis_id", "revision", name="uq_research_hypothesis_revision"),
        sa.CheckConstraint("revision >= 1", name="ck_research_hypothesis_revision"),
        sa.CheckConstraint(
            "status IN ('REGISTERED','SUPERSEDED','RETIRED')",
            name="ck_research_hypothesis_status",
        ),
    )
    op.create_table(
        "research_observation_capture",
        sa.Column("capture_id", sa.String(160), primary_key=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("source_contract_version", sa.String(160), nullable=False),
        sa.Column("parser_version", sa.String(160), nullable=False),
        sa.Column("evidence_class", sa.String(40), nullable=False),
        sa.Column("source_locator", sa.String(500), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_at", sa.DateTime(timezone=True)),
        sa.Column("ingestion_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(48), nullable=False),
        sa.Column("raw_payload", sa.LargeBinary()),
        sa.Column("raw_sha256", sa.String(64)),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("expected_observations", sa.Integer(), nullable=False),
        sa.Column("retained_observations", sa.Integer(), nullable=False),
        sa.Column("next_ordinal", sa.Integer(), nullable=False),
        sa.Column("quality_status", sa.String(32), nullable=False),
        sa.Column("completeness_status", sa.String(32), nullable=False),
        sa.Column("staleness_status", sa.String(32), nullable=False),
        sa.Column("limitations", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "evidence_class IN "
            "('FIXTURE','GENUINE_PUBLIC','HISTORICAL_PUBLIC','SANDBOX','PAPER','LIVE')",
            name="ck_research_capture_evidence_class",
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING','COMPLETED','CAPABILITY_UNAVAILABLE','FAILED')",
            name="ck_research_capture_status",
        ),
        sa.CheckConstraint(
            "byte_count >= 0 AND expected_observations >= 0 "
            "AND retained_observations >= 0 "
            "AND retained_observations <= expected_observations "
            "AND next_ordinal = retained_observations",
            name="ck_research_capture_counts",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_research_capture_authority"),
    )
    op.create_table(
        "research_observation",
        sa.Column("observation_id", sa.String(160), primary_key=True),
        sa.Column(
            "capture_id",
            sa.String(160),
            sa.ForeignKey("research_observation_capture.capture_id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("subject_id", sa.String(160), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("raw_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "capture_id", "ordinal", name="uq_research_observation_capture_ordinal"
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_research_observation_ordinal"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_research_observation_authority"),
    )
    op.create_table(
        "research_capture_checkpoint",
        sa.Column("checkpoint_id", sa.String(160), primary_key=True),
        sa.Column(
            "capture_id",
            sa.String(160),
            sa.ForeignKey("research_observation_capture.capture_id"),
            nullable=False,
        ),
        sa.Column("next_ordinal", sa.Integer(), nullable=False),
        sa.Column("retained_observations", sa.Integer(), nullable=False),
        sa.Column("raw_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("capture_id", "next_ordinal", name="uq_research_checkpoint_ordinal"),
        sa.CheckConstraint(
            "next_ordinal >= 0 AND next_ordinal = retained_observations",
            name="ck_research_checkpoint_counts",
        ),
    )
    op.create_table(
        "research_dataset_snapshot",
        sa.Column("snapshot_id", sa.String(160), primary_key=True),
        sa.Column("evidence_class", sa.String(40), nullable=False),
        sa.Column("component_capture_ids", sa.ARRAY(sa.String(160)), nullable=False),
        sa.Column("universe_id", sa.String(160), nullable=False),
        sa.Column("universe_policy_id", sa.String(160), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_research_snapshot_authority"),
    )
    op.create_table(
        "research_experiment_run",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column(
            "hypothesis_version_id",
            sa.String(160),
            sa.ForeignKey("research_hypothesis_version.hypothesis_version_id"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.String(160),
            sa.ForeignKey("research_dataset_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("executor_version", sa.String(160), nullable=False),
        sa.Column("configuration_hash", sa.String(64), nullable=False),
        sa.Column("code_revision", sa.String(160), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("evidence_class", sa.String(40), nullable=False),
        sa.Column("reproducibility_status", sa.String(40), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("state IN ('COMPLETED','FAILED')", name="ck_research_run_state"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_research_run_authority"),
    )
    op.create_table(
        "research_experiment_outcome",
        sa.Column("outcome_id", sa.String(160), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(160),
            sa.ForeignKey("research_experiment_run.run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paper_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("live_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "decision IN ('REJECTED','DEFERRED','PROMOTION_CANDIDATE')",
            name="ck_research_outcome_decision",
        ),
        sa.CheckConstraint(
            "paper_eligible = false AND live_eligible = false AND authority = 'NONE'",
            name="ck_research_outcome_authority",
        ),
    )
    for table in (
        "research_hypothesis_version",
        "research_observation",
        "research_capture_checkpoint",
        "research_dataset_snapshot",
        "research_experiment_run",
        "research_experiment_outcome",
    ):
        op.execute(sa.text(immutable_trigger_ddl(table)))
    op.execute(
        sa.text(
            """
            CREATE FUNCTION apex_guard_research_capture() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION
                        'research observation capture deletion is prohibited';
                ELSIF OLD.capture_id IS DISTINCT FROM NEW.capture_id
                   OR OLD.provider IS DISTINCT FROM NEW.provider
                   OR OLD.source_contract_version IS DISTINCT FROM NEW.source_contract_version
                   OR OLD.parser_version IS DISTINCT FROM NEW.parser_version
                   OR OLD.evidence_class IS DISTINCT FROM NEW.evidence_class
                   OR OLD.source_locator IS DISTINCT FROM NEW.source_locator
                   OR OLD.requested_at IS DISTINCT FROM NEW.requested_at
                   OR OLD.observation_at IS DISTINCT FROM NEW.observation_at
                   OR OLD.ingestion_at IS DISTINCT FROM NEW.ingestion_at
                   OR OLD.raw_payload IS DISTINCT FROM NEW.raw_payload
                   OR OLD.raw_sha256 IS DISTINCT FROM NEW.raw_sha256
                   OR OLD.byte_count IS DISTINCT FROM NEW.byte_count
                   OR OLD.expected_observations IS DISTINCT FROM NEW.expected_observations
                   OR OLD.request_hash IS DISTINCT FROM NEW.request_hash
                   OR OLD.authority IS DISTINCT FROM NEW.authority
                   OR OLD.status <> 'RUNNING'
                   OR NEW.status NOT IN ('RUNNING', 'COMPLETED')
                   OR NEW.retained_observations <= OLD.retained_observations
                THEN
                    RAISE EXCEPTION
                        'research observation capture transition is immutable or invalid';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER apex_guard_research_capture_trigger
            BEFORE UPDATE OR DELETE ON research_observation_capture
            FOR EACH ROW EXECUTE FUNCTION apex_guard_research_capture();
            """
        )
    )


def downgrade() -> None:
    tables = (
        "research_experiment_outcome",
        "research_experiment_run",
        "research_dataset_snapshot",
        "research_capture_checkpoint",
        "research_observation",
        "research_observation_capture",
        "research_hypothesis_version",
    )
    for table in tables:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "
                "RAISE EXCEPTION 'cannot downgrade 0048 with retained research evidence'; "
                "END IF; END $$"
            )
        )
    op.execute(
        sa.text("DROP TRIGGER apex_guard_research_capture_trigger ON research_observation_capture")
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_research_capture()"))
    for table in (
        "research_experiment_outcome",
        "research_experiment_run",
        "research_dataset_snapshot",
        "research_capture_checkpoint",
        "research_observation",
        "research_hypothesis_version",
    ):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
    for table in tables:
        op.drop_table(table)
