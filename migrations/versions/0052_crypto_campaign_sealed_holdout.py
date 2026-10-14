"""Add immutable crypto exposure, campaign, budget, and holdout governance.

Revision ID: 0052_crypto_campaign
Revises: 0051_crypto_challenger
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0052_crypto_campaign"
down_revision: str | None = "0051_crypto_challenger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "crypto_dataset_exposure",
    "crypto_research_campaign",
    "crypto_campaign_dataset",
    "crypto_campaign_candidate",
    "crypto_campaign_experiment",
    "crypto_campaign_event",
    "crypto_holdout_event",
)


def _evidence_columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    )


def upgrade() -> None:
    op.create_table(
        "crypto_dataset_exposure",
        sa.Column("exposure_id", sa.String(160), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.String(160),
            sa.ForeignKey("crypto_historical_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("category", sa.String(48), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(
            "category IN ('DEVELOPMENT_EXPOSED','VALIDATION_EXPOSED',"
            "'REPLICATION_EXPOSED','OPERATOR_EXPOSED','DOSSIER_EXPOSED',"
            "'SEALED_HOLDOUT','HOLDOUT_RELEASED','HOLDOUT_EVALUATED','INVALIDATED')",
            name="ck_crypto_dataset_exposure_category",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_dataset_exposure_authority"),
    )
    op.create_table(
        "crypto_research_campaign",
        sa.Column("campaign_id", sa.String(160), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("lifecycle_state", sa.String(48), nullable=False),
        sa.Column("candidate_budget", sa.Integer(), nullable=False),
        sa.Column("experiment_budget", sa.Integer(), nullable=False),
        sa.Column(
            "holdout_snapshot_id",
            sa.String(160),
            nullable=False,
            unique=True,
        ),
        *_evidence_columns(),
        sa.CheckConstraint("version = 1", name="ck_crypto_campaign_version"),
        sa.CheckConstraint(
            "candidate_budget BETWEEN 1 AND 3 AND experiment_budget BETWEEN 1 AND 6",
            name="ck_crypto_campaign_budgets",
        ),
        sa.CheckConstraint(
            "lifecycle_state = 'DEVELOPMENT_ACTIVE'",
            name="ck_crypto_campaign_initial_state",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_campaign_authority"),
    )
    op.create_table(
        "crypto_campaign_dataset",
        sa.Column("attachment_id", sa.String(160), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(160),
            sa.ForeignKey("crypto_research_campaign.campaign_id"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.String(160),
            sa.ForeignKey("crypto_historical_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(32), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint(
            "campaign_id", "snapshot_id", "role", name="uq_crypto_campaign_dataset_role"
        ),
        sa.CheckConstraint("role IN ('DEVELOPMENT','HOLDOUT')", name="ck_crypto_dataset_role"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_campaign_dataset_authority"),
    )
    op.create_table(
        "crypto_campaign_candidate",
        sa.Column("candidate_id", sa.String(160), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(160),
            sa.ForeignKey("crypto_research_campaign.campaign_id"),
            nullable=False,
        ),
        sa.Column("strategy_family", sa.String(160), nullable=False),
        sa.Column("strategy_semantics_hash", sa.String(64), nullable=False),
        *_evidence_columns(),
        sa.UniqueConstraint(
            "campaign_id", "strategy_family", name="uq_crypto_campaign_candidate_family"
        ),
        sa.UniqueConstraint(
            "campaign_id",
            "strategy_semantics_hash",
            name="uq_crypto_campaign_candidate_semantics",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_campaign_candidate_authority"),
    )
    op.create_table(
        "crypto_campaign_experiment",
        sa.Column("consumption_id", sa.String(160), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(160),
            sa.ForeignKey("crypto_research_campaign.campaign_id"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(160),
            sa.ForeignKey("crypto_campaign_candidate.candidate_id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(160), nullable=False),
        sa.Column(
            "snapshot_id",
            sa.String(160),
            sa.ForeignKey("crypto_historical_snapshot.snapshot_id"),
            nullable=False,
        ),
        *_evidence_columns(),
        sa.UniqueConstraint("campaign_id", "run_id", name="uq_crypto_campaign_experiment_run"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_campaign_experiment_authority"),
    )
    op.create_table(
        "crypto_campaign_event",
        sa.Column("event_id", sa.String(160), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(160),
            sa.ForeignKey("crypto_research_campaign.campaign_id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column(
            "candidate_id",
            sa.String(160),
            sa.ForeignKey("crypto_campaign_candidate.candidate_id"),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_campaign_event_authority"),
    )
    op.create_index(
        "uq_crypto_campaign_single_nomination",
        "crypto_campaign_event",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'CANDIDATE_NOMINATED'"),
    )
    op.create_index(
        "uq_crypto_campaign_single_release",
        "crypto_campaign_event",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'HOLDOUT_RELEASE_AUTHORIZED'"),
    )
    op.create_table(
        "crypto_holdout_event",
        sa.Column("event_id", sa.String(160), primary_key=True),
        sa.Column("holdout_id", sa.String(160), nullable=False),
        sa.Column(
            "campaign_id",
            sa.String(160),
            sa.ForeignKey("crypto_research_campaign.campaign_id"),
            nullable=False,
        ),
        sa.Column("state", sa.String(48), nullable=False),
        sa.Column(
            "candidate_id",
            sa.String(160),
            sa.ForeignKey("crypto_campaign_candidate.candidate_id"),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(
            "state IN ('CAPTURE_IN_PROGRESS','CAPTURE_FAILED','READY_TO_SEAL','SEALED',"
            "'RELEASE_AUTHORIZED','EVALUATED','INVALIDATED')",
            name="ck_crypto_holdout_state",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crypto_holdout_authority"),
    )
    op.create_index(
        "uq_crypto_holdout_single_seal",
        "crypto_holdout_event",
        ["holdout_id"],
        unique=True,
        postgresql_where=sa.text("state = 'SEALED'"),
    )
    op.create_index(
        "uq_crypto_holdout_single_release",
        "crypto_holdout_event",
        ["holdout_id"],
        unique=True,
        postgresql_where=sa.text("state = 'RELEASE_AUTHORIZED'"),
    )
    op.create_index(
        "uq_crypto_holdout_single_evaluation",
        "crypto_holdout_event",
        ["holdout_id"],
        unique=True,
        postgresql_where=sa.text("state = 'EVALUATED'"),
    )
    op.execute(
        """
        CREATE FUNCTION apex_crypto_campaign_budget_guard() RETURNS trigger AS $$
        DECLARE configured integer;
        DECLARE consumed integer;
        BEGIN
          IF TG_TABLE_NAME = 'crypto_campaign_candidate' THEN
            SELECT candidate_budget INTO configured FROM crypto_research_campaign
              WHERE campaign_id = NEW.campaign_id;
            SELECT count(*) INTO consumed FROM crypto_campaign_candidate
              WHERE campaign_id = NEW.campaign_id;
          ELSE
            SELECT experiment_budget INTO configured FROM crypto_research_campaign
              WHERE campaign_id = NEW.campaign_id;
            SELECT count(*) INTO consumed FROM crypto_campaign_experiment
              WHERE campaign_id = NEW.campaign_id;
          END IF;
          IF consumed >= configured THEN
            RAISE EXCEPTION 'crypto campaign budget exhausted';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER apex_crypto_candidate_budget_guard "
        "BEFORE INSERT ON crypto_campaign_candidate FOR EACH ROW "
        "EXECUTE FUNCTION apex_crypto_campaign_budget_guard()"
    )
    op.execute(
        "CREATE TRIGGER apex_crypto_experiment_budget_guard "
        "BEFORE INSERT ON crypto_campaign_experiment FOR EACH ROW "
        "EXECUTE FUNCTION apex_crypto_campaign_budget_guard()"
    )
    op.execute(
        """
        CREATE FUNCTION apex_crypto_exposure_guard() RETURNS trigger AS $$
        BEGIN
          IF NEW.category = 'SEALED_HOLDOUT' AND EXISTS (
            SELECT 1 FROM crypto_dataset_exposure
            WHERE snapshot_id = NEW.snapshot_id
              AND category IN ('DEVELOPMENT_EXPOSED','VALIDATION_EXPOSED',
                'REPLICATION_EXPOSED','OPERATOR_EXPOSED','DOSSIER_EXPOSED',
                'HOLDOUT_RELEASED','HOLDOUT_EVALUATED','INVALIDATED')
          ) THEN
            RAISE EXCEPTION 'exposed snapshot cannot become a sealed holdout';
          END IF;
          IF NEW.category = 'HOLDOUT_RELEASED' AND NOT EXISTS (
            SELECT 1 FROM crypto_dataset_exposure
            WHERE snapshot_id = NEW.snapshot_id AND category = 'SEALED_HOLDOUT'
          ) THEN
            RAISE EXCEPTION 'holdout release requires retained seal evidence';
          END IF;
          IF NEW.category = 'HOLDOUT_EVALUATED' AND NOT EXISTS (
            SELECT 1 FROM crypto_dataset_exposure
            WHERE snapshot_id = NEW.snapshot_id AND category = 'HOLDOUT_RELEASED'
          ) THEN
            RAISE EXCEPTION 'holdout evaluation requires retained release evidence';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER apex_crypto_exposure_guard "
        "BEFORE INSERT ON crypto_dataset_exposure FOR EACH ROW "
        "EXECUTE FUNCTION apex_crypto_exposure_guard()"
    )
    op.execute(
        """
        CREATE FUNCTION apex_crypto_holdout_transition_guard() RETURNS trigger AS $$
        DECLARE previous text;
        BEGIN
          SELECT state INTO previous FROM crypto_holdout_event
            WHERE holdout_id = NEW.holdout_id
            ORDER BY occurred_at DESC, event_id DESC LIMIT 1;
          IF NOT (
            (previous IS NULL AND NEW.state = 'CAPTURE_IN_PROGRESS') OR
            (previous = 'CAPTURE_IN_PROGRESS' AND
              NEW.state IN ('CAPTURE_FAILED','READY_TO_SEAL','INVALIDATED')) OR
            (previous = 'READY_TO_SEAL' AND NEW.state IN ('SEALED','INVALIDATED')) OR
            (previous = 'SEALED' AND NEW.state IN ('RELEASE_AUTHORIZED','INVALIDATED')) OR
            (previous = 'RELEASE_AUTHORIZED' AND
              NEW.state IN ('EVALUATED','INVALIDATED')) OR
            (previous IN ('CAPTURE_FAILED','EVALUATED') AND NEW.state = 'INVALIDATED')
          ) THEN
            RAISE EXCEPTION 'invalid crypto holdout transition';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER apex_crypto_holdout_transition_guard "
        "BEFORE INSERT ON crypto_holdout_event FOR EACH ROW "
        "EXECUTE FUNCTION apex_crypto_holdout_transition_guard()"
    )
    op.execute(
        """
        CREATE FUNCTION apex_crypto_campaign_transition_guard() RETURNS trigger AS $$
        DECLARE previous text;
        BEGIN
          SELECT event_type INTO previous FROM crypto_campaign_event
            WHERE campaign_id = NEW.campaign_id
            ORDER BY occurred_at DESC, event_id DESC LIMIT 1;
          IF NEW.event_type <> 'INVALIDATED' AND NOT (
            (previous IS NULL AND
              NEW.event_type IN ('CANDIDATE_NOMINATED','NO_CANDIDATE')) OR
            (previous = 'CANDIDATE_NOMINATED' AND
              NEW.event_type = 'DEVELOPMENT_CLOSED') OR
            (previous = 'DEVELOPMENT_CLOSED' AND
              NEW.event_type = 'HOLDOUT_RELEASE_AUTHORIZED') OR
            (previous = 'HOLDOUT_RELEASE_AUTHORIZED' AND
              NEW.event_type = 'HOLDOUT_EVALUATED') OR
            (previous = 'HOLDOUT_EVALUATED' AND NEW.event_type = 'COMPLETED')
          ) THEN
            RAISE EXCEPTION 'invalid crypto campaign transition';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER apex_crypto_campaign_transition_guard "
        "BEFORE INSERT ON crypto_campaign_event FOR EACH ROW "
        "EXECUTE FUNCTION apex_crypto_campaign_transition_guard()"
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
                "'cannot downgrade 0052 with retained crypto campaign evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
    op.execute(
        "DROP TRIGGER IF EXISTS apex_crypto_campaign_transition_guard ON crypto_campaign_event"
    )
    op.execute("DROP FUNCTION IF EXISTS apex_crypto_campaign_transition_guard()")
    op.execute(
        "DROP TRIGGER IF EXISTS apex_crypto_holdout_transition_guard ON crypto_holdout_event"
    )
    op.execute("DROP FUNCTION IF EXISTS apex_crypto_holdout_transition_guard()")
    op.execute("DROP TRIGGER apex_crypto_exposure_guard ON crypto_dataset_exposure")
    op.execute("DROP FUNCTION apex_crypto_exposure_guard()")
    op.execute("DROP TRIGGER apex_crypto_experiment_budget_guard ON crypto_campaign_experiment")
    op.execute("DROP TRIGGER apex_crypto_candidate_budget_guard ON crypto_campaign_candidate")
    op.execute("DROP FUNCTION apex_crypto_campaign_budget_guard()")
    for table in reversed(TABLES):
        op.drop_table(table)
