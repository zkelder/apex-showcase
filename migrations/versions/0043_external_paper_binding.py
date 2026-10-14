"""Bind external PAPER instructions to pending internal Securities execution.

Revision ID: 0043_external_paper_binding
Revises: 0042_external_paper_fills
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_external_paper_binding"
down_revision: str | None = "0042_external_paper_fills"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_paper_internal_execution_binding",
        sa.Column("binding_id", sa.String(160), primary_key=True),
        sa.Column(
            "authorization_id",
            sa.String(160),
            sa.ForeignKey("external_paper_authorization.authorization_id"),
            nullable=False,
        ),
        sa.Column(
            "instruction_id",
            sa.String(160),
            sa.ForeignKey("external_paper_instruction.instruction_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("internal_order_id", sa.String(160), nullable=False, unique=True),
        sa.Column("risk_reservation_id", sa.String(160), nullable=False),
        sa.Column("state", sa.String(48), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM external_paper_internal_execution_binding) "
            "THEN RAISE EXCEPTION 'cannot downgrade 0043 with retained external PAPER binding'; "
            "END IF; END $$"
        )
    )
    op.drop_table("external_paper_internal_execution_binding")
