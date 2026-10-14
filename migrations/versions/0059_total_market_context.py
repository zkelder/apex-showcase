"""Persist explanatory Total Market context and Opportunity lineage.

Revision ID: 0059_total_market_context
Revises: 0058_shared_opportunity
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0059_total_market_context"
down_revision: str | None = "0058_shared_opportunity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "total_market_context_component",
    "total_market_context_snapshot",
    "context_enriched_opportunity_set",
    "opportunity_context_attachment",
)


def _evidence_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    )


def upgrade() -> None:
    op.create_table(
        "total_market_context_component",
        sa.Column("component_id", sa.String(300), primary_key=True),
        sa.Column("component_type", sa.String(50), nullable=False),
        sa.Column("source_observation_id", sa.String(300), nullable=False),
        sa.Column("source_batch_id", sa.String(160), nullable=False),
        sa.Column("observation_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint(
            "component_type",
            "source_observation_id",
            name="uq_total_market_component_source",
        ),
        sa.CheckConstraint(
            "component_type IN ('US_EQUITY_MARKET','TREASURY_RATE','YIELD_CURVE',"
            "'MARKET_VOLATILITY','US_DOLLAR','COMMODITY_OR_INFLATION',"
            "'MACRO_EVENT_PROXIMITY')",
            name="ck_total_market_component_type",
        ),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_total_market_component_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_total_market_component_authority"),
    )
    op.create_table(
        "total_market_context_snapshot",
        sa.Column("snapshot_id", sa.String(300), primary_key=True),
        sa.Column("source_batch_id", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_evidence_columns(),
        sa.CheckConstraint(
            "length(manifest_hash) = 64 AND length(canonical_hash) = 64",
            name="ck_total_market_snapshot_hashes",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_total_market_snapshot_authority"),
    )
    op.create_table(
        "context_enriched_opportunity_set",
        sa.Column("enriched_set_id", sa.String(300), primary_key=True),
        sa.Column(
            "original_opportunity_set_id",
            sa.String(300),
            sa.ForeignKey("shared_opportunity_set.opportunity_set_id"),
            nullable=False,
        ),
        sa.Column(
            "context_snapshot_id",
            sa.String(300),
            sa.ForeignKey("total_market_context_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_evidence_columns(),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_context_enriched_set_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_context_enriched_set_authority"),
    )
    op.create_table(
        "opportunity_context_attachment",
        sa.Column("attachment_id", sa.String(300), primary_key=True),
        sa.Column(
            "enriched_set_id",
            sa.String(300),
            sa.ForeignKey("context_enriched_opportunity_set.enriched_set_id"),
            nullable=False,
        ),
        sa.Column(
            "projection_id",
            sa.String(300),
            sa.ForeignKey("shared_opportunity_projection.projection_id"),
            nullable=False,
        ),
        sa.Column(
            "context_snapshot_id",
            sa.String(300),
            sa.ForeignKey("total_market_context_snapshot.snapshot_id"),
            nullable=False,
        ),
        *_evidence_columns(),
        sa.UniqueConstraint(
            "enriched_set_id",
            "projection_id",
            name="uq_opportunity_context_enriched_projection",
        ),
        sa.CheckConstraint(
            "length(canonical_hash) = 64", name="ck_opportunity_context_attachment_hash"
        ),
        sa.CheckConstraint(
            "authority = 'NONE'", name="ck_opportunity_context_attachment_authority"
        ),
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
                "'cannot downgrade 0059 with retained Total Market context evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
