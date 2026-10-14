"""Persist bounded Kalshi operating and internal PAPER evidence.

Revision ID: 0062_kalshi_operating_paper
Revises: 0061_crypto_spot_paper
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0062_kalshi_operating_paper"
down_revision: str | None = "0061_crypto_spot_paper"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "apex_kalshi_capability_observation",
    "apex_kalshi_current_refresh",
    "apex_kalshi_paper_account",
    "apex_kalshi_paper_state_snapshot",
    "apex_kalshi_paper_eligibility_scan",
)


def _payload_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("length(canonical_hash) = 64"),
    )


def _authority_columns(*, provider: bool = True) -> tuple[sa.SchemaItem, ...]:
    values: list[sa.SchemaItem] = []
    if provider:
        values.extend(
            (
                sa.Column(
                    "provider_requests",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
                sa.Column(
                    "provider_writes",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
                sa.CheckConstraint("provider_requests >= 0"),
                sa.CheckConstraint("provider_writes = 0"),
            )
        )
    values.extend(
        (
            sa.Column(
                "authority",
                sa.String(16),
                nullable=False,
                server_default="PAPER_ONLY",
            ),
            sa.Column(
                "live_authority",
                sa.String(16),
                nullable=False,
                server_default="NONE",
            ),
            sa.CheckConstraint("authority IN ('NONE', 'PAPER_ONLY')"),
            sa.CheckConstraint("live_authority = 'NONE'"),
        )
    )
    return tuple(values)


def upgrade() -> None:
    op.create_table(
        TABLES[0],
        sa.Column("capability_id", sa.String(300), primary_key=True),
        sa.Column("inspected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_payload_columns(),
        *_authority_columns(),
        sa.CheckConstraint("provider_requests = 0"),
    )
    op.create_table(
        TABLES[1],
        sa.Column("refresh_id", sa.String(300), primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_class", sa.String(30), nullable=False),
        sa.Column("market_discovery_run_id", sa.String(300), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_payload_columns(),
        *_authority_columns(),
        sa.CheckConstraint("evidence_class IN ('FIXTURE', 'GENUINE_PUBLIC')"),
        sa.CheckConstraint("authority = 'NONE'"),
    )
    op.create_table(
        TABLES[2],
        sa.Column("account_id", sa.String(300), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opening_cash", sa.Numeric(38, 8), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_payload_columns(),
        *_authority_columns(provider=False),
        sa.CheckConstraint("opening_cash > 0"),
        sa.CheckConstraint("authority = 'PAPER_ONLY'"),
    )
    op.create_table(
        TABLES[3],
        sa.Column("snapshot_id", sa.String(300), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[2]}.account_id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("operation_type", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("prior_state_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *_payload_columns(),
        *_authority_columns(),
        sa.UniqueConstraint("account_id", "sequence"),
        sa.CheckConstraint("sequence > 0"),
        sa.CheckConstraint("provider_requests = 0"),
        sa.CheckConstraint("authority = 'PAPER_ONLY'"),
    )
    op.create_table(
        TABLES[4],
        sa.Column("scan_id", sa.String(300), primary_key=True),
        sa.Column("refresh_id", sa.String(300), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_payload_columns(),
        *_authority_columns(),
        sa.CheckConstraint("provider_requests = 0"),
        sa.CheckConstraint("authority = 'PAPER_ONLY'"),
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
                "'cannot downgrade 0062 with retained Kalshi operating evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
