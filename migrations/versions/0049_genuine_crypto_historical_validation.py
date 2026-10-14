"""Persist bounded genuine historical crypto validation evidence.

Revision ID: 0049_crypto_validation
Revises: 0048_research_evidence
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0049_crypto_validation"
down_revision: str | None = "0048_research_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crypto_hypothesis_specification",
        sa.Column(
            "hypothesis_version_id",
            sa.String(160),
            sa.ForeignKey("research_hypothesis_version.hypothesis_version_id"),
            primary_key=True,
        ),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_hypothesis_authority"),
    )
    op.create_table(
        "crypto_historical_capture",
        sa.Column("capture_id", sa.String(160), primary_key=True),
        sa.Column("evidence_class", sa.String(40), nullable=False),
        sa.Column("status", sa.String(48), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "evidence_class IN ('FIXTURE','HISTORICAL_PUBLIC')",
            name="ck_crypto_capture_evidence_class",
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING','COMPLETED','CAPABILITY_UNAVAILABLE','FAILED')",
            name="ck_crypto_capture_status",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_capture_authority"),
    )
    op.create_table(
        "crypto_capture_attempt",
        sa.Column("attempt_id", sa.String(160), primary_key=True),
        sa.Column(
            "capture_id",
            sa.String(160),
            sa.ForeignKey("crypto_historical_capture.capture_id"),
            nullable=False,
        ),
        sa.Column("request_ordinal", sa.Integer(), nullable=False),
        sa.Column("partition_ordinal", sa.Integer(), nullable=False),
        sa.Column("raw_payload", sa.LargeBinary()),
        sa.Column("raw_response_hash", sa.String(64)),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("capture_id", "request_ordinal", name="uq_crypto_attempt_request"),
        sa.CheckConstraint(
            "request_ordinal >= 0 AND request_ordinal < 4 "
            "AND partition_ordinal >= 0 AND partition_ordinal < 4",
            name="ck_crypto_attempt_bounds",
        ),
    )
    op.create_table(
        "crypto_daily_candle",
        sa.Column("normalized_hash", sa.String(64), primary_key=True),
        sa.Column(
            "capture_id",
            sa.String(160),
            sa.ForeignKey("crypto_historical_capture.capture_id"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.String(160),
            sa.ForeignKey("crypto_capture_attempt.attempt_id"),
            nullable=False,
        ),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("capture_id", "bucket_start", name="uq_crypto_candle_capture_bucket"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_candle_authority"),
    )
    op.create_table(
        "crypto_capture_checkpoint",
        sa.Column("checkpoint_id", sa.String(160), primary_key=True),
        sa.Column(
            "capture_id",
            sa.String(160),
            sa.ForeignKey("crypto_historical_capture.capture_id"),
            nullable=False,
        ),
        sa.Column("next_partition_ordinal", sa.Integer(), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "capture_id",
            "next_partition_ordinal",
            name="uq_crypto_checkpoint_partition",
        ),
        sa.CheckConstraint(
            "next_partition_ordinal >= 1 AND next_partition_ordinal <= 4",
            name="ck_crypto_checkpoint_bound",
        ),
    )
    op.create_table(
        "crypto_historical_snapshot",
        sa.Column("snapshot_id", sa.String(160), primary_key=True),
        sa.Column(
            "capture_id",
            sa.String(160),
            sa.ForeignKey("crypto_historical_capture.capture_id"),
            nullable=False,
        ),
        sa.Column("manifest_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("evidence_class", sa.String(40), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_snapshot_authority"),
    )
    op.create_table(
        "crypto_experiment_run",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column(
            "hypothesis_version_id",
            sa.String(160),
            sa.ForeignKey("crypto_hypothesis_specification.hypothesis_version_id"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.String(160),
            sa.ForeignKey("crypto_historical_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("configuration_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_run_authority"),
    )
    op.create_table(
        "crypto_experiment_outcome",
        sa.Column("outcome_id", sa.String(160), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(160),
            sa.ForeignKey("crypto_experiment_run.run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("paper_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("live_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "decision IN ('REJECTED','DEFERRED','PROMOTION_CANDIDATE')",
            name="ck_crypto_outcome_decision",
        ),
        sa.CheckConstraint(
            "paper_eligible = false AND live_eligible = false AND authority = 'NONE'",
            name="ck_crypto_outcome_authority",
        ),
    )
    for table in (
        "crypto_hypothesis_specification",
        "crypto_capture_attempt",
        "crypto_daily_candle",
        "crypto_capture_checkpoint",
        "crypto_historical_snapshot",
        "crypto_experiment_run",
        "crypto_experiment_outcome",
    ):
        op.execute(sa.text(immutable_trigger_ddl(table)))
    op.execute(
        sa.text(
            """
            CREATE FUNCTION apex_guard_crypto_capture() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE'
                   OR OLD.capture_id IS DISTINCT FROM NEW.capture_id
                   OR OLD.evidence_class IS DISTINCT FROM NEW.evidence_class
                   OR OLD.request_hash IS DISTINCT FROM NEW.request_hash
                   OR OLD.authority IS DISTINCT FROM NEW.authority
                   OR OLD.status <> 'RUNNING'
                   OR NEW.status NOT IN
                      ('RUNNING','COMPLETED','CAPABILITY_UNAVAILABLE','FAILED')
                   OR OLD.state_hash = NEW.state_hash
                THEN
                    RAISE EXCEPTION
                        'crypto historical capture transition is immutable or invalid';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER apex_guard_crypto_capture_trigger
            BEFORE UPDATE OR DELETE ON crypto_historical_capture
            FOR EACH ROW EXECUTE FUNCTION apex_guard_crypto_capture();
            """
        )
    )


def downgrade() -> None:
    tables = (
        "crypto_experiment_outcome",
        "crypto_experiment_run",
        "crypto_historical_snapshot",
        "crypto_capture_checkpoint",
        "crypto_daily_candle",
        "crypto_capture_attempt",
        "crypto_historical_capture",
        "crypto_hypothesis_specification",
    )
    for table in tables:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "
                "RAISE EXCEPTION 'cannot downgrade 0049 with retained crypto evidence'; "
                "END IF; END $$"
            )
        )
    op.execute(
        sa.text("DROP TRIGGER apex_guard_crypto_capture_trigger ON crypto_historical_capture")
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_crypto_capture()"))
    for table in (
        "crypto_experiment_outcome",
        "crypto_experiment_run",
        "crypto_historical_snapshot",
        "crypto_capture_checkpoint",
        "crypto_daily_candle",
        "crypto_capture_attempt",
        "crypto_hypothesis_specification",
    ):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
    for table in tables:
        op.drop_table(table)
