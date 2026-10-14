"""Persist the authority-free shared Opportunity operating layer.

Revision ID: 0058_shared_opportunity
Revises: 0057_crypto_market_pulse
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0058_shared_opportunity"
down_revision: str | None = "0057_crypto_market_pulse"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "shared_opportunity_projection",
    "shared_opportunity_set",
    "shared_opportunity_priority",
    "shared_opportunity_explanation",
    "shared_research_allocation_proposal",
)


def _evidence_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    )


def upgrade() -> None:
    op.create_table(
        "shared_opportunity_projection",
        sa.Column("projection_id", sa.String(300), primary_key=True),
        sa.Column("venue", sa.String(20), nullable=False),
        sa.Column("source_record_type", sa.String(160), nullable=False),
        sa.Column("source_record_id", sa.String(300), nullable=False),
        sa.Column("source_canonical_hash", sa.String(64), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint(
            "source_record_type",
            "source_record_id",
            name="uq_shared_opportunity_source_identity",
        ),
        sa.CheckConstraint("venue IN ('CRYPTO','KALSHI')", name="ck_shared_projection_venue"),
        sa.CheckConstraint(
            "length(source_canonical_hash) = 64 AND length(canonical_hash) = 64",
            name="ck_shared_projection_hashes",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_shared_projection_authority"),
    )
    op.create_table(
        "shared_opportunity_set",
        sa.Column("opportunity_set_id", sa.String(300), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("eligibility_payload", sa.Text(), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(
            "length(manifest_hash) = 64 AND length(canonical_hash) = 64",
            name="ck_shared_set_hashes",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_shared_set_authority"),
    )
    op.create_table(
        "shared_opportunity_priority",
        sa.Column("priority_id", sa.String(300), primary_key=True),
        sa.Column(
            "opportunity_set_id",
            sa.String(300),
            sa.ForeignKey("shared_opportunity_set.opportunity_set_id"),
            nullable=False,
        ),
        sa.Column(
            "projection_id",
            sa.String(300),
            sa.ForeignKey("shared_opportunity_projection.projection_id"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint(
            "opportunity_set_id", "projection_id", name="uq_shared_priority_set_projection"
        ),
        sa.UniqueConstraint("opportunity_set_id", "rank", name="uq_shared_priority_set_rank"),
        sa.CheckConstraint("rank > 0", name="ck_shared_priority_rank"),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_shared_priority_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_shared_priority_authority"),
    )
    op.create_table(
        "shared_opportunity_explanation",
        sa.Column("explanation_id", sa.String(300), primary_key=True),
        sa.Column(
            "opportunity_set_id",
            sa.String(300),
            sa.ForeignKey("shared_opportunity_set.opportunity_set_id"),
            nullable=False,
        ),
        sa.Column(
            "projection_id",
            sa.String(300),
            sa.ForeignKey("shared_opportunity_projection.projection_id"),
            nullable=False,
        ),
        *_evidence_columns(),
        sa.UniqueConstraint(
            "opportunity_set_id",
            "projection_id",
            name="uq_shared_explanation_set_projection",
        ),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_shared_explanation_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_shared_explanation_authority"),
    )
    op.create_table(
        "shared_research_allocation_proposal",
        sa.Column("proposal_id", sa.String(300), primary_key=True),
        sa.Column(
            "opportunity_set_id",
            sa.String(300),
            sa.ForeignKey("shared_opportunity_set.opportunity_set_id"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_evidence_columns(),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_shared_allocation_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_shared_allocation_authority"),
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
                "'cannot downgrade 0058 with retained shared Opportunity evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
