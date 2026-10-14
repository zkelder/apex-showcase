"""Persist bounded research proposal generation and experiment queue evidence.

Revision ID: 0066_research_proposal_queue
Revises: 0065_research_operations
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0066_research_proposal_queue"
down_revision: str | None = "0065_research_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "research_proposal_generator_run",
    "research_proposal_priority",
    "research_proposal_feasibility",
    "research_experiment_approval",
    "research_experiment_queue_item",
    "research_experiment_queue_event",
)


def _evidence_columns(*, experiment_authority: bool = False) -> tuple[sa.SchemaItem, ...]:
    authority = "EXPERIMENT_ONLY" if experiment_authority else "NONE"
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(32), nullable=False, server_default=authority),
        sa.Column("live_authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("length(canonical_hash) = 64"),
        sa.CheckConstraint(f"authority = '{authority}'"),
        sa.CheckConstraint("live_authority = 'NONE'"),
    )


def upgrade() -> None:
    op.create_table(
        TABLES[0],
        sa.Column("generator_run_id", sa.String(300), primary_key=True),
        sa.Column("generator_id", sa.String(100), nullable=False),
        sa.Column("policy_id", sa.String(100), nullable=False),
        sa.Column("origin", sa.String(30), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint("generator_id = 'RESEARCH_PROPOSAL_GENERATOR_V1'"),
        sa.CheckConstraint("policy_id = 'RESEARCH_PROPOSAL_GENERATION_POLICY_V1'"),
        sa.CheckConstraint("origin IN ('SYSTEM_HEURISTIC','AI_ASSISTED')"),
    )
    op.create_table(
        TABLES[1],
        sa.Column("priority_id", sa.String(300), primary_key=True),
        sa.Column("generator_run_id", sa.String(300), nullable=False),
        sa.Column(
            "proposal_id",
            sa.String(300),
            sa.ForeignKey("governed_research_proposal.proposal_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("proposal_class", sa.String(40), nullable=False),
        sa.Column("total_score", sa.Integer(), nullable=False),
        sa.Column("deterministic_rank", sa.Integer(), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(
            "proposal_class IN ('EVIDENCE_COMPLETION','REPLICATION',"
            "'MATERIAL_CHALLENGER','OPERATIONAL_METHOD')"
        ),
        sa.CheckConstraint("total_score BETWEEN 0 AND 100"),
        sa.CheckConstraint("deterministic_rank BETWEEN 1 AND 3"),
    )
    op.create_table(
        TABLES[2],
        sa.Column("feasibility_id", sa.String(300), primary_key=True),
        sa.Column("generator_run_id", sa.String(300), nullable=False),
        sa.Column(
            "proposal_id",
            sa.String(300),
            sa.ForeignKey("governed_research_proposal.proposal_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(
            "status IN ('READY_FOR_REVIEW','DUPLICATE','REQUIRES_EVIDENCE',"
            "'REQUIRES_IMPLEMENTATION','BUDGET_BLOCKED','INVALID')"
        ),
    )
    op.create_table(
        TABLES[3],
        sa.Column("approval_id", sa.String(300), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(300),
            sa.ForeignKey("governed_research_proposal.proposal_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "review_id",
            sa.String(300),
            sa.ForeignKey("governed_research_proposal_review.review_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(experiment_authority=True),
    )
    op.create_table(
        TABLES[4],
        sa.Column("queue_id", sa.String(300), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(300),
            sa.ForeignKey("governed_research_proposal.proposal_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "approval_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[3]}.approval_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("run_id", sa.String(300), nullable=False, unique=True),
        sa.Column("execution_order", sa.Integer(), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(experiment_authority=True),
        sa.CheckConstraint("execution_order BETWEEN 1 AND 3"),
    )
    op.create_table(
        TABLES[5],
        sa.Column("event_id", sa.String(300), primary_key=True),
        sa.Column(
            "queue_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[4]}.queue_id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("to_status", sa.String(30), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(experiment_authority=True),
        sa.UniqueConstraint("queue_id", "ordinal"),
        sa.CheckConstraint("ordinal > 0"),
        sa.CheckConstraint(
            "to_status IN ('QUEUED','RUNNING','COMPLETED','FAILED','BLOCKED','CANCELED')"
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
                "'cannot downgrade 0066 with retained research proposal queue evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
