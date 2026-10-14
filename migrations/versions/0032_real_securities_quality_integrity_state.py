"""Persist the explicit integrity classification for real Securities quality evidence.

Revision ID: 0032_real_securities_quality
Revises: 0031_real_securities_data
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_real_securities_quality"
down_revision: str | None = "0031_real_securities_data"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns("securities_data_quality_evaluation")
    }
    if "integrity_state" not in columns:
        op.add_column(
            "securities_data_quality_evaluation",
            sa.Column("integrity_state", sa.String(length=32), nullable=True),
        )
    op.execute(
        sa.text(
            "UPDATE securities_data_quality_evaluation "
            "SET integrity_state = 'BLOCKED' WHERE integrity_state IS NULL"
        )
    )
    op.alter_column("securities_data_quality_evaluation", "integrity_state", nullable=False)
    constraints = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_check_constraints(
            "securities_data_quality_evaluation"
        )
    }
    if "ck_real_quality_integrity" not in constraints:
        op.create_check_constraint(
            "ck_real_quality_integrity",
            "securities_data_quality_evaluation",
            "integrity_state IN ('READY','BLOCKED')",
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM securities_data_quality_evaluation) "
            "THEN RAISE EXCEPTION 'cannot downgrade 0032 with retained quality evidence'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint(
        "ck_real_quality_integrity", "securities_data_quality_evaluation", type_="check"
    )
    op.drop_column("securities_data_quality_evaluation", "integrity_state")
