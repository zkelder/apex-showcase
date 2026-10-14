"""Persist immutable retained-orderbook microstructure feature lineage.

Revision ID: 0018_orderbook_microstructure
Revises: 0017_strategy_shadowing
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    MICROSTRUCTURE_IMMUTABLE_TABLES,
    MICROSTRUCTURE_TABLES,
    immutable_trigger_ddl,
    microstructure_metadata,
)

revision: str = "0018_orderbook_microstructure"
down_revision: str | None = "0017_strategy_shadowing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add only immutable feature/prediction lineage; no financial schema changes."""

    microstructure_metadata.create_all(
        bind=op.get_bind(), tables=MICROSTRUCTURE_TABLES, checkfirst=False
    )
    for table_name in MICROSTRUCTURE_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))


def downgrade() -> None:
    """Refuse before DDL when retained reproducibility evidence would be lost."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM microstructure_feature_snapshot) "
            "OR EXISTS (SELECT 1 FROM microstructure_prediction_feature) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0018 with retained orderbook microstructure shadow evidence'; "
            "END IF; END $$"
        )
    )
    for table_name in MICROSTRUCTURE_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    microstructure_metadata.drop_all(bind=op.get_bind(), tables=MICROSTRUCTURE_TABLES)
