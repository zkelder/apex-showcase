from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.kalshi_capture_fixtures import VirtualCaptureClock
from apex.adapters.kalshi_public import KalshiPublicMarketNormalizer
from apex.adapters.kalshi_sweep_fixtures import (
    KalshiFixtureOrderbookSweepClient,
)
from apex.adapters.postgres import (
    postgres_bounded_orderbook_capture_store,
    postgres_public_market_discovery_store,
)
from apex.application.capture_public_orderbook_session import (
    CaptureBoundedPublicOrderbookSession,
)
from apex.application.discover_public_markets import DiscoverPublicPredictionMarkets
from apex.application.sweep_public_kalshi_orderbooks import (
    PublicKalshiOrderbookSweepRequest,
    PublicKalshiOrderbookSweepState,
    SweepPublicKalshiOrderbooks,
)

pytestmark = pytest.mark.postgres


def test_sweep_persists_only_existing_public_evidence_and_no_financial_state(
    postgres_engine: Engine,
) -> None:
    financial_tables = (
        "campaign",
        "ledger_journal",
        "risk_decision",
        "risk_reservation",
        '"order"',
        "execution_event",
        "position",
        "settlement_fact",
        "position_settlement",
    )
    with postgres_engine.connect() as connection:
        before = {
            table: connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
            for table in financial_tables
        }

    clock = VirtualCaptureClock()
    client = KalshiFixtureOrderbookSweepClient(clock)
    discovery_store = postgres_public_market_discovery_store(postgres_engine)
    capture_store = postgres_bounded_orderbook_capture_store(postgres_engine)
    capture = CaptureBoundedPublicOrderbookSession(
        client=client,
        store=capture_store,
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
        normalizer=KalshiPublicMarketNormalizer(),
    )

    report = SweepPublicKalshiOrderbooks(
        discovery=DiscoverPublicPredictionMarkets(
            client=client,
            normalizer=KalshiPublicMarketNormalizer(),
            store=discovery_store,
            clock=clock.wall_clock,
        ),
        capture=capture,
        candidate_selector=lambda run_id, limit: capture_store.select_capture_candidates(
            ingestion_run_id=run_id, limit=limit
        ),
    ).sweep(
        PublicKalshiOrderbookSweepRequest(
            sweep_id="postgres-public-orderbook-sweep-v1",
            market_count=3,
            maximum_pages=3,
            market_limit=50,
            maximum_enrichments=3,
            observations=3,
            interval_seconds=5,
            maximum_depth=5,
            maximum_bundle_quantity="10",
            timeout_seconds=10,
        )
    )

    assert report.state is PublicKalshiOrderbookSweepState.COMPLETED
    assert report.selected_market_count == 3
    assert all(value.successful_observation_count == 3 for value in report.markets)
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM orderbook_capture_session")) == 3
        assert connection.scalar(sa.text("SELECT count(*) FROM orderbook_capture_observation")) == 9
        assert connection.scalar(sa.text("SELECT count(*) FROM orderbook_history_analysis")) == 3
        assert connection.scalar(sa.text("SELECT count(*) FROM orderbook_history_observation")) == 9
        for table, count in before.items():
            assert connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) == count
