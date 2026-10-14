"""Add immutable parent lineage to new sustained-SHADOW plan versions.
Revision ID: 0039_shadow_lineage
Revises: 0038_sustained_shadow_operations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_shadow_lineage"
down_revision: str | None = "0038_sustained_shadow_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "securities_sustained_shadow_plan",
        sa.Column("parent_plan_id", sa.String(160), nullable=True),
    )
    op.create_foreign_key(
        "fk_sustained_shadow_plan_parent",
        "securities_sustained_shadow_plan",
        "securities_sustained_shadow_plan",
        ["parent_plan_id"],
        ["plan_id"],
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM securities_sustained_shadow_plan "
            "WHERE parent_plan_id IS NOT NULL) THEN RAISE EXCEPTION "
            "'cannot downgrade 0039 with retained plan lineage'; END IF; END $$"
        )
    )
    op.drop_constraint("fk_sustained_shadow_plan_parent", "securities_sustained_shadow_plan")
    op.drop_column("securities_sustained_shadow_plan", "parent_plan_id")
