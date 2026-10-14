"""Persist shared PAPER attribution and strategy-health evidence.

Revision ID: 0064_performance_attribution
Revises: 0063_cross_venue_allocation
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0064_performance_attribution"
down_revision: str | None = "0063_cross_venue_allocation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "shared_performance_policy",
    "shared_performance_attribution",
    "shared_performance_component",
    "strategy_health_assessment",
    "paper_venue_performance_summary",
    "paper_portfolio_performance_summary",
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
        sa.Column("attribution_id", sa.String(300), primary_key=True),
        sa.Column("policy_id", sa.String(300), nullable=False),
        sa.Column("source_id", sa.String(300), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("venue", sa.String(40), nullable=False),
        sa.Column("evidence_class", sa.String(24), nullable=False),
        sa.Column("strategy_or_method_id", sa.String(300), nullable=False),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(240), nullable=False, unique=True),
        *_evidence_columns(),
        sa.UniqueConstraint("source_id", "source_hash", "evidence_class"),
        sa.CheckConstraint(
            "venue IN ('CRYPTO_INTERNAL_PAPER','KALSHI_INTERNAL_PAPER','ALPACA_EXTERNAL_PAPER')"
        ),
        sa.CheckConstraint("evidence_class IN ('GENUINE_PAPER','FIXTURE_PAPER')"),
        sa.CheckConstraint("length(source_hash) = 64"),
    )
    op.create_table(
        TABLES[2],
        sa.Column("component_id", sa.String(300), primary_key=True),
        sa.Column(
            "attribution_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[1]}.attribution_id"),
            nullable=False,
        ),
        sa.Column("category", sa.String(40), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint("attribution_id", "category", "component_id"),
    )
    op.create_table(
        TABLES[3],
        sa.Column("health_id", sa.String(300), primary_key=True),
        sa.Column("policy_id", sa.String(300), nullable=False),
        sa.Column("strategy_or_method_id", sa.String(300), nullable=False),
        sa.Column("strategy_or_method_version", sa.String(300), nullable=False),
        sa.Column("evidence_class", sa.String(24), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(240), nullable=False, unique=True),
        *_evidence_columns(),
        sa.CheckConstraint("evidence_class IN ('GENUINE_PAPER','FIXTURE_PAPER')"),
    )
    for table_name, scope_check in (
        (TABLES[4], "scope_type = 'VENUE'"),
        (TABLES[5], "scope_type IN ('PORTFOLIO','STRATEGY_OR_METHOD')"),
    ):
        op.create_table(
            table_name,
            sa.Column("summary_id", sa.String(300), primary_key=True),
            sa.Column("scope_type", sa.String(20), nullable=False),
            sa.Column("scope_id", sa.String(300), nullable=False),
            sa.Column("evidence_class", sa.String(24), nullable=False),
            sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
            sa.Column("idempotency_key", sa.String(240), nullable=False, unique=True),
            *_evidence_columns(),
            sa.CheckConstraint(scope_check),
            sa.CheckConstraint("evidence_class IN ('GENUINE_PAPER','FIXTURE_PAPER')"),
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
                "'cannot downgrade 0064 with retained performance attribution evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
