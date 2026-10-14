"""Persist immutable public orderbook and fee-aware complement evidence.

Revision ID: 0010_public_orderbook_complement
Revises: 0009_event_observation_history
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    PUBLIC_COMPLEMENT_IMMUTABLE_TABLES,
    complement_metadata,
    immutable_trigger_ddl,
)

revision: str = "0010_public_orderbook_complement"
down_revision: str | None = "0009_event_observation_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RAW_ENDPOINT_CHECK = (
    "endpoint_identity IN ('KALSHI_GET_MARKETS', 'KALSHI_GET_EVENTS', "
    "'KALSHI_GET_MARKET_ORDERBOOK', 'KALSHI_GET_SERIES', "
    "'KALSHI_GET_SERIES_FEE_CHANGES')"
)
PRIOR_RAW_ENDPOINT_CHECK = "endpoint_identity IN ('KALSHI_GET_MARKETS', 'KALSHI_GET_EVENTS')"


COMPLEMENT_LINEAGE_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_series_fee_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM raw_provider_response
         WHERE raw_response_id = NEW.raw_response_id
           AND ingestion_run_id = NEW.ingestion_run_id
           AND provider = NEW.provider
           AND endpoint_identity IN ('KALSHI_GET_SERIES', 'KALSHI_GET_SERIES_FEE_CHANGES')
    ) THEN
        RAISE EXCEPTION 'series fee snapshot requires exact public series raw lineage';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION apex_guard_market_orderbook_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM raw_provider_response AS raw
          JOIN normalized_market_snapshot AS market
            ON market.snapshot_id = NEW.market_snapshot_id
         WHERE raw.raw_response_id = NEW.raw_response_id
           AND raw.ingestion_run_id = NEW.ingestion_run_id
           AND raw.provider = NEW.provider
           AND raw.endpoint_identity = 'KALSHI_GET_MARKET_ORDERBOOK'
           AND market.ingestion_run_id = NEW.ingestion_run_id
           AND market.provider = NEW.provider
           AND market.provider_market_ticker = NEW.provider_market_ticker
    ) THEN
        RAISE EXCEPTION 'orderbook snapshot requires exact market and orderbook raw lineage';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION apex_guard_binary_complement_evaluation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM market_orderbook_snapshot
         WHERE orderbook_snapshot_id = NEW.orderbook_snapshot_id
           AND ingestion_run_id = NEW.ingestion_run_id
           AND market_snapshot_id = NEW.market_snapshot_id
           AND provider = NEW.provider
           AND provider_market_ticker = NEW.provider_market_ticker
    ) THEN
        RAISE EXCEPTION 'binary complement evaluation requires exact orderbook lineage';
    END IF;
    IF NEW.fee_snapshot_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
          FROM series_fee_snapshot AS fee
          JOIN market_orderbook_snapshot AS book
            ON book.orderbook_snapshot_id = NEW.orderbook_snapshot_id
         WHERE fee.fee_snapshot_id = NEW.fee_snapshot_id
           AND fee.ingestion_run_id = NEW.ingestion_run_id
           AND fee.provider = NEW.provider
           AND fee.series_ticker = book.series_ticker
    ) THEN
        RAISE EXCEPTION 'binary complement evaluation fee lineage does not match the market series';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION apex_guard_binary_complement_leg() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM binary_complement_evaluation AS evaluation
          JOIN market_orderbook_level AS level
            ON level.orderbook_snapshot_id = evaluation.orderbook_snapshot_id
         WHERE evaluation.evaluation_id = NEW.evaluation_id
           AND level.level_id = NEW.source_level_id
           AND level.derived_ask_side = NEW.purchased_side
           AND level.derived_ask_price = NEW.ask_price
    ) THEN
        RAISE EXCEPTION 'binary complement leg requires exact derived-ask level lineage';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER apex_guard_series_fee_snapshot_trigger
BEFORE INSERT ON series_fee_snapshot
FOR EACH ROW EXECUTE FUNCTION apex_guard_series_fee_snapshot();

CREATE TRIGGER apex_guard_market_orderbook_snapshot_trigger
BEFORE INSERT ON market_orderbook_snapshot
FOR EACH ROW EXECUTE FUNCTION apex_guard_market_orderbook_snapshot();

CREATE TRIGGER apex_guard_binary_complement_evaluation_trigger
BEFORE INSERT ON binary_complement_evaluation
FOR EACH ROW EXECUTE FUNCTION apex_guard_binary_complement_evaluation();

CREATE TRIGGER apex_guard_binary_complement_leg_trigger
BEFORE INSERT ON binary_complement_leg
FOR EACH ROW EXECUTE FUNCTION apex_guard_binary_complement_leg();
"""


def upgrade() -> None:
    """Add only immutable public orderbook/fee evidence and its projection."""

    op.drop_constraint("ck_raw_provider_response_endpoint", "raw_provider_response", type_="check")
    op.create_check_constraint(
        "ck_raw_provider_response_endpoint", "raw_provider_response", RAW_ENDPOINT_CHECK
    )
    complement_metadata.create_all(bind=op.get_bind(), checkfirst=False)
    for table_name in PUBLIC_COMPLEMENT_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(COMPLEMENT_LINEAGE_GUARD_DDL))


def downgrade() -> None:
    """Refuse before any DDL if public orderbook/fee evidence would be lost."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM series_fee_snapshot) "
            "OR EXISTS (SELECT 1 FROM market_orderbook_snapshot) "
            "OR EXISTS (SELECT 1 FROM market_orderbook_level) "
            "OR EXISTS (SELECT 1 FROM binary_complement_evaluation) "
            "OR EXISTS (SELECT 1 FROM binary_complement_leg) "
            "OR EXISTS (SELECT 1 FROM raw_provider_response WHERE endpoint_identity IN "
            "('KALSHI_GET_MARKET_ORDERBOOK', 'KALSHI_GET_SERIES', "
            "'KALSHI_GET_SERIES_FEE_CHANGES')) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0010 with retained public orderbook or fee evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text("DROP TRIGGER apex_guard_binary_complement_leg_trigger ON binary_complement_leg")
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_binary_complement_evaluation_trigger "
            "ON binary_complement_evaluation"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_market_orderbook_snapshot_trigger ON market_orderbook_snapshot"
        )
    )
    op.execute(
        sa.text("DROP TRIGGER apex_guard_series_fee_snapshot_trigger ON series_fee_snapshot")
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_binary_complement_leg()"))
    op.execute(sa.text("DROP FUNCTION apex_guard_binary_complement_evaluation()"))
    op.execute(sa.text("DROP FUNCTION apex_guard_market_orderbook_snapshot()"))
    op.execute(sa.text("DROP FUNCTION apex_guard_series_fee_snapshot()"))
    for table_name in PUBLIC_COMPLEMENT_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    complement_metadata.drop_all(bind=op.get_bind(), checkfirst=False)
    op.drop_constraint("ck_raw_provider_response_endpoint", "raw_provider_response", type_="check")
    op.create_check_constraint(
        "ck_raw_provider_response_endpoint", "raw_provider_response", PRIOR_RAW_ENDPOINT_CHECK
    )
