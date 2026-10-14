"""Create the atomic durable authorization boundary.

Revision ID: 0001_atomic_authorization
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    IMMUTABLE_TABLES,
    TRIGGER_DDL,
    immutable_trigger_ddl,
    metadata,
)

revision: str = "0001_atomic_authorization"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create explicit fixed-point tables, constraints, indexes, and guards."""

    metadata.create_all(bind=op.get_bind(), checkfirst=False)
    op.execute(sa.text(TRIGGER_DDL))
    for table_name in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))


def downgrade() -> None:
    """Remove the persistence phase completely so upgrade is repeatable."""

    metadata.drop_all(bind=op.get_bind(), checkfirst=False)
    op.execute(sa.text("DROP FUNCTION IF EXISTS apex_guard_posting()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS apex_guard_journal()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS apex_reject_mutation()"))
