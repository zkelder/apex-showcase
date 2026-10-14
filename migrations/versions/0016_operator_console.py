"""Persist local Operator Console metadata and immutable audit actions.

Revision ID: 0016_operator_console
Revises: 0015_paper_portfolio
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    OPERATOR_IMMUTABLE_TABLES,
    OPERATOR_TABLES,
    immutable_trigger_ddl,
    operator_metadata,
)

revision: str = "0016_operator_console"
down_revision: str | None = "0015_paper_portfolio"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    operator_metadata.create_all(bind=op.get_bind(), tables=OPERATOR_TABLES, checkfirst=False)
    for table_name in OPERATOR_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))


def downgrade() -> None:
    # The console action log and display terms are retained audit evidence.  A
    # downgrade must never silently remove them or leave a partial schema.
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM campaign_operator_metadata) "
            "OR EXISTS (SELECT 1 FROM operator_action) THEN "
            "RAISE EXCEPTION 'cannot downgrade 0016 with retained operator console evidence'; "
            "END IF; END $$"
        )
    )
    for table_name in OPERATOR_IMMUTABLE_TABLES:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF to_regclass('{table_name}') IS NOT NULL THEN "
                f"EXECUTE 'DROP TRIGGER IF EXISTS apex_immutable_{table_name}_trigger "
                f"ON {table_name}'; "
                "END IF; END $$"
            )
        )
    operator_metadata.drop_all(bind=op.get_bind(), tables=OPERATOR_TABLES, checkfirst=True)
