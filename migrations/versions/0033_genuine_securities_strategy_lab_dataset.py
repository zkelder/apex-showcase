"""Persist immutable genuine Securities Strategy Lab dataset manifests.

Revision ID: 0033_genuine_securities_dataset
Revises: 0032_real_securities_quality
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    immutable_trigger_ddl,
)

revision: str = "0033_genuine_securities_dataset"
down_revision: str | None = "0032_real_securities_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "securities_daily_direction_dataset",
        sa.Column("dataset_id", sa.String(160), primary_key=True),
        sa.Column(
            "strategy_lab_dataset_id",
            sa.String(160),
            sa.ForeignKey("strategy_lab_dataset_snapshot.dataset_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "raw_batch_id",
            sa.String(160),
            sa.ForeignKey("securities_market_data_batch.batch_id"),
            nullable=False,
        ),
        sa.Column("raw_batch_hash", sa.String(64), nullable=False),
        sa.Column(
            "derived_batch_id",
            sa.String(160),
            sa.ForeignKey("securities_market_data_batch.batch_id"),
            nullable=False,
        ),
        sa.Column("derived_batch_hash", sa.String(64), nullable=False),
        sa.Column(
            "universe_id",
            sa.String(160),
            sa.ForeignKey("securities_universe_snapshot.universe_id"),
            nullable=False,
        ),
        sa.Column("universe_hash", sa.String(64), nullable=False),
        sa.Column("calendar_identity", sa.String(80), nullable=False),
        sa.Column("calendar_version", sa.String(80), nullable=False),
        sa.Column("feature_schema_hash", sa.String(64), nullable=False),
        sa.Column("label_definition_hash", sa.String(64), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("group_count", sa.Integer(), nullable=False),
        sa.Column("instrument_count", sa.Integer(), nullable=False),
        sa.Column("positive_label_count", sa.Integer(), nullable=False),
        sa.Column("negative_label_count", sa.Integer(), nullable=False),
        sa.Column("exclusion_counts", sa.Text(), nullable=False),
        sa.Column("artifacts", sa.Text(), nullable=False),
        sa.Column("fold_definitions", sa.Text(), nullable=False),
        sa.Column("holdout_group_ids", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("baseline_scorecard_ids", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("baseline_scorecard_hashes", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("simulator_conformance_ids", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("simulator_conformance_hashes", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("canonical_input_hash", sa.String(64), nullable=False),
        sa.Column("canonical_result_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("end_date >= start_date", name="ck_daily_direction_dataset_dates"),
        sa.CheckConstraint(
            "row_count > 0 AND group_count > 0 AND instrument_count > 0 "
            "AND positive_label_count >= 0 AND negative_label_count >= 0 "
            "AND positive_label_count + negative_label_count = row_count",
            name="ck_daily_direction_dataset_counts",
        ),
        sa.CheckConstraint(
            "length(raw_batch_hash) = 64 AND length(derived_batch_hash) = 64 "
            "AND length(universe_hash) = 64 AND length(feature_schema_hash) = 64 "
            "AND length(label_definition_hash) = 64 AND length(canonical_input_hash) = 64 "
            "AND length(canonical_result_hash) = 64",
            name="ck_daily_direction_dataset_hashes",
        ),
    )
    op.execute(sa.text(immutable_trigger_ddl("securities_daily_direction_dataset")))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM securities_daily_direction_dataset) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0033 with retained genuine Securities dataset evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_immutable_securities_daily_direction_dataset_trigger "
            "ON securities_daily_direction_dataset"
        )
    )
    op.drop_table("securities_daily_direction_dataset")
