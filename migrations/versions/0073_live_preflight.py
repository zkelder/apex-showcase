"""Persist immutable non-executing LIVE-intent preflight evidence.

Revision ID: 0073_live_preflight
Revises: 0072_kalshi_read_only
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.live_preflight_postgres import (
    LIVE_PREFLIGHT_TABLES,
    live_preflight_metadata,
)
from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0073_live_preflight"
down_revision: str | None = "0072_kalshi_read_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    live_preflight_metadata.create_all(bind=op.get_bind())
    for table in LIVE_PREFLIGHT_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(LIVE_PREFLIGHT_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION 'cannot downgrade 0073 with LIVE preflight evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(LIVE_PREFLIGHT_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
