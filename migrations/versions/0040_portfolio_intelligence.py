"""Persist immutable research-only Portfolio Intelligence runs.
Revision ID: 0040_portfolio_intelligence
Revises: 0039_shadow_lineage
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_portfolio_intelligence"
down_revision: str | Sequence[str] | None = "0039_shadow_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "apex_portfolio_intelligence_run",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM apex_portfolio_intelligence_run) "
            "THEN RAISE EXCEPTION 'cannot downgrade 0040 with retained Portfolio Intelligence'; "
            "END IF; END $$"
        )
    )
    op.drop_table("apex_portfolio_intelligence_run")
