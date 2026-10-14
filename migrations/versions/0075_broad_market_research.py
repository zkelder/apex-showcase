"""Persist durable security-reference, broad-equity, and options-intelligence evidence.

Revision ID: 0075_broad_market_research
Revises: 0074_paper_activation
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0075_broad_market_research"
down_revision: str | None = "0074_paper_activation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "security_reference_identity",
    "security_reference_universe_snapshot",
    "broad_equity_security_quality",
    "broad_equity_discovery_run",
    "broad_equity_shadow_plan",
    "options_intelligence_capability",
    "options_intelligence_contract",
    "options_intelligence_optionable_underlying",
    "options_intelligence_shadow_proposal",
)


def upgrade() -> None:
    op.create_table(
        "security_reference_identity",
        sa.Column("canonical_hash", sa.String(64), primary_key=True),
        sa.Column("canonical_security_id", sa.String(160), nullable=False),
        sa.Column("current_symbol", sa.String(32), nullable=False),
        sa.Column("requires_review", sa.Boolean(), nullable=False),
        sa.Column("classifications", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_security_identity_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_security_identity_authority"),
    )
    op.create_index(
        "ix_security_identity_symbol",
        "security_reference_identity",
        ["current_symbol"],
    )
    op.create_index(
        "ix_security_identity_review",
        "security_reference_identity",
        ["requires_review"],
    )
    op.create_table(
        "security_reference_universe_snapshot",
        sa.Column("idempotency_key", sa.String(160), primary_key=True),
        sa.Column("universe_id", sa.String(160), nullable=False, unique=True),
        sa.Column("policy_id", sa.String(160), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("total_considered", sa.Integer(), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "total_considered >= 0 AND eligible_count >= 0 AND eligible_count <= total_considered",
            name="ck_security_universe_counts",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_security_universe_authority"),
    )
    op.create_table(
        "broad_equity_security_quality",
        sa.Column("canonical_hash", sa.String(64), primary_key=True),
        sa.Column("capture_id", sa.String(160), nullable=False),
        sa.Column("canonical_security_id", sa.String(160), nullable=False),
        sa.Column("readiness", sa.String(32), nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "readiness IN "
            "('READY','PARTIAL','BLOCKED_DATA','BLOCKED_IDENTITY',"
            "'INSUFFICIENT_HISTORY','PROVIDER_LIMITED')",
            name="ck_broad_equity_quality_readiness",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_broad_equity_quality_authority"),
    )
    op.create_index(
        "ix_broad_equity_quality_capture",
        "broad_equity_security_quality",
        ["capture_id"],
    )
    op.create_index(
        "ix_broad_equity_quality_security",
        "broad_equity_security_quality",
        ["canonical_security_id"],
    )
    op.create_table(
        "broad_equity_discovery_run",
        sa.Column("idempotency_key", sa.String(160), primary_key=True),
        sa.Column("run_id", sa.String(160), nullable=False, unique=True),
        sa.Column("universe_id", sa.String(160), nullable=False),
        sa.Column("data_capture_id", sa.String(160), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_broad_equity_discovery_authority"),
    )
    op.create_table(
        "broad_equity_shadow_plan",
        sa.Column("idempotency_key", sa.String(160), primary_key=True),
        sa.Column("plan_id", sa.String(160), nullable=False, unique=True),
        sa.Column(
            "discovery_run_id",
            sa.String(160),
            sa.ForeignKey("broad_equity_discovery_run.run_id"),
            nullable=False,
        ),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("state IN ('DRAFT')", name="ck_broad_equity_shadow_state"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_broad_equity_shadow_authority"),
    )
    op.create_table(
        "options_intelligence_capability",
        sa.Column("canonical_hash", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_options_capability_authority"),
    )
    op.create_index(
        "ix_options_capability_provider",
        "options_intelligence_capability",
        ["provider"],
    )
    op.create_table(
        "options_intelligence_contract",
        sa.Column("canonical_contract_id", sa.String(160), primary_key=True),
        sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
        sa.Column("option_type", sa.String(8), nullable=False),
        sa.Column("expiration", sa.String(10), nullable=False),
        sa.Column("source_provider", sa.String(80), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("option_type IN ('CALL','PUT')", name="ck_options_contract_type"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_options_contract_authority"),
    )
    op.create_index(
        "ix_options_contract_underlying",
        "options_intelligence_contract",
        ["underlying_canonical_security_id"],
    )
    op.create_index(
        "ix_options_contract_expiration",
        "options_intelligence_contract",
        ["expiration"],
    )
    op.create_table(
        "options_intelligence_optionable_underlying",
        sa.Column("canonical_hash", sa.String(64), primary_key=True),
        sa.Column("canonical_security_id", sa.String(160), nullable=False),
        sa.Column("optionable", sa.Boolean(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_options_optionable_authority"),
    )
    op.create_index(
        "ix_options_optionable_security",
        "options_intelligence_optionable_underlying",
        ["canonical_security_id"],
    )
    op.create_index(
        "ix_options_optionable_flag",
        "options_intelligence_optionable_underlying",
        ["optionable"],
    )
    op.create_table(
        "options_intelligence_shadow_proposal",
        sa.Column("idempotency_key", sa.String(160), primary_key=True),
        sa.Column("proposal_id", sa.String(160), nullable=False, unique=True),
        sa.Column("discovery_run_id", sa.String(160), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_options_shadow_proposal_authority"),
    )
    for table in TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in TABLES:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                f"'cannot downgrade 0075 with retained {table} evidence'; "
                "END IF; END $$"
            )
        )
    for table in TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
    for table in reversed(TABLES):
        op.drop_table(table)
