from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex.adapters.kalshi_consistency_fixtures import KalshiFixturePublicConsistencyClient
from apex.adapters.kalshi_public import KalshiPublicEventNormalizer, KalshiPublicMarketNormalizer
from apex.adapters.postgres import (
    postgres_public_event_consistency_store,
    postgres_public_market_discovery_store,
)
from apex.application.discover_public_events import (
    DiscoverPublicEventConsistency,
    PublicEventScanRequest,
)
from apex.application.discover_public_markets import (
    DiscoverPublicPredictionMarkets,
    PublicMarketScanRequest,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
REVISION_0006 = "0006_public_market_discovery"
REVISION_0007 = "0007_public_event_consistency"
REVISION_0008 = "0008_persisted_scan_comparison"
EVENT_TABLES = {
    "normalized_event_snapshot",
    "event_market_relationship",
    "consistency_evaluation",
}


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _migrate(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        command.upgrade(_config(connection), revision)
        connection.commit()


def _downgrade(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        command.downgrade(_config(connection), revision)
        connection.commit()


def _current_revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _discover_market(engine: Engine, *, run_key: str) -> None:
    DiscoverPublicPredictionMarkets(
        client=KalshiFixturePublicConsistencyClient(),
        normalizer=KalshiPublicMarketNormalizer(),
        store=postgres_public_market_discovery_store(engine),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    ).discover(PublicMarketScanRequest(run_key=run_key, maximum_pages=3))


def _discover_events(engine: Engine, *, market_run_key: str, event_run_key: str):
    _discover_market(engine, run_key=market_run_key)
    return DiscoverPublicEventConsistency(
        client=KalshiFixturePublicConsistencyClient(),
        normalizer=KalshiPublicEventNormalizer(),
        store=postgres_public_event_consistency_store(engine),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    ).discover(PublicEventScanRequest(run_key=event_run_key, maximum_pages=3))


def test_0007_creates_only_event_relationship_and_evaluation_evidence_schema(
    postgres_engine: Engine,
) -> None:
    inspector = inspect(postgres_engine)
    assert EVENT_TABLES.issubset(inspector.get_table_names())
    event_columns = {
        column["name"]: column for column in inspector.get_columns("normalized_event_snapshot")
    }
    evaluation_columns = {
        column["name"]: column for column in inspector.get_columns("consistency_evaluation")
    }
    assert isinstance(event_columns["mutually_exclusive"]["type"], sa.Boolean)
    assert isinstance(evaluation_columns["yes_ask_sum"]["type"], sa.Numeric)
    assert evaluation_columns["yes_ask_sum"]["type"].precision == 38
    assert evaluation_columns["yes_ask_sum"]["type"].scale == 18
    financial_tables = {
        "campaign",
        "ledger_journal",
        "risk_decision",
        "risk_reservation",
        "order",
        "position",
        "settlement_fact",
    }
    for table_name in EVENT_TABLES:
        assert not financial_tables.intersection(
            key["referred_table"] for key in inspector.get_foreign_keys(table_name)
        )


def test_event_scan_persists_exact_immutable_lineage_without_financial_state(
    postgres_engine: Engine,
) -> None:
    financial_tables = ("campaign", "ledger_journal", "risk_decision", "order", "position")
    with postgres_engine.connect() as connection:
        before = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
            for table in financial_tables
        }

    first = _discover_events(
        postgres_engine, market_run_key="postgres-event-markets", event_run_key="postgres-events"
    )
    replay = _discover_events(
        postgres_engine, market_run_key="postgres-event-markets", event_run_key="postgres-events"
    )

    assert first.candidate_count == 1
    assert first.ranked_event_tickers == ("EVT-CANDIDATE",)
    assert replay.idempotent_replay
    with postgres_engine.connect() as connection:
        raw_rows = (
            connection.execute(
                sa.text(
                    "SELECT raw_payload, raw_payload_sha256, endpoint_identity "
                    "FROM raw_provider_response "
                    "ORDER BY endpoint_identity, request_page_ordinal"
                )
            )
            .mappings()
            .all()
        )
        assert {row["endpoint_identity"] for row in raw_rows} == {
            "KALSHI_GET_EVENTS",
            "KALSHI_GET_MARKETS",
        }
        assert all(
            sha256(bytes(row["raw_payload"])).hexdigest() == row["raw_payload_sha256"]
            for row in raw_rows
        )
        assert connection.scalar(sa.text("SELECT count(*) FROM normalized_event_snapshot")) == 9
        assert connection.scalar(sa.text("SELECT count(*) FROM event_market_relationship")) == 12
        assert connection.scalar(sa.text("SELECT count(*) FROM consistency_evaluation")) == 9
        for table, count in before.items():
            assert connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) == count

    with (
        postgres_engine.begin() as connection,
        pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"),
    ):
        connection.execute(sa.text("UPDATE consistency_evaluation SET quote_coverage_count = 0"))
    with (
        postgres_engine.begin() as connection,
        pytest.raises(sa.exc.DBAPIError, match="exact provider and ticker lineage"),
    ):
        connection.execute(
            sa.text(
                "INSERT INTO event_market_relationship "
                "(relationship_id, event_snapshot_id, market_snapshot_id, provider, "
                "provider_event_ticker, provider_market_ticker, relationship_ordinal, "
                "market_rule_content_hash, event_nested_rule_content_hash, "
                "event_settlement_source_hash, nested_settlement_source_hash, "
                "event_nested_close_time, relationship_state, "
                "canonical_relationship_hash, created_at) "
                "SELECT 'bad-event-market-relationship', event_snapshot_id, market_snapshot_id, "
                "provider, provider_event_ticker, 'wrong-ticker', relationship_ordinal, "
                "market_rule_content_hash, event_nested_rule_content_hash, "
                "event_settlement_source_hash, nested_settlement_source_hash, "
                "event_nested_close_time, relationship_state, "
                "canonical_relationship_hash, created_at "
                "FROM event_market_relationship LIMIT 1"
            )
        )


def test_empty_0007_downgrades_to_0006_and_reupgrades_with_market_and_campaign_data(
    postgres_engine: Engine,
    initialize_campaign,
) -> None:
    initialize_campaign(campaign_id="campaign-before-0007")
    _downgrade(postgres_engine, REVISION_0006)
    try:
        assert _current_revision(postgres_engine) == REVISION_0006
        assert EVENT_TABLES.isdisjoint(inspect(postgres_engine).get_table_names())
        _discover_market(postgres_engine, run_key="market-survives-0007-downgrade")
        _migrate(postgres_engine, "head")
        assert _current_revision(postgres_engine) == ("0118_evidence_gap_recovery")
        assert EVENT_TABLES.issubset(inspect(postgres_engine).get_table_names())
        with postgres_engine.connect() as connection:
            market_count = connection.scalar(
                sa.text("SELECT count(*) FROM normalized_market_snapshot")
            )
            assert market_count == 12
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM campaign WHERE campaign_id = 'campaign-before-0007'"
                    )
                )
                == 1
            )
    finally:
        _migrate(postgres_engine, "head")


def test_retained_event_evidence_blocks_0007_downgrade_without_partial_loss(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0007)
    try:
        _discover_events(
            postgres_engine,
            market_run_key="retained-event-markets",
            event_run_key="retained-event-evidence",
        )
        with postgres_engine.connect() as connection:
            tables_before = set(inspect(postgres_engine).get_table_names())
            evidence_before = {
                table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
                for table in (
                    "provider_ingestion_run",
                    "raw_provider_response",
                    *sorted(EVENT_TABLES),
                    "ledger_journal",
                    "ledger_posting",
                )
            }

        with pytest.raises(Exception, match="retained public event consistency evidence"):
            _downgrade(postgres_engine, REVISION_0006)

        assert _current_revision(postgres_engine) == REVISION_0007
        assert set(inspect(postgres_engine).get_table_names()) == tables_before
        with postgres_engine.connect() as connection:
            evidence_after = {
                table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
                for table in evidence_before
            }
        assert evidence_after == evidence_before
    finally:
        _migrate(postgres_engine, "head")
