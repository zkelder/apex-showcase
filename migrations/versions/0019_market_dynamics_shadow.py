"""Persist immutable market-dynamics SHADOW feature lineage.

Revision ID: 0019_market_dynamics
Revises: 0018_orderbook_microstructure
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    MARKET_DYNAMICS_IMMUTABLE_TABLES,
    MARKET_DYNAMICS_TABLES,
    dynamics_metadata,
    immutable_trigger_ddl,
)

revision: str = "0019_market_dynamics"
down_revision: str | None = "0018_orderbook_microstructure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add temporal-price feature and prediction provenance only."""
    op.drop_constraint("ck_strategy_governance_family", "strategy_governance", type_="check")
    op.create_check_constraint(
        "ck_strategy_governance_family",
        "strategy_governance",
        "family IN ('EXTERNAL_PREDICTIVE', 'MARKET_STRUCTURE', 'MARKET_DYNAMICS', "
        "'CROSS_MARKET', 'OPERATOR_SUPPLIED', 'FIXTURE_ONLY')",
    )
    dynamics_metadata.create_all(
        bind=op.get_bind(), tables=MARKET_DYNAMICS_TABLES, checkfirst=False
    )
    for table_name in MARKET_DYNAMICS_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))


def downgrade() -> None:
    """Refuse before DDL when retained forward-validation evidence would be lost."""
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM market_dynamics_feature_snapshot) "
            "OR EXISTS (SELECT 1 FROM market_dynamics_prediction_feature) "
            "OR EXISTS (SELECT 1 FROM strategy_governance WHERE family = 'MARKET_DYNAMICS') "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0019 with retained market dynamics shadow evidence'; "
            "END IF; END $$"
        )
    )
    for table_name in MARKET_DYNAMICS_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    dynamics_metadata.drop_all(bind=op.get_bind(), tables=MARKET_DYNAMICS_TABLES)
    op.drop_constraint("ck_strategy_governance_family", "strategy_governance", type_="check")
    op.create_check_constraint(
        "ck_strategy_governance_family",
        "strategy_governance",
        "family IN ('EXTERNAL_PREDICTIVE', 'MARKET_STRUCTURE', 'CROSS_MARKET', "
        "'OPERATOR_SUPPLIED', 'FIXTURE_ONLY')",
    )
