"""Normalized Portfolio State + Investment Mandate v1.

Persists provider-neutral normalized portfolio truth (``portfolio_account_
balance``, ``portfolio_position``, ``portfolio_snapshot`` -- computed only
from already-retained Crypto Spot PAPER, Kalshi PAPER, Securities internal
PAPER, and optionally already-retrieved genuine External PAPER (Alpaca)
evidence; never inferred, never a provider write) and a durable, versioned
Investment Mandate lineage plus its deterministic evaluation against a
retained Opportunity (``investment_mandate``, ``mandate_evaluation``).

Every row carries ``authority = 'NONE'``: this milestone adds no trading
authority, no order path, and no LIVE eligibility. All five tables are
append-only/immutable via the existing trigger convention; downgrade
refuses to run while any retained row exists.

Revision ID: 0092_portfolio_mandate
Revises: 0091_corporate_regime_followup
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0092_portfolio_mandate"
down_revision: str | None = "0091_corporate_regime_followup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PORTFOLIO_VENUES = "'COINBASE','KALSHI','ALPACA','APEX_MANAGED_SECURITIES_DATA'"
_ACCOUNT_KINDS = "'APEX_INTERNAL_PAPER_SIMULATION','PROVIDER_PAPER_BROKER'"

PORTFOLIO_STATE_TABLES = (
    "portfolio_account_balance",
    "portfolio_position",
    "portfolio_snapshot",
)
INVESTMENT_MANDATE_TABLES = (
    "investment_mandate",
    "mandate_evaluation",
)
ALL_TABLES = (*PORTFOLIO_STATE_TABLES, *INVESTMENT_MANDATE_TABLES)


def _evidence_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    )


def upgrade() -> None:
    op.create_table(
        "portfolio_account_balance",
        sa.Column("balance_id", sa.String(300), primary_key=True),
        sa.Column("venue", sa.String(40), nullable=False),
        sa.Column("account_kind", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(300), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(f"venue IN ({_PORTFOLIO_VENUES})", name="ck_portfolio_balance_venue"),
        sa.CheckConstraint(
            f"account_kind IN ({_ACCOUNT_KINDS})", name="ck_portfolio_balance_account_kind"
        ),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_portfolio_balance_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_portfolio_balance_authority"),
    )
    op.create_table(
        "portfolio_position",
        sa.Column("position_id", sa.String(300), primary_key=True),
        sa.Column("venue", sa.String(40), nullable=False),
        sa.Column("account_kind", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(300), nullable=False),
        sa.Column("instrument_id", sa.String(300), nullable=False),
        sa.Column("market_family", sa.String(60), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(f"venue IN ({_PORTFOLIO_VENUES})", name="ck_portfolio_position_venue"),
        sa.CheckConstraint(
            f"account_kind IN ({_ACCOUNT_KINDS})", name="ck_portfolio_position_account_kind"
        ),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_portfolio_position_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_portfolio_position_authority"),
    )
    op.create_table(
        "portfolio_snapshot",
        sa.Column("snapshot_id", sa.String(300), primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completeness", sa.String(40), nullable=False),
        sa.Column("source_manifest_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_evidence_columns(),
        sa.CheckConstraint(
            "completeness IN ('COMPLETE','PARTIAL_UNKNOWN_SOURCES','ALL_SOURCES_UNKNOWN')",
            name="ck_portfolio_snapshot_completeness",
        ),
        sa.CheckConstraint(
            "length(source_manifest_hash) = 64 AND length(canonical_hash) = 64",
            name="ck_portfolio_snapshot_hashes",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_portfolio_snapshot_authority"),
    )
    op.create_table(
        "investment_mandate",
        sa.Column("mandate_id", sa.String(300), primary_key=True),
        sa.Column("lineage_id", sa.String(300), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "previous_mandate_id",
            sa.String(300),
            sa.ForeignKey("investment_mandate.mandate_id"),
            nullable=True,
        ),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_evidence_columns(),
        sa.UniqueConstraint("lineage_id", "version", name="uq_investment_mandate_lineage_version"),
        sa.CheckConstraint("version > 0", name="ck_investment_mandate_version"),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_investment_mandate_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_investment_mandate_authority"),
    )
    op.create_table(
        "mandate_evaluation",
        sa.Column("evaluation_id", sa.String(300), primary_key=True),
        sa.Column(
            "mandate_id",
            sa.String(300),
            sa.ForeignKey("investment_mandate.mandate_id"),
            nullable=False,
        ),
        sa.Column(
            "portfolio_snapshot_id",
            sa.String(300),
            sa.ForeignKey("portfolio_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column(
            "opportunity_id",
            sa.String(300),
            sa.ForeignKey("shared_opportunity_projection.projection_id"),
            nullable=False,
        ),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        *_evidence_columns(),
        sa.CheckConstraint(
            "state IN ('ALLOWED','CONSTRAINED','REJECTED')", name="ck_mandate_evaluation_state"
        ),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_mandate_evaluation_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_mandate_evaluation_authority"),
    )
    for table in ALL_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(ALL_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                "'cannot downgrade 0092 with retained portfolio-state/mandate evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(ALL_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
