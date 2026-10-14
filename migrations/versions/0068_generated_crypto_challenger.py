"""Retain generated Crypto refinement and reusable stateful research evidence.

Revision ID: 0068_generated_crypto_challenger
Revises: 0067_first_generated_method
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0068_generated_crypto_challenger"
down_revision: str | None = "0067_first_generated_method"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "research_proposal_refinement",
    "crypto_stateful_method_specification",
    "crypto_execution_history",
    "crypto_stateful_experiment_run",
    "crypto_stateful_current_signal",
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
        sa.Column("refinement_id", sa.String(300), primary_key=True),
        sa.Column(
            "original_proposal_id",
            sa.String(300),
            sa.ForeignKey("governed_research_proposal.proposal_id"),
            nullable=False,
        ),
        sa.Column(
            "refined_proposal_id",
            sa.String(300),
            sa.ForeignKey("governed_research_proposal.proposal_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("method_id", sa.String(300), nullable=False),
        sa.Column("refined_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
    )
    op.create_table(
        TABLES[1],
        sa.Column("specification_id", sa.String(300), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(300),
            sa.ForeignKey("governed_research_proposal.proposal_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("method_id", sa.String(300), nullable=False, unique=True),
        sa.Column("implemented_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
    )
    op.create_table(
        TABLES[2],
        sa.Column("history_id", sa.String(300), primary_key=True),
        sa.Column("product_id", sa.String(160), nullable=False),
        sa.Column(
            "source_observation_id",
            sa.String(300),
            sa.ForeignKey("crypto_product_observation.observation_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
    )
    op.create_table(
        TABLES[3],
        sa.Column("run_id", sa.String(300), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(300),
            sa.ForeignKey("governed_research_proposal.proposal_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "specification_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[1]}.specification_id"),
            nullable=False,
        ),
        sa.Column("dataset_id", sa.String(300), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
    )
    op.create_table(
        TABLES[4],
        sa.Column("signal_id", sa.String(300), primary_key=True),
        sa.Column("method_id", sa.String(300), nullable=False),
        sa.Column(
            "experiment_run_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[3]}.run_id"),
            nullable=False,
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.CheckConstraint("state IN ('CASH', 'LONG')"),
        *_evidence_columns(),
    )
    for table in TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "
                "RAISE EXCEPTION 'cannot downgrade 0068 with generated Crypto "
                "challenger evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
