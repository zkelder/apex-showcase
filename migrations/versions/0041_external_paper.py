"""Retain external PAPER broker projections without a second financial engine.

Revision ID: 0041_external_paper
Revises: 0040_portfolio_intelligence
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_external_paper"
down_revision: str | None = "0040_portfolio_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_paper_capability_snapshot",
        sa.Column("snapshot_id", sa.String(160), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "external_paper_authorization",
        sa.Column("authorization_id", sa.String(160), primary_key=True),
        sa.Column(
            "campaign_id", sa.String(160), sa.ForeignKey("campaign.campaign_id"), nullable=False
        ),
        sa.Column("paper_trial_authorization_id", sa.String(160), nullable=False),
        sa.Column("portfolio_run_id", sa.String(160), nullable=False),
        sa.Column("portfolio_instruction_id", sa.String(160), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "external_paper_instruction",
        sa.Column("instruction_id", sa.String(160), primary_key=True),
        sa.Column(
            "authorization_id",
            sa.String(160),
            sa.ForeignKey("external_paper_authorization.authorization_id"),
            nullable=False,
        ),
        sa.Column(
            "campaign_id", sa.String(160), sa.ForeignKey("campaign.campaign_id"), nullable=False
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("state", sa.String(48), nullable=False),
        sa.Column("broker_order_id", sa.String(160)),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "external_paper_submission_attempt",
        sa.Column("attempt_id", sa.String(160), primary_key=True),
        sa.Column(
            "instruction_id",
            sa.String(160),
            sa.ForeignKey("external_paper_instruction.instruction_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(48), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "external_paper_broker_evidence",
        sa.Column("evidence_id", sa.String(160), primary_key=True),
        sa.Column(
            "instruction_id",
            sa.String(160),
            sa.ForeignKey("external_paper_instruction.instruction_id"),
        ),
        sa.Column("broker_order_id", sa.String(160)),
        sa.Column("observation_type", sa.String(80), nullable=False),
        sa.Column("endpoint_identity", sa.String(160), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "external_paper_reconciliation",
        sa.Column("reconciliation_id", sa.String(160), primary_key=True),
        sa.Column(
            "instruction_id",
            sa.String(160),
            sa.ForeignKey("external_paper_instruction.instruction_id"),
            nullable=False,
        ),
        sa.Column("classification", sa.String(80), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "external_paper_campaign_control",
        sa.Column(
            "campaign_id", sa.String(160), sa.ForeignKey("campaign.campaign_id"), primary_key=True
        ),
        sa.Column("kill_state", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column("operator_action_id", sa.String(160)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_external_paper_instruction_campaign",
        "external_paper_instruction",
        ["campaign_id", "updated_at"],
    )
    op.create_index(
        "ix_external_paper_evidence_instruction",
        "external_paper_broker_evidence",
        ["instruction_id", "observed_at"],
    )


def downgrade() -> None:
    for table in (
        "external_paper_capability_snapshot",
        "external_paper_authorization",
        "external_paper_instruction",
        "external_paper_submission_attempt",
        "external_paper_broker_evidence",
        "external_paper_reconciliation",
        "external_paper_campaign_control",
    ):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) "
                "THEN RAISE EXCEPTION "
                "'cannot downgrade 0041 with retained external PAPER evidence'; "
                "END IF; END $$"
            )
        )
    op.drop_index(
        "ix_external_paper_evidence_instruction", table_name="external_paper_broker_evidence"
    )
    op.drop_index("ix_external_paper_instruction_campaign", table_name="external_paper_instruction")
    op.drop_table("external_paper_campaign_control")
    op.drop_table("external_paper_reconciliation")
    op.drop_table("external_paper_broker_evidence")
    op.drop_table("external_paper_submission_attempt")
    op.drop_table("external_paper_instruction")
    op.drop_table("external_paper_authorization")
    op.drop_table("external_paper_capability_snapshot")
