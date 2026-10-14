"""Retain validated external PAPER fill lineage.

Revision ID: 0042_external_paper_fills
Revises: 0041_external_paper
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_external_paper_fills"
down_revision: str | None = "0041_external_paper"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_paper_validated_fill",
        sa.Column("validated_fill_id", sa.String(160), primary_key=True),
        sa.Column(
            "instruction_id",
            sa.String(160),
            sa.ForeignKey("external_paper_instruction.instruction_id"),
            nullable=False,
        ),
        sa.Column(
            "evidence_id",
            sa.String(160),
            sa.ForeignKey("external_paper_broker_evidence.evidence_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("state", sa.String(48), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "external_paper_fill_application",
        sa.Column(
            "validated_fill_id",
            sa.String(160),
            sa.ForeignKey("external_paper_validated_fill.validated_fill_id"),
            primary_key=True,
        ),
        sa.Column("internal_fill_id", sa.String(160), nullable=False, unique=True),
        sa.Column("internal_position_id", sa.String(160)),
        sa.Column("journal_ids", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in ("external_paper_validated_fill", "external_paper_fill_application"):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) "
                "THEN RAISE EXCEPTION "
                "'cannot downgrade 0042 with retained external PAPER fill evidence'; "
                "END IF; END $$"
            )
        )
    op.drop_table("external_paper_fill_application")
    op.drop_table("external_paper_validated_fill")
