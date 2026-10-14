"""Persist append-only Coinbase-compatible Crypto Spot PAPER evidence.

Revision ID: 0061_crypto_spot_paper
Revises: 0060_recurring_operating_cycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0061_crypto_spot_paper"
down_revision: str | None = "0060_recurring_operating_cycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "apex_crypto_paper_provider_contract",
    "apex_crypto_paper_capability_observation",
    "apex_crypto_paper_account",
    "apex_crypto_paper_state_snapshot",
    "apex_crypto_paper_eligibility_scan",
)


def _authority_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column(
            "provider_requests",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
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
        sa.CheckConstraint("provider_requests = 0"),
        sa.CheckConstraint("authority = 'PAPER_ONLY'"),
        sa.CheckConstraint("live_authority = 'NONE'"),
    )


def _payload_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("length(canonical_hash) = 64"),
    )


def upgrade() -> None:
    op.create_table(
        TABLES[0],
        sa.Column("contract_id", sa.String(300), primary_key=True),
        sa.Column("product_id", sa.String(300), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_payload_columns(),
        *_authority_columns(),
    )
    op.create_table(
        TABLES[1],
        sa.Column("capability_id", sa.String(300), primary_key=True),
        sa.Column(
            "contract_id",
            sa.String(300),
            sa.ForeignKey(f"{TABLES[0]}.contract_id"),
            nullable=True,
        ),
        sa.Column("inspected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_payload_columns(),
        *_authority_columns(),
    )
    op.create_table(
        TABLES[2],
        sa.Column("account_id", sa.String(300), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_payload_columns(),
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
        sa.CheckConstraint("authority = 'PAPER_ONLY'"),
        sa.CheckConstraint("live_authority = 'NONE'"),
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
        sa.Column("operation_type", sa.String(60), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("prior_state_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *_payload_columns(),
        *_authority_columns(),
        sa.UniqueConstraint("account_id", "sequence"),
        sa.CheckConstraint("sequence > 0"),
        sa.CheckConstraint("prior_state_hash IS NULL OR length(prior_state_hash) = 64"),
    )
    op.create_table(
        TABLES[4],
        sa.Column("scan_id", sa.String(300), primary_key=True),
        sa.Column("opportunity_set_id", sa.String(300), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_payload_columns(),
        *_authority_columns(),
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
                "'cannot downgrade 0061 with retained Crypto PAPER evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
