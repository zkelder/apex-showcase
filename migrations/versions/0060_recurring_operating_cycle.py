"""Persist immutable research-only recurring operating-cycle evidence.

Revision ID: 0060_recurring_operating_cycle
Revises: 0059_total_market_context
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0060_recurring_operating_cycle"
down_revision: str | None = "0059_total_market_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "apex_operating_cycle_plan",
    "apex_operating_control_event",
    "apex_operating_cycle_run",
    "apex_operating_cycle_event",
    "apex_operating_task_result",
    "apex_operating_brief",
    "apex_operating_health",
)


def _evidence_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("length(canonical_hash) = 64"),
        sa.CheckConstraint("authority = 'NONE'"),
    )


def upgrade() -> None:
    op.create_table(
        TABLES[0],
        sa.Column("plan_id", sa.String(300), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_evidence_columns(),
    )
    op.create_table(
        TABLES[1],
        sa.Column("event_id", sa.String(300), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[0]}.plan_id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_evidence_columns(),
        sa.UniqueConstraint("plan_id", "ordinal"),
        sa.CheckConstraint("state IN ('ENABLED','PAUSED')"),
    )
    op.create_table(
        TABLES[2],
        sa.Column("cycle_id", sa.String(300), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[0]}.plan_id"),
            nullable=False,
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_work_payload", sa.Text(), nullable=False),
        sa.Column("due_work_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_evidence_columns(),
        sa.UniqueConstraint("plan_id", "as_of"),
        sa.CheckConstraint("length(due_work_hash) = 64"),
    )
    op.create_table(
        TABLES[3],
        sa.Column("event_id", sa.String(300), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[2]}.cycle_id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint("cycle_id", "ordinal"),
    )
    op.create_table(
        TABLES[4],
        sa.Column("task_result_id", sa.String(300), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[2]}.cycle_id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("task_type", sa.String(60), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint("cycle_id", "ordinal"),
    )
    op.create_table(
        TABLES[5],
        sa.Column("brief_id", sa.String(300), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[2]}.cycle_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
    )
    op.create_table(
        TABLES[6],
        sa.Column("health_id", sa.String(300), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[2]}.cycle_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        *_evidence_columns(),
    )
    for table in TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "
                "RAISE EXCEPTION "
                "'cannot downgrade 0060 with retained operating-cycle evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
