"""Persist local sustained-SHADOW schedule and operation jobs.
Revision ID: 0038_sustained_shadow_operations
Revises: 0037_sustained_shadow
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_sustained_shadow_operations"
down_revision: str | None = "0037_sustained_shadow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "securities_sustained_shadow_schedule_policy",
        sa.Column("policy_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey("securities_sustained_shadow_plan.plan_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "securities_sustained_shadow_operation_job",
        sa.Column("job_id", sa.String(160), primary_key=True),
        sa.Column(
            "policy_id",
            sa.String(160),
            sa.ForeignKey("securities_sustained_shadow_schedule_policy.policy_id"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(160), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("policy_id", "session_id", name="uq_sustained_shadow_operation_scope"),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM securities_sustained_shadow_operation_job) "
            "THEN RAISE EXCEPTION 'cannot downgrade 0038 with retained operation jobs'; "
            "END IF; END $$"
        )
    )
    op.drop_table("securities_sustained_shadow_operation_job")
    op.drop_table("securities_sustained_shadow_schedule_policy")
