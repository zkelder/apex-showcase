"""Persist continuous-market securities research and SHADOW evidence.

Revision ID: 0025_securities_research_shadow
Revises: 0024_strategy_lab_shadow
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    SECURITIES_IMMUTABLE_TABLES,
    SECURITIES_TABLES,
    immutable_trigger_ddl,
    securities_metadata,
)

revision: str = "0025_securities_research_shadow"
down_revision: str | None = "0024_strategy_lab_shadow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    securities_metadata.create_all(
        bind=op.get_bind(),
        tables=SECURITIES_TABLES,
        checkfirst=False,
    )
    for table_name in SECURITIES_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))


def downgrade() -> None:
    """Never discard retained Securities Stage 1 evidence."""

    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table.name}")' for table in SECURITIES_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0025 with retained Securities research evidence'; "
            "END IF; END $$"
        )
    )
    for table_name in SECURITIES_IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table_name}_trigger ON "{table_name}"'))
    securities_metadata.drop_all(bind=op.get_bind(), tables=SECURITIES_TABLES)
