"""Persist immutable governed research-operations evidence.

Revision ID: 0065_research_operations
Revises: 0064_performance_attribution
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0065_research_operations"
down_revision: str | None = "0064_performance_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "research_agenda_item",
    "governed_research_proposal",
    "governed_research_proposal_review",
    "governed_research_budget_event",
    "governed_research_recommendation",
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
        sa.Column("agenda_item_id", sa.String(300), primary_key=True),
        sa.Column("source_fact_id", sa.String(300), nullable=False, unique=True),
        sa.Column("venue", sa.String(30), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint("venue IN ('CRYPTO','KALSHI','CROSS_VENUE')"),
        sa.CheckConstraint("priority BETWEEN 1 AND 100"),
        sa.CheckConstraint("status IN ('OPEN','PROPOSED','DEFERRED','RESOLVED')"),
    )
    op.create_table(
        TABLES[1],
        sa.Column("proposal_id", sa.String(300), primary_key=True),
        sa.Column(
            "agenda_item_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[0]}.agenda_item_id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("venue", sa.String(30), nullable=False),
        sa.Column("origin", sa.String(30), nullable=False),
        sa.Column("semantic_hash", sa.String(64), nullable=False),
        sa.Column("configuration_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint("agenda_item_id", "version"),
        sa.CheckConstraint("version > 0"),
        sa.CheckConstraint("venue IN ('CRYPTO','KALSHI','CROSS_VENUE')"),
        sa.CheckConstraint("origin IN ('OPERATOR','SYSTEM_HEURISTIC','AI_ASSISTED','IMPORTED')"),
        sa.CheckConstraint("length(semantic_hash) = 64"),
        sa.CheckConstraint("length(configuration_hash) = 64"),
    )
    op.create_table(
        TABLES[2],
        sa.Column("review_id", sa.String(300), primary_key=True),
        sa.Column(
            "proposal_id", sa.String(300), sa.ForeignKey(f"{TABLES[1]}.proposal_id"), nullable=False
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("to_state", sa.String(40), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint("proposal_id", "ordinal"),
        sa.CheckConstraint("ordinal > 0"),
        sa.CheckConstraint(
            "to_state IN ('READY_FOR_REVIEW','APPROVED_FOR_EXPERIMENT','RUNNING',"
            "'COMPLETED','REJECTED','DEFERRED','DUPLICATE','INVALID')"
        ),
    )
    op.create_table(
        TABLES[3],
        sa.Column("event_id", sa.String(300), primary_key=True),
        sa.Column(
            "proposal_id", sa.String(300), sa.ForeignKey(f"{TABLES[1]}.proposal_id"), nullable=False
        ),
        sa.Column("run_id", sa.String(300), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("consumed_runs", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint("proposal_id", "ordinal"),
        sa.CheckConstraint("ordinal > 0"),
        sa.CheckConstraint("consumed_runs IN (0,1)"),
        sa.CheckConstraint(
            "event_type IN ('APPROVAL_RESERVED','EXPERIMENT_RUN','REPLAY','FAILED_RUN',"
            "'SUPERSEDED_DEFECT_RUN')"
        ),
    )
    op.create_table(
        TABLES[4],
        sa.Column("recommendation_id", sa.String(300), primary_key=True),
        sa.Column(
            "proposal_id", sa.String(300), sa.ForeignKey(f"{TABLES[1]}.proposal_id"), nullable=False
        ),
        sa.Column("run_id", sa.String(300), nullable=False, unique=True),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(
            "state IN ('REJECT','REVISE','REPLICATE','PAPER_CANDIDATE','INSUFFICIENT_EVIDENCE')"
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
                "'cannot downgrade 0065 with retained governed research evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
