"""Persist secure provider and LIVE-readiness evidence only.

Revision ID: 0069_secure_live_readiness
Revises: 0068_generated_crypto_challenger
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.live_readiness_postgres import (
    LIVE_READINESS_EVIDENCE_TABLES,
    live_readiness_metadata,
)
from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0069_secure_live_readiness"
down_revision: str | None = "0068_generated_crypto_challenger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    live_readiness_metadata.create_all(bind=op.get_bind())
    for table in LIVE_READINESS_EVIDENCE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(LIVE_READINESS_EVIDENCE_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION 'cannot downgrade 0069 with secure LIVE-readiness evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(LIVE_READINESS_EVIDENCE_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
