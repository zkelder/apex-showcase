"""Persist research-only Strategy Lab SHADOW deployment evidence.

Revision ID: 0024_strategy_lab_shadow
Revises: 0023_strategy_lab_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    STRATEGY_LAB_SHADOW_IMMUTABLE_TABLES,
    STRATEGY_LAB_SHADOW_TABLES,
    immutable_trigger_ddl,
    strategy_lab_metadata,
)

revision: str = "0024_strategy_lab_shadow"
down_revision: str | None = "0023_strategy_lab_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    strategy_lab_metadata.create_all(bind=op.get_bind(), tables=STRATEGY_LAB_SHADOW_TABLES)
    for table_name in STRATEGY_LAB_SHADOW_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))


def downgrade() -> None:
    """Retained SHADOW research evidence must never be silently discarded."""
    checks = " OR ".join(
        f"EXISTS (SELECT 1 FROM {table.name})" for table in STRATEGY_LAB_SHADOW_TABLES
    )
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0024 with retained Strategy Lab SHADOW evidence'; "
            "END IF; END $$"
        )
    )
    for table_name in STRATEGY_LAB_SHADOW_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    strategy_lab_metadata.drop_all(bind=op.get_bind(), tables=STRATEGY_LAB_SHADOW_TABLES)
