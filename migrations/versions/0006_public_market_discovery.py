"""Persist immutable, read-only public market discovery evidence.

Revision ID: 0006_public_market_discovery
Revises: 0005_durable_void_settlement
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    PUBLIC_MARKET_IMMUTABLE_TABLES,
    immutable_trigger_ddl,
    market_discovery_metadata,
)

revision: str = "0006_public_market_discovery"
down_revision: str | None = "0005_durable_void_settlement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INGESTION_RUN_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_provider_ingestion_run() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'public market ingestion run cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY['state', 'completed_at', 'page_count', 'market_count'])
       <> (to_jsonb(NEW) - ARRAY['state', 'completed_at', 'page_count', 'market_count']) THEN
        RAISE EXCEPTION 'public market ingestion configuration is immutable';
    END IF;
    IF OLD.state <> 'RUNNING'
       OR NEW.state NOT IN ('COMPLETED', 'FAILED')
       OR NEW.completed_at IS NULL
       OR NEW.page_count < OLD.page_count
       OR NEW.market_count < OLD.market_count THEN
        RAISE EXCEPTION 'public market ingestion transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER apex_guard_provider_ingestion_run_trigger
BEFORE UPDATE OR DELETE ON provider_ingestion_run
FOR EACH ROW EXECUTE FUNCTION apex_guard_provider_ingestion_run();
"""


def upgrade() -> None:
    """Create only the isolated public evidence tables and narrow guards."""

    market_discovery_metadata.create_all(bind=op.get_bind(), checkfirst=False)
    for table_name in PUBLIC_MARKET_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(INGESTION_RUN_GUARD_DDL))


def downgrade() -> None:
    """Drop the isolated boundary only when no captured evidence would be lost."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM provider_ingestion_run) "
            "OR EXISTS (SELECT 1 FROM raw_provider_response) "
            "OR EXISTS (SELECT 1 FROM normalized_market_snapshot) "
            "OR EXISTS (SELECT 1 FROM shadow_market_evaluation) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0006 with retained public market discovery evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text("DROP TRIGGER apex_guard_provider_ingestion_run_trigger ON provider_ingestion_run")
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_provider_ingestion_run()"))
    for table_name in PUBLIC_MARKET_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    market_discovery_metadata.drop_all(bind=op.get_bind(), checkfirst=False)
