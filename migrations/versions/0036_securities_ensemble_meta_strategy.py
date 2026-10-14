"""Persist immutable research-only Securities ensemble runs.

Revision ID: 0036_securities_ensemble
Revises: 0035_strategy_intelligence
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0036_securities_ensemble"
down_revision: str | None = "0035_strategy_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "securities_ensemble_run",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column(
            "intelligence_run_id",
            sa.String(160),
            sa.ForeignKey("securities_strategy_intelligence_run.run_id"),
            nullable=False,
        ),
        sa.Column("artifacts", sa.Text(), nullable=False),
        sa.Column("blockers", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("canonical_input_hash", sa.String(64), nullable=False),
        sa.Column("canonical_result_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(canonical_input_hash) = 64 AND length(canonical_result_hash) = 64",
            name="ck_securities_ensemble_hashes",
        ),
    )
    op.execute(sa.text(immutable_trigger_ddl("securities_ensemble_run")))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM securities_ensemble_run) "
            "THEN RAISE EXCEPTION 'cannot downgrade 0036 with retained ensemble evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_immutable_securities_ensemble_run_trigger ON securities_ensemble_run"
        )
    )
    op.drop_table("securities_ensemble_run")
