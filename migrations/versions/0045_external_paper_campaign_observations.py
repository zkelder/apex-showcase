"""Retain bounded External PAPER campaign observations and activity blocks.

Revision ID: 0045_external_paper_campaign_obs
Revises: 0044_securities_multi_fill
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_external_paper_campaign_obs"
down_revision: str | None = "0044_securities_multi_fill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_paper_campaign_observation",
        sa.Column("observation_id", sa.String(160), primary_key=True),
        sa.Column(
            "campaign_id", sa.String(160), sa.ForeignKey("campaign.campaign_id"), nullable=False
        ),
        sa.Column(
            "evidence_id",
            sa.String(160),
            sa.ForeignKey("external_paper_broker_evidence.evidence_id"),
            nullable=False,
        ),
        sa.Column("observation_type", sa.String(80), nullable=False),
        sa.Column("unknown_broker_activity", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_external_paper_campaign_observation_campaign",
        "external_paper_campaign_observation",
        ["campaign_id", "observed_at"],
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM external_paper_campaign_observation) "
            "THEN RAISE EXCEPTION 'cannot downgrade 0045 with retained external PAPER "
            "campaign observations'; END IF; END $$"
        )
    )
    op.drop_index(
        "ix_external_paper_campaign_observation_campaign",
        table_name="external_paper_campaign_observation",
    )
    op.drop_table("external_paper_campaign_observation")
