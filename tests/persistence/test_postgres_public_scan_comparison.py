from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    postgres_public_scan_comparison_store,
)
from apex.application.compare_public_scans import (
    ComparePublicMarketScans,
    PublicScanComparisonError,
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
REVISION_0007 = "0007_public_event_consistency"
REVISION_0008 = "0008_persisted_scan_comparison"
COMPARISON_TABLES = {
    "scan_comparison",
    "market_snapshot_delta",
    "event_consistency_transition",
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


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _persist_scan(engine: Engine, *, run_key: str, offset: timedelta) -> str:
    client = KalshiFixturePublicConsistencyClient(received_at_offset=offset)
    summary = DiscoverPublicPredictionMarkets(
        client=client,
        normalizer=KalshiPublicMarketNormalizer(),
        store=postgres_public_market_discovery_store(engine),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC) + offset,
    ).discover(PublicMarketScanRequest(run_key=run_key, maximum_pages=3))
    DiscoverPublicEventConsistency(
        client=client,
        normalizer=KalshiPublicEventNormalizer(),
        store=postgres_public_event_consistency_store(engine),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC) + offset,
    ).discover(
        PublicEventScanRequest(
            run_key=f"{run_key}-events",
            maximum_pages=3,
            market_ingestion_run_id=summary.ingestion_run_id,
        )
    )
    return summary.ingestion_run_id


def test_0008_isolated_schema_and_empty_downgrade_reupgrade(postgres_engine: Engine) -> None:
    inspector = inspect(postgres_engine)
    assert COMPARISON_TABLES.issubset(inspector.get_table_names())
    financial_tables = {"campaign", "ledger_journal", "risk_decision", "order", "position"}
    for table_name in COMPARISON_TABLES:
        assert not financial_tables.intersection(
            foreign_key["referred_table"] for foreign_key in inspector.get_foreign_keys(table_name)
        )
    _downgrade(postgres_engine, REVISION_0007)
    try:
        assert _revision(postgres_engine) == REVISION_0007
        assert COMPARISON_TABLES.isdisjoint(inspect(postgres_engine).get_table_names())
        _migrate(postgres_engine, "head")
        assert _revision(postgres_engine) == "0118_evidence_gap_recovery"
    finally:
        _migrate(postgres_engine, "head")


def test_comparison_is_idempotent_atomic_and_does_not_mutate_financial_state(
    postgres_engine: Engine,
) -> None:
    baseline_id = _persist_scan(postgres_engine, run_key="comparison-baseline", offset=timedelta(0))
    current_id = _persist_scan(
        postgres_engine, run_key="comparison-current", offset=timedelta(minutes=10)
    )
    financial_tables = ("campaign", "ledger_journal", "risk_decision", "order", "position")
    with postgres_engine.connect() as connection:
        before = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
            for table in financial_tables
        }
    service = ComparePublicMarketScans(store=postgres_public_scan_comparison_store(postgres_engine))
    first = service.compare(
        baseline_ingestion_run_id=baseline_id,
        current_ingestion_run_id=current_id,
    )
    restarted = ComparePublicMarketScans(
        store=postgres_public_scan_comparison_store(postgres_engine)
    ).compare(
        baseline_ingestion_run_id=baseline_id,
        current_ingestion_run_id=current_id,
    )
    assert first.comparison.matched_market_count == 12
    assert restarted.idempotent_replay
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM scan_comparison")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM market_snapshot_delta")) == 12
        assert connection.scalar(sa.text("SELECT count(*) FROM event_consistency_transition")) == 8
        for table, count in before.items():
            assert connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) == count

    with pytest.raises(PublicScanComparisonError, match="precede"):
        service.compare(
            baseline_ingestion_run_id=current_id,
            current_ingestion_run_id=baseline_id,
        )
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM scan_comparison")) == 1


def test_retained_comparison_evidence_blocks_0008_downgrade_atomically(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0008)
    try:
        baseline_id = _persist_scan(
            postgres_engine, run_key="comparison-guard-baseline", offset=timedelta(0)
        )
        current_id = _persist_scan(
            postgres_engine, run_key="comparison-guard-current", offset=timedelta(minutes=10)
        )
        ComparePublicMarketScans(
            store=postgres_public_scan_comparison_store(postgres_engine)
        ).compare(
            baseline_ingestion_run_id=baseline_id,
            current_ingestion_run_id=current_id,
        )
        with postgres_engine.connect() as connection:
            before = {
                table: connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
                for table in COMPARISON_TABLES
            }
        with pytest.raises(Exception, match="retained persisted scan comparison evidence"):
            _downgrade(postgres_engine, REVISION_0007)
        assert _revision(postgres_engine) == REVISION_0008
        with postgres_engine.connect() as connection:
            after = {
                table: connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
                for table in COMPARISON_TABLES
            }
        assert after == before
    finally:
        _migrate(postgres_engine, "head")
