"""Persist bounded operational reliability evidence only.

Revision ID: 0070_operational_reliability
Revises: 0069_secure_live_readiness
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.operational_reliability_postgres import (
    OPERATIONAL_RELIABILITY_TABLES,
    operational_reliability_metadata,
)
from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0070_operational_reliability"
down_revision: str | None = "0069_secure_live_readiness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    operational_reliability_metadata.create_all(bind=op.get_bind())
    for table in OPERATIONAL_RELIABILITY_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(OPERATIONAL_RELIABILITY_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION 'cannot downgrade 0070 with operational reliability evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(OPERATIONAL_RELIABILITY_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
