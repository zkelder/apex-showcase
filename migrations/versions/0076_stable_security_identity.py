"""Stable, source-preference-invariant security identity and options relink.

Revision ID: 0076_stable_security_identity
Revises: 0075_broad_market_research
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0076_stable_security_identity"
down_revision: str | None = "0075_broad_market_research"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "security_stable_identity",
    "security_identity_resolution",
    "options_underlying_stable_linkage",
)


def upgrade() -> None:
    op.create_table(
        "security_stable_identity",
        sa.Column("canonical_hash", sa.String(64), primary_key=True),
        sa.Column("stable_security_id", sa.String(160), nullable=False),
        sa.Column("identity_basis", sa.String(24), nullable=False),
        sa.Column("cik", sa.String(10), nullable=True),
        sa.Column("normalized_symbol", sa.String(32), nullable=False),
        sa.Column("current_symbol", sa.String(32), nullable=False),
        sa.Column(
            "source_identity_hash",
            sa.String(64),
            sa.ForeignKey("security_reference_identity.canonical_hash"),
            nullable=False,
        ),
        sa.Column("source_canonical_security_id", sa.String(160), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_stable_identity_hash"),
        sa.CheckConstraint(
            "identity_basis IN ('CIK_AND_SYMBOL','SYMBOL_ONLY')",
            name="ck_stable_identity_basis",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_stable_identity_authority"),
    )
    op.create_index(
        "ix_stable_identity_stable_id", "security_stable_identity", ["stable_security_id"]
    )
    op.create_index("ix_stable_identity_symbol", "security_stable_identity", ["current_symbol"])
    op.create_index("ix_stable_identity_review", "security_stable_identity", ["review_required"])

    op.create_table(
        "security_identity_resolution",
        sa.Column("canonical_hash", sa.String(64), primary_key=True),
        sa.Column("old_identity_id", sa.String(160), nullable=False),
        sa.Column("old_identity_kind", sa.String(40), nullable=False),
        sa.Column("resolved_stable_security_id", sa.String(160), nullable=True),
        sa.Column("resolution_state", sa.String(40), nullable=False),
        sa.Column("automatic", sa.Boolean(), nullable=False),
        sa.Column("resolution_method", sa.String(80), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_identity_resolution_hash"),
        sa.CheckConstraint(
            "old_identity_kind IN ('LEGACY_CANONICAL_SECURITY_ID','STABLE_SECURITY_ID')",
            name="ck_identity_resolution_kind",
        ),
        sa.CheckConstraint(
            "resolution_state IN "
            "('CURRENT','ALIAS','SUPERSEDED','EQUIVALENT',"
            "'AMBIGUOUS_REQUIRES_REVIEW','DISTINCT_SECURITY')",
            name="ck_identity_resolution_state",
        ),
        sa.CheckConstraint(
            "NOT (automatic AND resolution_state IN "
            "('AMBIGUOUS_REQUIRES_REVIEW','DISTINCT_SECURITY'))",
            name="ck_identity_resolution_ambiguity_not_automatic",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_identity_resolution_authority"),
    )
    op.create_index(
        "ix_identity_resolution_old_id", "security_identity_resolution", ["old_identity_id"]
    )
    op.create_index(
        "ix_identity_resolution_resolved_id",
        "security_identity_resolution",
        ["resolved_stable_security_id"],
    )
    op.create_index(
        "ix_identity_resolution_state", "security_identity_resolution", ["resolution_state"]
    )

    op.create_table(
        "options_underlying_stable_linkage",
        sa.Column("canonical_hash", sa.String(64), primary_key=True),
        sa.Column("raw_underlying_symbol", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("resolved_stable_security_id", sa.String(160), nullable=True),
        sa.Column("historical_underlying_security_id", sa.String(160), nullable=True),
        sa.Column("contract_count", sa.Integer(), nullable=False),
        sa.Column("linkage_run_id", sa.String(160), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_options_stable_linkage_hash"),
        sa.CheckConstraint(
            "outcome IN ('LINKED','UNLINKED','AMBIGUOUS')",
            name="ck_options_stable_linkage_outcome",
        ),
        sa.CheckConstraint("contract_count >= 0", name="ck_options_stable_linkage_count"),
        sa.CheckConstraint(
            "(outcome = 'LINKED') = (resolved_stable_security_id IS NOT NULL)",
            name="ck_options_stable_linkage_resolved_consistency",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_options_stable_linkage_authority"),
    )
    op.create_index(
        "ix_options_stable_linkage_symbol",
        "options_underlying_stable_linkage",
        ["raw_underlying_symbol"],
    )
    op.create_index(
        "ix_options_stable_linkage_resolved",
        "options_underlying_stable_linkage",
        ["resolved_stable_security_id"],
    )
    op.create_index(
        "ix_options_stable_linkage_run", "options_underlying_stable_linkage", ["linkage_run_id"]
    )
    op.create_index(
        "ix_options_stable_linkage_outcome", "options_underlying_stable_linkage", ["outcome"]
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
                f"'cannot downgrade 0076 with retained {table} evidence'; "
                "END IF; END $$"
            )
        )
    for table in TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
    for table in reversed(TABLES):
        op.drop_table(table)
