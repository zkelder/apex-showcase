"""Historical market-data capture plans, checkpointed chunks, deterministic
dataset snapshots, and data-quality reports.

Revision ID: 0077_historical_market_data
Revises: 0076_stable_security_identity
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0077_historical_market_data"
down_revision: str | None = "0076_stable_security_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "historical_instrument_identity",
    "historical_capture_plan",
    "historical_dataset_snapshot",
    "historical_data_quality_report",
)
ALL_TABLES = (
    "historical_instrument_identity",
    "historical_capture_plan",
    "historical_capture_chunk",
    "historical_dataset_snapshot",
    "historical_data_quality_report",
)

CHUNK_GUARD_DDL = r"""
CREATE FUNCTION apex_guard_historical_capture_chunk() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'historical capture chunk cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'state','operation_id','batch_id','retry_count','started_at','completed_at','payload'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'state','operation_id','batch_id','retry_count','started_at','completed_at','payload'
    ]) THEN
        RAISE EXCEPTION 'historical capture chunk identity fields are immutable';
    END IF;
    IF OLD.state = 'PENDING' AND NEW.state = 'RUNNING' THEN RETURN NEW; END IF;
    IF OLD.state = 'RUNNING' AND NEW.state IN ('COMPLETED','CREDENTIALS_REQUIRED','FAILED')
        THEN RETURN NEW;
    END IF;
    IF OLD.state = 'CREDENTIALS_REQUIRED' AND NEW.state = 'RUNNING' THEN RETURN NEW; END IF;
    IF OLD.state = 'FAILED' AND NEW.state = 'RUNNING' THEN RETURN NEW; END IF;
    IF OLD.state = 'COMPLETED' AND NEW.state = 'COMPLETED' THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'historical capture chunk transition is invalid';
