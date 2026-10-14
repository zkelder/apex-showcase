"""Persist bounded generic crypto candidate definitions and compiled plans.

Revision ID: 0054_crypto_candidate_generic
Revises: 0053_crypto_campaign_candidate_1
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0054_crypto_candidate_generic"
down_revision: str | None = "0053_crypto_campaign_candidate_1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "crypto_campaign_compiled_plan",
    "crypto_campaign_candidate_definition",
)


def _evidence_columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    )


def upgrade() -> None:
    op.create_table(
        "crypto_campaign_candidate_definition",
        sa.Column("definition_id", sa.String(160), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(160),
            sa.ForeignKey("crypto_research_campaign.campaign_id"),
            nullable=False,
        ),
        sa.Column("candidate_id", sa.String(160), nullable=False),
        sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
        sa.Column("strategy_specification_id", sa.String(160), nullable=False),
        sa.Column("schema_version", sa.String(80), nullable=False),
        sa.Column("semantics_hash", sa.String(64), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(
            "schema_version = 'GENERIC_CRYPTO_CANDIDATE_DEFINITION_V1'",
            name="ck_crypto_generic_definition_schema",
        ),
        sa.CheckConstraint(
            "authority = 'NONE'",
            name="ck_crypto_generic_definition_authority",
        ),
    )
    op.create_table(
        "crypto_campaign_compiled_plan",
        sa.Column("plan_id", sa.String(160), primary_key=True),
        sa.Column(
            "definition_id",
            sa.String(160),
            sa.ForeignKey("crypto_campaign_candidate_definition.definition_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("compiler_id", sa.String(80), nullable=False),
        sa.Column("compiler_version", sa.Integer(), nullable=False),
        sa.Column("semantics_hash", sa.String(64), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False, unique=True),
        *_evidence_columns(),
        sa.CheckConstraint(
            "compiler_id = 'GENERIC_CRYPTO_CANDIDATE_COMPILER_V1' AND compiler_version = 1",
            name="ck_crypto_generic_plan_compiler",
        ),
        sa.CheckConstraint(
            "authority = 'NONE'",
            name="ck_crypto_generic_plan_authority",
        ),
    )
    for table in TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in TABLES:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "
                "RAISE EXCEPTION "
                "'cannot downgrade 0054 with retained generic crypto candidate evidence'; "
                "END IF; END $$"
            )
        )
    for table in TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
