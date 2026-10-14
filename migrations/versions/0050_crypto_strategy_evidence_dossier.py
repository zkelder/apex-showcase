"""Persist immutable crypto strategy specifications, replications, and dossiers.

Revision ID: 0050_crypto_dossier
Revises: 0049_crypto_validation
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0050_crypto_dossier"
down_revision: str | None = "0049_crypto_validation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "crypto_strategy_specification",
    "crypto_replication_protocol",
    "crypto_replication_run",
    "crypto_replication_outcome",
    "crypto_evidence_dossier",
    "crypto_dossier_conclusion",
)


def _authority_columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    )


def upgrade() -> None:
    op.create_table(
        "crypto_strategy_specification",
        sa.Column("specification_id", sa.String(160), primary_key=True),
        sa.Column(
            "hypothesis_version_id",
            sa.String(160),
            sa.ForeignKey("research_hypothesis_version.hypothesis_version_id"),
            nullable=False,
        ),
        sa.Column(
            "original_run_id",
            sa.String(160),
            sa.ForeignKey("crypto_experiment_run.run_id"),
            nullable=False,
        ),
        sa.Column(
            "original_outcome_id",
            sa.String(160),
            sa.ForeignKey("crypto_experiment_outcome.outcome_id"),
            nullable=False,
        ),
        sa.Column("semantics_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "parent_specification_id",
            sa.String(160),
            sa.ForeignKey("crypto_strategy_specification.specification_id"),
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("change_rationale", sa.String(500), nullable=False),
        sa.Column("lifecycle_state", sa.String(40), nullable=False),
        *_authority_columns(),
        sa.CheckConstraint("version >= 1", name="ck_crypto_strategy_version"),
        sa.CheckConstraint(
            "(version = 1 AND parent_specification_id IS NULL) OR "
            "(version > 1 AND parent_specification_id IS NOT NULL "
            "AND length(change_rationale) > 0)",
            name="ck_crypto_strategy_lineage",
        ),
        sa.CheckConstraint(
            "lifecycle_state = 'REJECTED_RESEARCH' AND authority = 'NONE'",
            name="ck_crypto_strategy_authority",
        ),
    )
    op.create_table(
        "crypto_replication_protocol",
        sa.Column("protocol_id", sa.String(160), primary_key=True),
        sa.Column(
            "strategy_specification_id",
            sa.String(160),
            sa.ForeignKey("crypto_strategy_specification.specification_id"),
            nullable=False,
        ),
        sa.Column("protocol_hash", sa.String(64), nullable=False, unique=True),
        *_authority_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_replication_protocol_authority"),
    )
    op.create_table(
        "crypto_replication_run",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column(
            "protocol_id",
            sa.String(160),
            sa.ForeignKey("crypto_replication_protocol.protocol_id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_specification_id",
            sa.String(160),
            sa.ForeignKey("crypto_strategy_specification.specification_id"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.String(160),
            sa.ForeignKey("crypto_historical_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("configuration_hash", sa.String(64), nullable=False),
        *_authority_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_replication_run_authority"),
    )
    op.create_table(
        "crypto_replication_outcome",
        sa.Column("outcome_id", sa.String(160), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(160),
            sa.ForeignKey("crypto_replication_run.run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("paper_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("live_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_authority_columns(),
        sa.CheckConstraint(
            "decision IN ('REJECTED','DEFERRED','PROMOTION_CANDIDATE')",
            name="ck_crypto_replication_outcome_decision",
        ),
        sa.CheckConstraint(
            "paper_eligible = false AND live_eligible = false AND authority = 'NONE'",
            name="ck_crypto_replication_outcome_authority",
        ),
    )
    op.create_table(
        "crypto_evidence_dossier",
        sa.Column("dossier_id", sa.String(160), primary_key=True),
        sa.Column(
            "strategy_specification_id",
            sa.String(160),
            sa.ForeignKey("crypto_strategy_specification.specification_id"),
            nullable=False,
        ),
        sa.Column(
            "replication_run_id",
            sa.String(160),
            sa.ForeignKey("crypto_replication_run.run_id"),
            nullable=False,
        ),
        sa.Column("membership_hash", sa.String(64), nullable=False, unique=True),
        *_authority_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_dossier_authority"),
    )
    op.create_table(
        "crypto_dossier_conclusion",
        sa.Column("conclusion_id", sa.String(160), primary_key=True),
        sa.Column(
            "dossier_id",
            sa.String(160),
            sa.ForeignKey("crypto_evidence_dossier.dossier_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("conclusion", sa.String(40), nullable=False),
        sa.Column("paper_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("live_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_authority_columns(),
        sa.CheckConstraint(
            "conclusion IN ('REJECTION_CONFIRMED','MIXED_EVIDENCE','INCONCLUSIVE')",
            name="ck_crypto_dossier_conclusion_value",
        ),
        sa.CheckConstraint(
            "paper_eligible = false AND live_eligible = false AND authority = 'NONE'",
            name="ck_crypto_dossier_conclusion_authority",
        ),
    )
    for table in TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "
                "RAISE EXCEPTION 'cannot downgrade 0050 with retained crypto dossier evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
