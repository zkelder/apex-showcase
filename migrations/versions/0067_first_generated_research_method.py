"""Retain the first generated research method selection, implementation, and runs.

Revision ID: 0067_first_generated_method
Revises: 0066_research_proposal_queue
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0067_first_generated_method"
down_revision: str | None = "0066_research_proposal_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "first_generated_research_method_selection",
    "first_generated_research_method_implementation",
    "first_generated_research_method_run",
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
    op.drop_constraint(
        "research_proposal_feasibility_proposal_id_key",
        "research_proposal_feasibility",
        type_="unique",
    )
    op.create_index(
        "ix_research_proposal_feasibility_latest",
        "research_proposal_feasibility",
        ["proposal_id", "evaluated_at", "feasibility_id"],
    )
    op.create_table(
        TABLES[0],
        sa.Column("selection_id", sa.String(300), primary_key=True),
        sa.Column(
            "generator_run_id",
            sa.String(300),
            sa.ForeignKey("research_proposal_generator_run.generator_run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "selected_proposal_id",
            sa.String(300),
            sa.ForeignKey("governed_research_proposal.proposal_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
    )
    op.create_table(
        TABLES[1],
        sa.Column("implementation_id", sa.String(300), primary_key=True),
        sa.Column(
            "selection_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[0]}.selection_id"),
            nullable=False,
            unique=True,
        ),
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
        sa.Column("method_run_id", sa.String(300), primary_key=True),
        sa.Column(
            "implementation_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[1]}.implementation_id"),
            nullable=False,
        ),
        sa.Column(
            "proposal_id",
            sa.String(300),
            sa.ForeignKey("governed_research_proposal.proposal_id"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
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
                "RAISE EXCEPTION 'cannot downgrade 0067 with first generated method evidence'; "
                "END IF; END $$"
            )
        )
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT proposal_id FROM research_proposal_feasibility "
            "GROUP BY proposal_id HAVING count(*) > 1) THEN "
            "RAISE EXCEPTION 'cannot downgrade 0067 with feasibility history'; "
            "END IF; END $$"
        )
    )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
    op.drop_index(
        "ix_research_proposal_feasibility_latest",
        table_name="research_proposal_feasibility",
    )
    op.create_unique_constraint(
        "research_proposal_feasibility_proposal_id_key",
        "research_proposal_feasibility",
        ["proposal_id"],
    )
