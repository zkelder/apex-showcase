"""Persist bounded crypto market-pulse refresh and lineage evidence.

Revision ID: 0057_crypto_market_pulse
Revises: 0056_crypto_universe_quality
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0057_crypto_market_pulse"
down_revision: str | None = "0056_crypto_universe_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "crypto_market_pulse_plan",
    "crypto_market_pulse_refresh",
    "crypto_market_pulse_event",
    "crypto_market_pulse_checkpoint",
    "crypto_market_pulse_snapshot",
    "crypto_market_pulse_opportunity_lineage",
    "crypto_market_pulse_change_summary",
)


def _evidence_columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    )


def upgrade() -> None:
    op.create_table(
        "crypto_market_pulse_plan",
        sa.Column("plan_id", sa.String(160), primary_key=True),
        sa.Column(
            "universe_snapshot_id",
            sa.String(160),
            sa.ForeignKey("crypto_universe_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_pulse_plan_authority"),
    )
    op.create_table(
        "crypto_market_pulse_refresh",
        sa.Column("refresh_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey("crypto_market_pulse_plan.plan_id"),
            nullable=False,
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "universe_snapshot_id",
            sa.String(160),
            sa.ForeignKey("crypto_universe_snapshot.snapshot_id"),
            nullable=False,
        ),
        *_evidence_columns(),
        sa.UniqueConstraint("plan_id", "as_of", name="uq_crypto_pulse_plan_as_of"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_pulse_refresh_authority"),
    )
    op.create_table(
        "crypto_market_pulse_event",
        sa.Column("event_id", sa.String(160), primary_key=True),
        sa.Column(
            "refresh_id",
            sa.String(160),
            sa.ForeignKey("crypto_market_pulse_refresh.refresh_id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint("refresh_id", "ordinal", name="uq_crypto_pulse_event_ordinal"),
        sa.CheckConstraint("ordinal BETWEEN 1 AND 4", name="ck_crypto_pulse_event_ordinal"),
        sa.CheckConstraint(
            "state IN ('PLANNED','RUNNING','COMPLETED','PARTIAL','FAILED','SKIPPED_NOT_DUE')",
            name="ck_crypto_pulse_event_state",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_pulse_event_authority"),
    )
    op.create_table(
        "crypto_market_pulse_checkpoint",
        sa.Column("checkpoint_id", sa.String(160), primary_key=True),
        sa.Column(
            "refresh_id",
            sa.String(160),
            sa.ForeignKey("crypto_market_pulse_refresh.refresh_id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.String(160), nullable=False),
        sa.Column("quality", sa.String(40), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint("refresh_id", "ordinal", name="uq_crypto_pulse_checkpoint_ordinal"),
        sa.UniqueConstraint("refresh_id", "product_id", name="uq_crypto_pulse_checkpoint_product"),
        sa.CheckConstraint("ordinal BETWEEN 1 AND 10", name="ck_crypto_pulse_checkpoint_ordinal"),
        sa.CheckConstraint(
            "quality IN ('PASSED','FAILED')", name="ck_crypto_pulse_checkpoint_quality"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_pulse_checkpoint_authority"),
    )
    op.create_table(
        "crypto_market_pulse_snapshot",
        sa.Column("pulse_id", sa.String(160), primary_key=True),
        sa.Column(
            "refresh_id",
            sa.String(160),
            sa.ForeignKey("crypto_market_pulse_refresh.refresh_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "universe_snapshot_id",
            sa.String(160),
            sa.ForeignKey("crypto_universe_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("manifest_hash", sa.String(64), nullable=False, unique=True),
        *_evidence_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_pulse_snapshot_authority"),
    )
    op.create_table(
        "crypto_market_pulse_opportunity_lineage",
        sa.Column("lineage_id", sa.String(160), primary_key=True),
        sa.Column(
            "refresh_id",
            sa.String(160),
            sa.ForeignKey("crypto_market_pulse_refresh.refresh_id"),
            nullable=False,
        ),
        sa.Column("canonical_asset_id", sa.String(80), nullable=False),
        sa.Column("persistence", sa.String(40), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint(
            "refresh_id",
            "canonical_asset_id",
            name="uq_crypto_pulse_lineage_asset",
        ),
        sa.CheckConstraint(
            "persistence IN ('CURRENT_POSITIVE','PERSISTENT_POSITIVE','REVERSED',"
            "'INSUFFICIENT_HISTORY','DATA_QUALITY_BLOCKED')",
            name="ck_crypto_pulse_lineage_persistence",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_pulse_lineage_authority"),
    )
    op.create_table(
        "crypto_market_pulse_change_summary",
        sa.Column("summary_id", sa.String(160), primary_key=True),
        sa.Column(
            "refresh_id",
            sa.String(160),
            sa.ForeignKey("crypto_market_pulse_refresh.refresh_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "pulse_id",
            sa.String(160),
            sa.ForeignKey("crypto_market_pulse_snapshot.pulse_id"),
            nullable=False,
        ),
        *_evidence_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_pulse_summary_authority"),
    )
    for table in TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "
                "RAISE EXCEPTION "
                "'cannot downgrade 0057 with retained crypto market-pulse evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
