"""Persist immutable public events, exact relationships, and shadow consistency evidence.

Revision ID: 0007_public_event_consistency
Revises: 0006_public_market_discovery
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    PUBLIC_EVENT_IMMUTABLE_TABLES,
    event_discovery_metadata,
    immutable_trigger_ddl,
)

revision: str = "0007_public_event_consistency"
down_revision: str | None = "0006_public_market_discovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EVENT_RELATIONSHIP_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_event_market_relationship() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    event_provider text;
    event_ticker text;
    market_provider text;
    market_event_ticker text;
    market_ticker text;
BEGIN
    SELECT event_snapshot.provider, event_snapshot.provider_event_ticker
      INTO event_provider, event_ticker
      FROM normalized_event_snapshot AS event_snapshot
     WHERE event_snapshot.event_snapshot_id = NEW.event_snapshot_id;
    SELECT market_snapshot.provider,
           market_snapshot.event_ticker,
           market_snapshot.provider_market_ticker
      INTO market_provider, market_event_ticker, market_ticker
      FROM normalized_market_snapshot AS market_snapshot
     WHERE market_snapshot.snapshot_id = NEW.market_snapshot_id;
    IF event_provider IS NULL OR market_provider IS NULL
       OR NEW.provider <> event_provider
       OR NEW.provider <> market_provider
       OR NEW.provider_event_ticker <> event_ticker
       OR NEW.provider_event_ticker <> market_event_ticker
       OR NEW.provider_market_ticker <> market_ticker THEN
        RAISE EXCEPTION 'event-market relationship requires exact provider and ticker lineage';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER apex_guard_event_market_relationship_trigger
BEFORE INSERT ON event_market_relationship
FOR EACH ROW EXECUTE FUNCTION apex_guard_event_market_relationship();
"""


def upgrade() -> None:
    """Add only read-only event/relationship/evaluation evidence to the 0006 boundary."""

    op.drop_constraint(
        "ck_provider_ingestion_run_capability", "provider_ingestion_run", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_ingestion_run_capability",
        "provider_ingestion_run",
        "capability IN ('PUBLIC_REST_MARKET_DISCOVERY', 'PUBLIC_REST_EVENT_DISCOVERY')",
    )
    op.drop_constraint("ck_raw_provider_response_endpoint", "raw_provider_response", type_="check")
    op.create_check_constraint(
        "ck_raw_provider_response_endpoint",
        "raw_provider_response",
        "endpoint_identity IN ('KALSHI_GET_MARKETS', 'KALSHI_GET_EVENTS')",
    )
    event_discovery_metadata.create_all(bind=op.get_bind(), checkfirst=False)
    for table_name in PUBLIC_EVENT_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(EVENT_RELATIONSHIP_GUARD_DDL))


def downgrade() -> None:
    """Refuse before any schema operation when event-only durable evidence is retained."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM normalized_event_snapshot) "
            "OR EXISTS (SELECT 1 FROM event_market_relationship) "
            "OR EXISTS (SELECT 1 FROM consistency_evaluation) "
            "OR EXISTS (SELECT 1 FROM provider_ingestion_run "
            "WHERE capability = 'PUBLIC_REST_EVENT_DISCOVERY') "
            "OR EXISTS (SELECT 1 FROM raw_provider_response "
            "WHERE endpoint_identity = 'KALSHI_GET_EVENTS') "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0007 with retained public event consistency evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_event_market_relationship_trigger ON event_market_relationship"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_event_market_relationship()"))
    for table_name in PUBLIC_EVENT_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    event_discovery_metadata.drop_all(bind=op.get_bind(), checkfirst=False)
    op.drop_constraint("ck_raw_provider_response_endpoint", "raw_provider_response", type_="check")
    op.create_check_constraint(
        "ck_raw_provider_response_endpoint",
        "raw_provider_response",
        "endpoint_identity = 'KALSHI_GET_MARKETS'",
    )
    op.drop_constraint(
        "ck_provider_ingestion_run_capability", "provider_ingestion_run", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_ingestion_run_capability",
        "provider_ingestion_run",
        "capability = 'PUBLIC_REST_MARKET_DISCOVERY'",
    )
