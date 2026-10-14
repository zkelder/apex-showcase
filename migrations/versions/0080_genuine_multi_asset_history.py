"""Genuine multi-asset historical evidence bring-up: a per-instrument
genuine cohort data-quality gate report, and the cross-market dataset
binding that ties the genuine equity/ETF cohort, existing genuine BTC
historical evidence, and the equity-derived historical Total Market
Context series to one immutable, addressable identity.

Both tables added here hold genuinely new facts (per-instrument
classification with reasons; several independent coverage windows plus an
explicit alignment-policy reference) that cannot be represented faithfully
in any existing table -- see ``domain.genuine_data_quality_gate`` and
``domain.cross_market_dataset``. No existing table from ``0079`` or earlier
is altered.

Revision ID: 0080_genuine_multi_asset_history
Revises: 0079_quant_research_methodology
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0080_genuine_multi_asset_history"
down_revision: str | None = "0079_quant_research_methodology"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "genuine_cohort_quality_gate_report",
    "cross_market_dataset",
)
ALL_TABLES = IMMUTABLE_TABLES


def upgrade() -> None:
    op.create_table(
        "genuine_cohort_quality_gate_report",
        sa.Column("report_id", sa.String(160), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.String(160),
            sa.ForeignKey("historical_dataset_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("cohort_policy_id", sa.String(160), nullable=False),
        sa.Column("requested_instrument_count", sa.Integer(), nullable=False),
        sa.Column("successful_instrument_count", sa.Integer(), nullable=False),
        sa.Column("usable_count", sa.Integer(), nullable=False),
        sa.Column("usable_with_warnings_count", sa.Integer(), nullable=False),
        sa.Column("insufficient_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("requested_instrument_count >= 0", name="ck_gcqgr_requested_count"),
        sa.CheckConstraint("successful_instrument_count >= 0", name="ck_gcqgr_successful_count"),
        sa.CheckConstraint(
            "usable_count >= 0 AND usable_with_warnings_count >= 0 "
            "AND insufficient_count >= 0 AND rejected_count >= 0",
            name="ck_gcqgr_classification_counts",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_gcqgr_authority"),
    )
    op.create_index(
        "ix_genuine_cohort_quality_gate_report_snapshot",
        "genuine_cohort_quality_gate_report",
        ["snapshot_id"],
    )
    op.create_index(
        "ix_genuine_cohort_quality_gate_report_policy",
        "genuine_cohort_quality_gate_report",
        ["cohort_policy_id"],
    )

    op.create_table(
        "cross_market_dataset",
        sa.Column("cross_market_dataset_id", sa.String(160), primary_key=True),
        sa.Column(
            "equity_dataset_snapshot_id",
            sa.String(160),
            sa.ForeignKey("historical_dataset_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("cohort_policy_id", sa.String(160), nullable=False),
        sa.Column(
            "quality_gate_report_id",
            sa.String(160),
            sa.ForeignKey("genuine_cohort_quality_gate_report.report_id"),
            nullable=False,
        ),
        sa.Column("crypto_provider", sa.String(40), nullable=False),
        sa.Column("crypto_product_id", sa.String(40), nullable=False),
        sa.Column("equity_coverage_start", sa.String(10), nullable=False),
        sa.Column("equity_coverage_end", sa.String(10), nullable=False),
        sa.Column("crypto_coverage_start", sa.String(10), nullable=False),
        sa.Column("crypto_coverage_end", sa.String(10), nullable=False),
        sa.Column("common_overlap_start", sa.String(10), nullable=True),
        sa.Column("common_overlap_end", sa.String(10), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "equity_coverage_start <= equity_coverage_end", name="ck_cmd_equity_range"
        ),
        sa.CheckConstraint(
            "crypto_coverage_start <= crypto_coverage_end", name="ck_cmd_crypto_range"
        ),
        sa.CheckConstraint(
            "(common_overlap_start IS NULL) = (common_overlap_end IS NULL)",
            name="ck_cmd_overlap_pair",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmd_authority"),
    )
    op.create_index(
        "ix_cross_market_dataset_equity_snapshot",
        "cross_market_dataset",
        ["equity_dataset_snapshot_id"],
    )
    op.create_index(
        "ix_cross_market_dataset_quality_gate_report",
        "cross_market_dataset",
        ["quality_gate_report_id"],
    )

    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0080 with retained genuine multi-asset history evidence'; "
            "END IF; END $$"
        )
    )
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("cross_market_dataset")
    op.drop_table("genuine_cohort_quality_gate_report")
