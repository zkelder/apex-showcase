"""Persist canonical Securities baselines and multi-simulator evidence.

Revision ID: 0027_securities_baselines
Revises: 0026_securities_paper_execution
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    SECURITIES_BASELINE_IMMUTABLE_TABLES,
    SECURITIES_BASELINE_TABLES,
    immutable_trigger_ddl,
    securities_baseline_metadata,
)

revision: str = "0027_securities_baselines"
down_revision: str | None = "0026_securities_paper_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    securities_baseline_metadata.create_all(
        bind=op.get_bind(),
        tables=SECURITIES_BASELINE_TABLES,
        checkfirst=False,
    )
    for table_name in SECURITIES_BASELINE_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))


def downgrade() -> None:
    """Refuse before DDL whenever retained canonical baseline evidence exists."""

    checks = " OR ".join(
        f'EXISTS (SELECT 1 FROM "{table.name}")' for table in SECURITIES_BASELINE_TABLES
    )
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0027 with retained canonical Securities baseline "
            "or simulator evidence'; END IF; END $$"
        )
    )
    for table_name in reversed(SECURITIES_BASELINE_IMMUTABLE_TABLES):
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table_name}_trigger ON "{table_name}"'))
    securities_baseline_metadata.drop_all(
        bind=op.get_bind(),
        tables=SECURITIES_BASELINE_TABLES,
    )
