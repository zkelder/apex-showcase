"""Persist immutable materially different crypto challenger evidence.

Revision ID: 0051_crypto_challenger
Revises: 0050_crypto_dossier
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0051_crypto_challenger"
down_revision: str | None = "0050_crypto_dossier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "crypto_challenger_hypothesis",
    "crypto_challenger_protocol",
    "crypto_challenger_run",
    "crypto_challenger_outcome",
    "crypto_challenger_dossier",
    "crypto_challenger_conclusion",
)


def _evidence_columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    )


def upgrade() -> None:
    op.drop_constraint(
        "ck_crypto_strategy_authority",
        "crypto_strategy_specification",
        type_="check",
    )
    op.create_check_constraint(
        "ck_crypto_strategy_authority",
        "crypto_strategy_specification",
        "lifecycle_state IN ('REJECTED_RESEARCH','CHALLENGER_RESEARCH') AND authority = 'NONE'",
    )
    op.create_table(
        "crypto_challenger_hypothesis",
        sa.Column(
            "hypothesis_version_id",
            sa.String(160),
            sa.ForeignKey("research_hypothesis_version.hypothesis_version_id"),
            primary_key=True,
        ),
        *_evidence_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_challenger_hypothesis_authority"),
    )
    op.create_table(
        "crypto_challenger_protocol",
        sa.Column("protocol_id", sa.String(160), primary_key=True),
        sa.Column(
            "hypothesis_version_id",
            sa.String(160),
            sa.ForeignKey("crypto_challenger_hypothesis.hypothesis_version_id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_specification_id",
            sa.String(160),
            sa.ForeignKey("crypto_strategy_specification.specification_id"),
            nullable=False,
        ),
        sa.Column("protocol_hash", sa.String(64), nullable=False, unique=True),
        *_evidence_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_challenger_protocol_authority"),
    )
    op.create_table(
        "crypto_challenger_run",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column(
            "protocol_id",
            sa.String(160),
            sa.ForeignKey("crypto_challenger_protocol.protocol_id"),
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
        *_evidence_columns(),
        sa.UniqueConstraint(
            "protocol_id", "snapshot_id", name="uq_crypto_challenger_protocol_snapshot"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_challenger_run_authority"),
    )
    op.create_table(
        "crypto_challenger_outcome",
        sa.Column("outcome_id", sa.String(160), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(160),
            sa.ForeignKey("crypto_challenger_run.run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("paper_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("live_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_evidence_columns(),
        sa.CheckConstraint(
            "decision IN ('REJECTED','DEFERRED','PROMOTION_CANDIDATE')",
            name="ck_crypto_challenger_outcome_decision",
        ),
        sa.CheckConstraint(
            "paper_eligible = false AND live_eligible = false AND authority = 'NONE'",
            name="ck_crypto_challenger_outcome_authority",
        ),
    )
    op.create_table(
        "crypto_challenger_dossier",
        sa.Column("dossier_id", sa.String(160), primary_key=True),
        sa.Column(
            "protocol_id",
            sa.String(160),
            sa.ForeignKey("crypto_challenger_protocol.protocol_id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_specification_id",
            sa.String(160),
            sa.ForeignKey("crypto_strategy_specification.specification_id"),
            nullable=False,
        ),
        sa.Column(
            "earlier_run_id",
            sa.String(160),
            sa.ForeignKey("crypto_challenger_run.run_id"),
            nullable=False,
        ),
        sa.Column(
            "later_run_id",
            sa.String(160),
            sa.ForeignKey("crypto_challenger_run.run_id"),
            nullable=False,
        ),
        sa.Column("membership_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("complete_windows", sa.Integer(), nullable=False),
        sa.Column("passing_windows", sa.Integer(), nullable=False),
        sa.Column("failing_windows", sa.Integer(), nullable=False),
        sa.Column("inconclusive_windows", sa.Integer(), nullable=False),
        sa.Column("compounded_conservative_return", sa.Numeric(28, 8), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(
            "earlier_run_id <> later_run_id",
            name="ck_crypto_challenger_dossier_distinct_runs",
        ),
        sa.CheckConstraint(
            "complete_windows BETWEEN 0 AND 2 "
            "AND passing_windows BETWEEN 0 AND 2 "
            "AND failing_windows BETWEEN 0 AND 2 "
            "AND inconclusive_windows BETWEEN 0 AND 2 "
            "AND passing_windows + failing_windows + inconclusive_windows = 2",
            name="ck_crypto_challenger_dossier_counts",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_challenger_dossier_authority"),
    )
    op.create_table(
        "crypto_challenger_conclusion",
        sa.Column("conclusion_id", sa.String(160), primary_key=True),
        sa.Column(
            "dossier_id",
            sa.String(160),
            sa.ForeignKey("crypto_challenger_dossier.dossier_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("conclusion", sa.String(40), nullable=False),
        sa.Column("paper_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("live_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_evidence_columns(),
        sa.CheckConstraint(
            "conclusion IN ('RESEARCH_CANDIDATE','MIXED_EVIDENCE','REJECTED','INCONCLUSIVE')",
            name="ck_crypto_challenger_conclusion_value",
        ),
        sa.CheckConstraint(
            "paper_eligible = false AND live_eligible = false AND authority = 'NONE'",
            name="ck_crypto_challenger_conclusion_authority",
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
                "RAISE EXCEPTION "
                "'cannot downgrade 0051 with retained crypto challenger evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
    op.drop_constraint(
        "ck_crypto_strategy_authority",
        "crypto_strategy_specification",
        type_="check",
    )
    op.create_check_constraint(
        "ck_crypto_strategy_authority",
        "crypto_strategy_specification",
        "lifecycle_state = 'REJECTED_RESEARCH' AND authority = 'NONE'",
    )
