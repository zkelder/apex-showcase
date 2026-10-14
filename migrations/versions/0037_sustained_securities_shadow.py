"""Persist observation-only sustained Securities SHADOW evidence.
Revision ID: 0037_sustained_shadow
Revises: 0036_securities_ensemble
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0037_sustained_shadow"
down_revision: str | None = "0036_securities_ensemble"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "securities_sustained_shadow_plan",
        sa.Column("plan_id", sa.String(160), primary_key=True),
        sa.Column("universe_id", sa.String(160), nullable=False),
        sa.Column("universe_hash", sa.String(64), nullable=False),
        sa.Column("feature_schema_hash", sa.String(64), nullable=False),
        sa.Column(
            "intelligence_run_id",
            sa.String(160),
            sa.ForeignKey("securities_strategy_intelligence_run.run_id"),
            nullable=False,
        ),
        sa.Column(
            "ensemble_run_id",
            sa.String(160),
            sa.ForeignKey("securities_ensemble_run.run_id"),
            nullable=False,
        ),
        sa.Column("blockers", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("watermark", sa.String(160)),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "securities_sustained_shadow_prediction",
        sa.Column("prediction_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey("securities_sustained_shadow_plan.plan_id"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(160), nullable=False),
        sa.Column("instrument_id", sa.String(160), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "plan_id", "session_id", "instrument_id", name="uq_sustained_shadow_prediction_key"
        ),
    )
    op.create_table(
        "securities_sustained_shadow_outcome",
        sa.Column(
            "prediction_id",
            sa.String(160),
            sa.ForeignKey("securities_sustained_shadow_prediction.prediction_id"),
            primary_key=True,
        ),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
    )
    op.execute(sa.text(immutable_trigger_ddl("securities_sustained_shadow_prediction")))
    op.execute(sa.text(immutable_trigger_ddl("securities_sustained_shadow_outcome")))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM securities_sustained_shadow_prediction) "
            "THEN RAISE EXCEPTION 'cannot downgrade 0037 with retained sustained shadow evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_immutable_securities_sustained_shadow_outcome_trigger "
            "ON securities_sustained_shadow_outcome"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_immutable_securities_sustained_shadow_prediction_trigger "
            "ON securities_sustained_shadow_prediction"
        )
    )
    op.drop_table("securities_sustained_shadow_outcome")
    op.drop_table("securities_sustained_shadow_prediction")
    op.drop_table("securities_sustained_shadow_plan")
