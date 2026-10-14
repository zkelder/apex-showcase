"""Persist engineering-only Alpaca PAPER acceptance identity and preflight."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_alpaca_paper_acceptance"
down_revision: str | None = "0045_external_paper_campaign_obs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alpaca_paper_acceptance_campaign",
        sa.Column("acceptance_id", sa.String(160), primary_key=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column(
            "campaign_id",
            sa.String(160),
            sa.ForeignKey("campaign.campaign_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "instruction_id",
            sa.String(160),
            sa.ForeignKey("external_paper_instruction.instruction_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_input_hash", sa.String(64), nullable=False),
        sa.Column("canonical_result_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "alpaca_paper_acceptance_preflight",
        sa.Column("preflight_id", sa.String(160), primary_key=True),
        sa.Column(
            "acceptance_id",
            sa.String(160),
            sa.ForeignKey("alpaca_paper_acceptance_campaign.acceptance_id"),
            nullable=False,
        ),
        sa.Column(
            "instruction_id",
            sa.String(160),
            sa.ForeignKey("external_paper_instruction.instruction_id"),
            nullable=False,
        ),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("freshness_expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_alpaca_paper_acceptance_preflight_instruction",
        "alpaca_paper_acceptance_preflight",
        ["instruction_id", "checked_at"],
    )


def downgrade() -> None:
    for table in ("alpaca_paper_acceptance_preflight", "alpaca_paper_acceptance_campaign"):
        op.execute(
            sa.text(
                f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {table}) THEN "
                "RAISE EXCEPTION 'cannot downgrade 0046 with retained "
                "acceptance evidence'; END IF; END $$"
            )
        )
    op.drop_index(
        "ix_alpaca_paper_acceptance_preflight_instruction",
        table_name="alpaca_paper_acceptance_preflight",
    )
    op.drop_table("alpaca_paper_acceptance_preflight")
    op.drop_table("alpaca_paper_acceptance_campaign")
