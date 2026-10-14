"""Persist immutable operator-governed genuine PAPER activation events.

Revision ID: 0074_paper_activation
Revises: 0073_live_preflight
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0074_paper_activation"
down_revision: str | None = "0073_live_preflight"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "apex_operating_paper_activation_event"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("event_id", sa.String(300), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(300),
            sa.ForeignKey("apex_operating_cycle_plan.plan_id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("plan_id", "ordinal"),
        sa.CheckConstraint("state IN ('ENABLED','DISABLED')"),
        sa.CheckConstraint("length(canonical_hash) = 64"),
        sa.CheckConstraint("authority = 'NONE'"),
    )
    op.execute(sa.text(immutable_trigger_ddl(TABLE)))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {TABLE}) THEN "  # nosec B608
            "RAISE EXCEPTION "
            "'cannot downgrade 0074 with retained PAPER activation evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(sa.text(f"DROP TRIGGER apex_immutable_{TABLE}_trigger ON {TABLE}"))
    op.drop_table(TABLE)
