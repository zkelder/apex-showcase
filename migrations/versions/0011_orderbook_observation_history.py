"""Persist immutable same-market public orderbook observation history.

Revision ID: 0011_orderbook_history
Revises: 0010_public_orderbook_complement
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    PUBLIC_ORDERBOOK_HISTORY_IMMUTABLE_TABLES,
    immutable_trigger_ddl,
    orderbook_history_metadata,
)

revision: str = "0011_orderbook_history"
down_revision: str | None = "0010_public_orderbook_complement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ORDERBOOK_HISTORY_LINEAGE_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_orderbook_history_analysis() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM market_orderbook_snapshot
         WHERE orderbook_snapshot_id = NEW.first_orderbook_snapshot_id
           AND provider = NEW.provider
           AND provider_market_ticker = NEW.provider_market_ticker
    ) OR NOT EXISTS (
        SELECT 1 FROM market_orderbook_snapshot
         WHERE orderbook_snapshot_id = NEW.last_orderbook_snapshot_id
           AND provider = NEW.provider
           AND provider_market_ticker = NEW.provider_market_ticker
    ) THEN
        RAISE EXCEPTION 'orderbook history analysis requires exact Kalshi market boundaries';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION apex_guard_orderbook_history_observation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    analysis_provider text;
    analysis_ticker text;
BEGIN
    SELECT provider, provider_market_ticker
      INTO analysis_provider, analysis_ticker
      FROM orderbook_history_analysis
     WHERE analysis_id = NEW.analysis_id;
    IF analysis_provider IS NULL THEN
        RAISE EXCEPTION 'orderbook history observation requires retained analysis';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM market_orderbook_snapshot AS book
          JOIN binary_complement_evaluation AS evaluation
            ON evaluation.orderbook_snapshot_id = book.orderbook_snapshot_id
          JOIN normalized_market_snapshot AS market
            ON market.snapshot_id = book.market_snapshot_id
          JOIN raw_provider_response AS raw
            ON raw.raw_response_id = book.raw_response_id
         WHERE book.orderbook_snapshot_id = NEW.orderbook_snapshot_id
           AND evaluation.evaluation_id = NEW.evaluation_id
           AND market.snapshot_id = NEW.market_snapshot_id
           AND book.provider = analysis_provider
           AND book.provider_market_ticker = analysis_ticker
           AND evaluation.provider = analysis_provider
           AND evaluation.provider_market_ticker = analysis_ticker
           AND raw.provider = analysis_provider
           AND raw.endpoint_identity = 'KALSHI_GET_MARKET_ORDERBOOK'
           AND NEW.rule_evidence_hash IS NOT DISTINCT FROM market.rule_content_hash
           AND NEW.series_ticker = book.series_ticker
    ) THEN
        RAISE EXCEPTION
            'orderbook history observation requires exact market, evaluation, and raw lineage';
    END IF;
    IF NEW.fee_snapshot_id IS NULL THEN
        IF NEW.fee_evidence_hash IS NOT NULL OR NEW.fee_schedule_identity IS NOT NULL
           OR NEW.fee_multiplier IS NOT NULL OR NEW.fee_model_identity IS NOT NULL THEN
            RAISE EXCEPTION
                'orderbook history observation without fee evidence cannot retain fee fields';
        END IF;
    ELSIF NOT EXISTS (
        SELECT 1 FROM series_fee_snapshot AS fee
          JOIN market_orderbook_snapshot AS book
            ON book.orderbook_snapshot_id = NEW.orderbook_snapshot_id
         WHERE fee.fee_snapshot_id = NEW.fee_snapshot_id
           AND fee.provider = analysis_provider
           AND fee.series_ticker = book.series_ticker
           AND NEW.fee_evidence_hash = fee.canonical_fee_hash
           AND NEW.fee_schedule_identity = fee.schedule_identity
           AND NEW.fee_multiplier IS NOT DISTINCT FROM fee.fee_multiplier
           AND NEW.fee_model_identity = fee.fee_model_identity
           AND NEW.rounding_identity = fee.rounding_identity
    ) THEN
        RAISE EXCEPTION
            'orderbook history observation fee evidence does not match its market series';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER apex_guard_orderbook_history_analysis_trigger
BEFORE INSERT ON orderbook_history_analysis
FOR EACH ROW EXECUTE FUNCTION apex_guard_orderbook_history_analysis();

CREATE TRIGGER apex_guard_orderbook_history_observation_trigger
BEFORE INSERT ON orderbook_history_observation
FOR EACH ROW EXECUTE FUNCTION apex_guard_orderbook_history_observation();
"""


def upgrade() -> None:
    """Add only immutable orderbook-history projections over retained evidence."""

    orderbook_history_metadata.create_all(bind=op.get_bind(), checkfirst=False)
    for table_name in PUBLIC_ORDERBOOK_HISTORY_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(ORDERBOOK_HISTORY_LINEAGE_GUARD_DDL))


def downgrade() -> None:
    """Refuse before DDL whenever retained orderbook history would be lost."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM orderbook_history_analysis) "
            "OR EXISTS (SELECT 1 FROM orderbook_history_observation) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0011 with retained orderbook observation history evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_orderbook_history_observation_trigger "
            "ON orderbook_history_observation"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_orderbook_history_analysis_trigger "
            "ON orderbook_history_analysis"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_orderbook_history_observation()"))
    op.execute(sa.text("DROP FUNCTION apex_guard_orderbook_history_analysis()"))
    for table_name in PUBLIC_ORDERBOOK_HISTORY_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    orderbook_history_metadata.drop_all(bind=op.get_bind(), checkfirst=False)
