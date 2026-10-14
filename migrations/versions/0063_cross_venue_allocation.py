"""Persist authority-free cross-venue allocation evidence.

Revision ID: 0063_cross_venue_allocation
Revises: 0062_kalshi_operating_paper
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0063_cross_venue_allocation"
down_revision: str | None = "0062_kalshi_operating_paper"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "cross_venue_allocation_policy",
    "cross_venue_allocation_decision",
    "cross_venue_allocation_line",
    "cross_venue_constraint_explanation",
    "cross_venue_paper_trial_proposal",
)


def _evidence_columns() -> tuple[sa.SchemaItem, ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.Column("live_authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("length(canonical_hash) = 64"),
        sa.CheckConstraint("authority = 'NONE'"),
        sa.CheckConstraint("live_authority = 'NONE'"),
    )


def upgrade() -> None:
    op.create_table(
        TABLES[0],
        sa.Column("policy_id", sa.String(300), primary_key=True),
        *_evidence_columns(),
    )
    op.create_table(
        TABLES[1],
        sa.Column("decision_id", sa.String(300), primary_key=True),
        sa.Column(
            "policy_id", sa.String(300), sa.ForeignKey(f"{TABLES[0]}.policy_id"), nullable=False
        ),
        sa.Column(
            "enriched_set_id",
            sa.String(300),
            sa.ForeignKey("context_enriched_opportunity_set.enriched_set_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "opportunity_set_id",
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
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_evidence_columns(),
    )
    op.create_table(
        TABLES[2],
        sa.Column("line_id", sa.String(300), primary_key=True),
        sa.Column(
            "decision_id", sa.String(300), sa.ForeignKey(f"{TABLES[1]}.decision_id"), nullable=False
        ),
        sa.Column(
            "projection_id",
            sa.String(300),
            sa.ForeignKey("shared_opportunity_projection.projection_id"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(38, 8), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint("decision_id", "projection_id", name="uq_cross_venue_line_projection"),
        sa.CheckConstraint("amount > 0"),
    )
    op.create_table(
        TABLES[3],
        sa.Column("explanation_id", sa.String(300), primary_key=True),
        sa.Column(
            "decision_id", sa.String(300), sa.ForeignKey(f"{TABLES[1]}.decision_id"), nullable=False
        ),
        sa.Column(
            "projection_id",
            sa.String(300),
            sa.ForeignKey("shared_opportunity_projection.projection_id"),
            nullable=False,
        ),
        *_evidence_columns(),
        sa.UniqueConstraint(
            "decision_id", "projection_id", name="uq_cross_venue_explanation_projection"
        ),
    )
    op.create_table(
        TABLES[4],
        sa.Column("proposal_id", sa.String(300), primary_key=True),
        sa.Column(
            "decision_id", sa.String(300), sa.ForeignKey(f"{TABLES[1]}.decision_id"), nullable=False
        ),
        sa.Column(
            "line_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[2]}.line_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("venue", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint("venue IN ('CRYPTO', 'KALSHI')"),
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
                "'cannot downgrade 0063 with retained cross-venue allocation evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
