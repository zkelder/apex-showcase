"""Persist bounded multi-asset crypto universe and discovery evidence.

Revision ID: 0055_crypto_multi_asset
Revises: 0054_crypto_candidate_generic
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0055_crypto_multi_asset"
down_revision: str | None = "0054_crypto_candidate_generic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "crypto_public_product_capture",
    "crypto_public_product",
    "crypto_product_observation",
    "crypto_universe_snapshot",
    "crypto_discovery_run",
    "crypto_research_opportunity",
)


def _evidence_columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    )


def upgrade() -> None:
    op.create_table(
        "crypto_public_product_capture",
        sa.Column("capture_id", sa.String(160), primary_key=True),
        sa.Column("evidence_class", sa.String(40), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_product_capture_authority"),
    )
    op.create_table(
        "crypto_public_product",
        sa.Column(
            "capture_id",
            sa.String(160),
            sa.ForeignKey("crypto_public_product_capture.capture_id"),
            primary_key=True,
        ),
        sa.Column("product_id", sa.String(160), primary_key=True),
        *_evidence_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_product_authority"),
    )
    op.create_table(
        "crypto_product_observation",
        sa.Column("observation_id", sa.String(160), primary_key=True),
        sa.Column("product_id", sa.String(160), nullable=False),
        sa.Column("evidence_class", sa.String(40), nullable=False),
        sa.Column("checkpoint_ordinal", sa.Integer(), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(
            "checkpoint_ordinal BETWEEN 1 AND 10",
            name="ck_crypto_product_observation_checkpoint",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_observation_authority"),
    )
    op.create_table(
        "crypto_universe_snapshot",
        sa.Column("snapshot_id", sa.String(160), primary_key=True),
        sa.Column(
            "source_capture_id",
            sa.String(160),
            sa.ForeignKey("crypto_public_product_capture.capture_id"),
            nullable=False,
        ),
        sa.Column("policy_id", sa.String(80), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False, unique=True),
        *_evidence_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_universe_authority"),
    )
    op.create_table(
        "crypto_discovery_run",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column(
            "universe_snapshot_id",
            sa.String(160),
            sa.ForeignKey("crypto_universe_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("method_id", sa.String(80), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_discovery_authority"),
    )
    op.create_table(
        "crypto_research_opportunity",
        sa.Column("opportunity_id", sa.String(160), primary_key=True),
        sa.Column(
            "discovery_run_id",
            sa.String(160),
            sa.ForeignKey("crypto_discovery_run.run_id"),
            nullable=False,
        ),
        sa.Column("product_id", sa.String(160), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(
            "status IN ('RESEARCH_ONLY', 'REJECTED')",
            name="ck_crypto_research_opportunity_status",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_opportunity_authority"),
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
                "'cannot downgrade 0055 with retained multi-asset crypto evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