END;
$$;
CREATE TRIGGER apex_guard_historical_capture_chunk_trigger
BEFORE UPDATE OR DELETE ON historical_capture_chunk
FOR EACH ROW EXECUTE FUNCTION apex_guard_historical_capture_chunk();
"""


def upgrade() -> None:
    op.create_table(
        "historical_instrument_identity",
        sa.Column("instrument_id", sa.String(160), primary_key=True),
        # Not a hard foreign key: security_stable_identity.stable_security_id
        # is not itself unique-constrained (append-only evidence may retain
        # more than one historical row per distinct stable identity), the
        # identical reason migration 0076's own
        # security_identity_resolution.resolved_stable_security_id is a plain
        # column rather than a foreign key. The lineage is a pure function
        # (domain.historical_market_data.historical_instrument_id) that any
        # caller can independently recompute and verify.
        sa.Column("stable_security_id", sa.String(160), nullable=False),
        sa.Column("symbol_at_capture", sa.String(32), nullable=False),
        sa.Column("asset_class", sa.String(24), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "asset_class IN ('COMMON_STOCK','ETF')", name="ck_historical_instrument_asset_class"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_historical_instrument_authority"),
    )
    op.create_index(
        "ix_historical_instrument_stable_id",
        "historical_instrument_identity",
        ["stable_security_id"],
    )
    op.create_index(
        "ix_historical_instrument_symbol", "historical_instrument_identity", ["symbol_at_capture"]
    )

    op.create_table(
        "historical_capture_plan",
        sa.Column("plan_id", sa.String(160), primary_key=True),
        sa.Column("policy_id", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("start_date", sa.String(10), nullable=False),
        sa.Column("end_date", sa.String(10), nullable=False),
        sa.Column("evidence_class", sa.String(24), nullable=False),
        sa.Column("universe_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("operator_action_id", sa.String(160), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("idempotency_key", name="uq_historical_capture_plan_idempotency"),
        sa.CheckConstraint("universe_count > 0", name="ck_historical_plan_universe_count"),
        sa.CheckConstraint("chunk_count > 0", name="ck_historical_plan_chunk_count"),
        sa.CheckConstraint("start_date <= end_date", name="ck_historical_plan_date_range"),
        sa.CheckConstraint(
            "evidence_class IN "
            "('FIXTURE','GENUINE_PUBLIC','HISTORICAL_PUBLIC','SANDBOX','PAPER','LIVE')",
            name="ck_historical_plan_evidence_class",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_historical_plan_authority"),
    )
    op.create_index("ix_historical_plan_created_at", "historical_capture_plan", ["created_at"])

    op.create_table(
        "historical_capture_chunk",
        sa.Column("chunk_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey("historical_capture_plan.plan_id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("operation_id", sa.String(160), nullable=True),
        sa.Column(
            "batch_id",
            sa.String(160),
            sa.ForeignKey("securities_market_data_batch.batch_id"),
            nullable=True,
        ),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("plan_id", "ordinal", name="uq_historical_chunk_plan_ordinal"),
        sa.CheckConstraint("ordinal >= 0", name="ck_historical_chunk_ordinal"),
        sa.CheckConstraint("retry_count >= 0", name="ck_historical_chunk_retry_count"),
        sa.CheckConstraint(
            "state IN ('PENDING','RUNNING','COMPLETED','CREDENTIALS_REQUIRED','FAILED')",
            name="ck_historical_chunk_state",
        ),
        sa.CheckConstraint(
            "(state = 'COMPLETED') = (batch_id IS NOT NULL)",
            name="ck_historical_chunk_batch_consistency",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_historical_chunk_authority"),
    )
    op.create_index("ix_historical_chunk_plan", "historical_capture_chunk", ["plan_id"])
    op.create_index("ix_historical_chunk_state", "historical_capture_chunk", ["state"])

    op.create_table(
        "historical_dataset_snapshot",
        sa.Column("snapshot_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey("historical_capture_plan.plan_id"),
            nullable=False,
        ),
        sa.Column("evidence_class", sa.String(24), nullable=False),
        sa.Column("start_date", sa.String(10), nullable=False),
        sa.Column("end_date", sa.String(10), nullable=False),
        sa.Column("completeness_state", sa.String(16), nullable=False),
        sa.Column("point_in_time_universe_state", sa.String(48), nullable=False),
        sa.Column("survivorship_state", sa.String(32), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("observation_count >= 0", name="ck_historical_snapshot_obs_count"),
        sa.CheckConstraint(
            "completeness_state IN ('COMPLETE','PARTIAL')",
            name="ck_historical_snapshot_completeness",
        ),
        sa.CheckConstraint(
            "point_in_time_universe_state IN "
            "('POINT_IN_TIME_UNIVERSE_INCOMPLETE','POINT_IN_TIME_UNIVERSE_VERIFIED')",
            name="ck_historical_snapshot_pit_state",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_historical_snapshot_authority"),
    )
    op.create_index("ix_historical_snapshot_plan", "historical_dataset_snapshot", ["plan_id"])
    op.create_index(
        "ix_historical_snapshot_created_at", "historical_dataset_snapshot", ["created_at"]
    )

    op.create_table(
        "historical_data_quality_report",
        sa.Column("report_id", sa.String(160), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.String(160),
            sa.ForeignKey("historical_dataset_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("requested_security_count", sa.Integer(), nullable=False),
        sa.Column("captured_security_count", sa.Integer(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "requested_security_count >= captured_security_count",
            name="ck_historical_quality_counts",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_historical_quality_authority"),
    )
    op.create_index(
        "ix_historical_quality_snapshot", "historical_data_quality_report", ["snapshot_id"]
    )

    op.execute(sa.text(CHUNK_GUARD_DDL))
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0077 with retained historical market-data evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_historical_capture_chunk_trigger ON historical_capture_chunk"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_historical_capture_chunk()"))
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("historical_data_quality_report")
    op.drop_table("historical_dataset_snapshot")
    op.drop_table("historical_capture_chunk")
    op.drop_table("historical_capture_plan")
    op.drop_table("historical_instrument_identity")
