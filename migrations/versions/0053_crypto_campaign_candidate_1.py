"""Persist immutable crypto campaign Candidate 1 development evidence.

Revision ID: 0053_crypto_campaign_candidate_1
Revises: 0052_crypto_campaign
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0053_crypto_campaign_candidate_1"
down_revision: str | None = "0052_crypto_campaign"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "crypto_campaign_candidate_hypothesis",
    "crypto_campaign_candidate_comparison",
    "crypto_campaign_candidate_protocol",
    "crypto_campaign_candidate_run",
    "crypto_campaign_candidate_outcome",
    "crypto_campaign_candidate_dossier",
    "crypto_campaign_candidate_nomination",
)


def _evidence_columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    )


def upgrade() -> None:
    op.create_table(
        "crypto_campaign_candidate_hypothesis",
        sa.Column(
            "hypothesis_version_id",
            sa.String(160),
            sa.ForeignKey("research_hypothesis_version.hypothesis_version_id"),
            primary_key=True,
        ),
        sa.Column(
            "campaign_id",
            sa.String(160),
            sa.ForeignKey("crypto_research_campaign.campaign_id"),
            nullable=False,
        ),
        *_evidence_columns(),
        sa.CheckConstraint(
            "authority = 'NONE'",
            name="ck_crypto_candidate_hypothesis_authority",
        ),
    )
    op.create_table(
        "crypto_campaign_candidate_comparison",
        sa.Column(
            "candidate_id",
            sa.String(160),
            sa.ForeignKey("crypto_campaign_candidate.candidate_id"),
            primary_key=True,
        ),
        sa.Column(
            "strategy_specification_id",
            sa.String(160),
            sa.ForeignKey("crypto_strategy_specification.specification_id"),
            nullable=False,
        ),
        *_evidence_columns(),
        sa.CheckConstraint(
            "authority = 'NONE'",
            name="ck_crypto_candidate_comparison_authority",
        ),
    )
    op.create_table(
        "crypto_campaign_candidate_protocol",
        sa.Column("protocol_id", sa.String(160), primary_key=True),
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
            unique=True,
        ),
        sa.Column("protocol_hash", sa.String(64), nullable=False, unique=True),
        *_evidence_columns(),
        sa.CheckConstraint(
            "authority = 'NONE'",
            name="ck_crypto_candidate_protocol_authority",
        ),
    )
    op.create_table(
        "crypto_campaign_candidate_run",
        sa.Column("run_id", sa.String(160), primary_key=True),
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
        sa.Column(
            "protocol_id",
            sa.String(160),
            sa.ForeignKey("crypto_campaign_candidate_protocol.protocol_id"),
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
            "protocol_id",
            "snapshot_id",
            name="uq_crypto_candidate_protocol_snapshot",
        ),
        sa.CheckConstraint(
            "authority = 'NONE'",
            name="ck_crypto_candidate_run_authority",
        ),
    )
    op.create_table(
        "crypto_campaign_candidate_outcome",
        sa.Column("outcome_id", sa.String(160), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(160),
            sa.ForeignKey("crypto_campaign_candidate_run.run_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("paper_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("live_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_evidence_columns(),
        sa.CheckConstraint(
            "decision IN ('REJECTED','DEFERRED','PROMOTION_CANDIDATE')",
            name="ck_crypto_candidate_outcome_decision",
        ),
        sa.CheckConstraint(
            "paper_eligible = false AND live_eligible = false AND authority = 'NONE'",
            name="ck_crypto_candidate_outcome_authority",
        ),
    )
    op.create_table(
        "crypto_campaign_candidate_dossier",
        sa.Column("dossier_id", sa.String(160), primary_key=True),
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
            unique=True,
        ),
        sa.Column(
            "protocol_id",
            sa.String(160),
            sa.ForeignKey("crypto_campaign_candidate_protocol.protocol_id"),
            nullable=False,
        ),
        sa.Column("membership_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("passing_windows", sa.Integer(), nullable=False),
        *_evidence_columns(),
        sa.CheckConstraint(
            "passing_windows BETWEEN 0 AND 2",
            name="ck_crypto_candidate_dossier_passing_windows",
        ),
        sa.CheckConstraint(
            "authority = 'NONE'",
            name="ck_crypto_candidate_dossier_authority",
        ),
    )
    op.create_table(
        "crypto_campaign_candidate_nomination",
        sa.Column("evaluation_id", sa.String(160), primary_key=True),
        sa.Column(
            "dossier_id",
            sa.String(160),
            sa.ForeignKey("crypto_campaign_candidate_dossier.dossier_id"),
            nullable=False,
            unique=True,
        ),
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
            unique=True,
        ),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("paper_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("live_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_evidence_columns(),
        sa.CheckConstraint(
            "paper_eligible = false AND live_eligible = false AND authority = 'NONE'",
            name="ck_crypto_candidate_nomination_authority",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION apex_crypto_candidate_run_guard() RETURNS trigger AS $$
        DECLARE holdout_snapshot text;
        BEGIN
          SELECT c.holdout_snapshot_id INTO holdout_snapshot
          FROM crypto_research_campaign c WHERE c.campaign_id = NEW.campaign_id;
          IF NEW.snapshot_id = holdout_snapshot THEN
            RAISE EXCEPTION 'sealed holdout cannot be used by candidate development run';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM crypto_campaign_dataset d
            WHERE d.campaign_id = NEW.campaign_id
              AND d.snapshot_id = NEW.snapshot_id
              AND d.role = 'DEVELOPMENT'
          ) THEN
            RAISE EXCEPTION 'candidate run requires attached development snapshot';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER apex_crypto_candidate_run_guard_trigger "
        "BEFORE INSERT ON crypto_campaign_candidate_run FOR EACH ROW "
        "EXECUTE FUNCTION apex_crypto_candidate_run_guard()"
    )
    op.execute(
        """
        CREATE FUNCTION apex_crypto_candidate_nomination_guard() RETURNS trigger AS $$
        BEGIN
          IF NEW.event_type = 'CANDIDATE_NOMINATED'
             AND EXISTS (
               SELECT 1 FROM crypto_campaign_candidate_comparison c
               WHERE c.candidate_id = NEW.candidate_id
             )
             AND NOT EXISTS (
               SELECT 1 FROM crypto_campaign_candidate_nomination n
               WHERE n.candidate_id = NEW.candidate_id
                 AND n.campaign_id = NEW.campaign_id
                 AND n.eligible = true
             ) THEN
            RAISE EXCEPTION 'candidate nomination requires eligible retained evaluation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER apex_crypto_candidate_nomination_guard_trigger "
        "BEFORE INSERT ON crypto_campaign_event FOR EACH ROW "
        "EXECUTE FUNCTION apex_crypto_candidate_nomination_guard()"
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
                "'cannot downgrade 0053 with retained crypto campaign candidate evidence'; "
                "END IF; END $$"
            )
        )
    op.execute(
        "DROP TRIGGER apex_crypto_candidate_nomination_guard_trigger ON crypto_campaign_event"
    )
    op.execute("DROP FUNCTION apex_crypto_candidate_nomination_guard()")
    op.execute(
        "DROP TRIGGER apex_crypto_candidate_run_guard_trigger ON crypto_campaign_candidate_run"
    )
    op.execute("DROP FUNCTION apex_crypto_candidate_run_guard()")
    for table in reversed(TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
