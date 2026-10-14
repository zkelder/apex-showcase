"""Persist approval-gated Kalshi private read-only evidence.

Revision ID: 0072_kalshi_read_only
Revises: 0071_coinbase_read_only
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.kalshi_read_only_postgres import (
    KALSHI_READ_ONLY_TABLES,
    kalshi_read_only_metadata,
)
from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0072_kalshi_read_only"
down_revision: str | None = "0071_coinbase_read_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    kalshi_read_only_metadata.create_all(bind=op.get_bind())
    for table in KALSHI_READ_ONLY_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(KALSHI_READ_ONLY_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION 'cannot downgrade 0072 with Kalshi read-only evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(KALSHI_READ_ONLY_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
