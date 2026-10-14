"""Persist immutable research-only Securities strategy-intelligence runs.

Revision ID: 0035_strategy_intelligence
Revises: 0034_validation_candidate_link
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0035_strategy_intelligence"
down_revision: str | None = "0034_validation_candidate_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "securities_strategy_intelligence_run",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.String(160),
            sa.ForeignKey("securities_daily_direction_dataset.dataset_id"),
            nullable=False,
        ),
        sa.Column(
            "raw_batch_id",
            sa.String(160),
            sa.ForeignKey("securities_market_data_batch.batch_id"),
            nullable=False,
        ),
        sa.Column(
            "derived_batch_id",
            sa.String(160),
            sa.ForeignKey("securities_market_data_batch.batch_id"),
            nullable=False,
        ),
        sa.Column("feature_policy_id", sa.String(160), nullable=False),
        sa.Column("regime_policy_hash", sa.String(64), nullable=False),
        sa.Column("outcome_policy_hash", sa.String(64), nullable=False),
        sa.Column("action_policy_hash", sa.String(64), nullable=False),
        sa.Column("ranking_policy_hash", sa.String(64), nullable=False),
        sa.Column("model_integrity_state", sa.String(80), nullable=False),
        sa.Column("simulator_conformance_state", sa.String(80), nullable=False),
        sa.Column("blocking_reasons", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("artifacts", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("canonical_input_hash", sa.String(64), nullable=False),
        sa.Column("canonical_result_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(regime_policy_hash) = 64 AND length(outcome_policy_hash) = 64 AND "
            "length(action_policy_hash) = 64 AND length(ranking_policy_hash) = 64 AND "
            "length(canonical_input_hash) = 64 AND length(canonical_result_hash) = 64",
            name="ck_strategy_intelligence_hashes",
        ),
    )
    op.execute(sa.text(immutable_trigger_ddl("securities_strategy_intelligence_run")))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM securities_strategy_intelligence_run) "
            "THEN RAISE EXCEPTION 'cannot downgrade 0035 with retained "
            "strategy-intelligence evidence'; END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_immutable_securities_strategy_intelligence_run_trigger "
            "ON securities_strategy_intelligence_run"
        )
    )
    op.drop_table("securities_strategy_intelligence_run")
