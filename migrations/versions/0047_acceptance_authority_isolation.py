"""Allow the engineering acceptance path to retain no research signal or review."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_acceptance_isolation"
down_revision: str | None = "0046_alpaca_paper_acceptance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "paper_trial_authorization", "review_id", existing_type=sa.String(160), nullable=True
    )
    op.alter_column(
        "securities_paper_opportunity", "signal_id", existing_type=sa.String(160), nullable=True
    )


def downgrade() -> None:
    for table, column in (
        ("paper_trial_authorization", "review_id"),
        ("securities_paper_opportunity", "signal_id"),
    ):
        op.execute(
            sa.text(
                f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {table} WHERE {column} IS NULL) THEN "
                "RAISE EXCEPTION 'cannot downgrade 0047 with retained engineering "
                "acceptance evidence'; "
                "END IF; END $$"
            )
        )
    op.alter_column(
        "securities_paper_opportunity", "signal_id", existing_type=sa.String(160), nullable=False
    )
    op.alter_column(
        "paper_trial_authorization", "review_id", existing_type=sa.String(160), nullable=False
    )
