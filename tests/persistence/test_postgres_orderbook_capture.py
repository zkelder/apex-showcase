from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex.adapters.kalshi_capture_fixtures import (
    MARKET_TICKER,
    KalshiFixtureOrderbookCaptureClient,
    VirtualCaptureClock,
)
from apex.adapters.kalshi_market_fixtures import KalshiFixtureMarketDataClient
from apex.adapters.kalshi_public import KalshiPublicMarketNormalizer
from apex.adapters.postgres import (
    PostgresSettings,
    create_postgres_engine,
    postgres_bounded_orderbook_capture_store,
    postgres_public_market_discovery_store,
)
from apex.application.capture_public_orderbook_session import (
    BoundedOrderbookCaptureRequest,
    CaptureBoundedPublicOrderbookSession,
    CaptureMarketCandidate,
    InMemoryPublicOrderbookCaptureStore,
    OrderbookCaptureSessionState,
)
from apex.application.discover_public_markets import (
    DiscoverPublicPredictionMarkets,
    PublicMarketScanRequest,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
REVISION_0011 = "0011_orderbook_history"
REVISION_0012 = "0012_orderbook_capture"
HEAD_REVISION = "0118_evidence_gap_recovery"
CAPTURE_TABLES = {"orderbook_capture_session", "orderbook_capture_observation"}


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


def _request(session_id: str = "postgres-capture-v1") -> BoundedOrderbookCaptureRequest:
    return BoundedOrderbookCaptureRequest(
        session_id=session_id,
        market_ticker=MARKET_TICKER,
        observations=5,
        interval_seconds=5,
        maximum_depth=5,
        maximum_bundle_quantity="10",
        timeout_seconds=10,
    )


def _service(engine: Engine, clock: VirtualCaptureClock) -> CaptureBoundedPublicOrderbookSession:
    return CaptureBoundedPublicOrderbookSession(
        client=KalshiFixtureOrderbookCaptureClient(clock),
        store=postgres_bounded_orderbook_capture_store(engine),
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
        normalizer=KalshiPublicMarketNormalizer(),
    )


def _discover(engine: Engine, *, run_key: str):
    return DiscoverPublicPredictionMarkets(
        client=KalshiFixtureMarketDataClient(),
        normalizer=KalshiPublicMarketNormalizer(),
        store=postgres_public_market_discovery_store(engine),
        clock=lambda: VirtualCaptureClock().wall_clock(),
    ).discover(PublicMarketScanRequest(run_key=run_key, page_size=25, maximum_pages=3))


def test_0012_empty_schema_downgrades_and_reupgrades(postgres_engine: Engine) -> None:
    assert CAPTURE_TABLES.issubset(inspect(postgres_engine).get_table_names())
    _downgrade(postgres_engine, REVISION_0011)
    try:
        assert _revision(postgres_engine) == REVISION_0011
        assert CAPTURE_TABLES.isdisjoint(inspect(postgres_engine).get_table_names())
        _migrate(postgres_engine, "head")
        assert _revision(postgres_engine) == HEAD_REVISION
    finally:
        _migrate(postgres_engine, "head")


def test_capture_selection_diagnostic_groups_retained_structural_reasons(
    postgres_engine: Engine,
) -> None:
    summary = _discover(postgres_engine, run_key="capture-selection-diagnostic")

    diagnostic = postgres_bounded_orderbook_capture_store(postgres_engine).selection_diagnostics(
        ingestion_run_id=summary.ingestion_run_id
    )

    assert diagnostic.provider_market_count == 4
    assert diagnostic.successfully_parsed_count == 3
    assert diagnostic.structurally_invalid_count == 1
    assert diagnostic.list_series_identity_absent_count == 4
    assert diagnostic.structurally_eligible_pre_enrichment_count == 3
    assert diagnostic.pre_enrichment_tickers == (
        "KXTEST-SECOND",
        "KXTEST-INCOMPLETE",
        "KXTEST-VALID",
    )
    assert diagnostic.reason_counts == (
        ("CLOSE_TIME_NOT_AFTER_OBSERVATION", 1),
        ("MARKET_NOT_OPEN", 1),
        ("PROVIDER_DATA_INVALID", 1),
        ("RULE_TEXT_MISSING", 1),
    )


def test_capture_candidates_remain_eligible_without_list_series_identity_and_sort_deterministically(
    postgres_engine: Engine,
) -> None:
    summary = _discover(postgres_engine, run_key="capture-event-enrichment-candidates")

    candidates = postgres_bounded_orderbook_capture_store(
        postgres_engine
    ).select_capture_candidates(
        ingestion_run_id=summary.ingestion_run_id,
        limit=10,
    )

    assert tuple(candidate.provider_market_ticker for candidate in candidates) == (
        "KXTEST-SECOND",
        "KXTEST-INCOMPLETE",
        "KXTEST-VALID",
    )
    assert all(
        candidate.series_identity_state == "PENDING_EVENT_ENRICHMENT" for candidate in candidates
    )
    assert all(candidate.is_provisional for candidate in candidates)
    by_ticker = {candidate.provider_market_ticker: candidate for candidate in candidates}
    assert by_ticker["KXTEST-SECOND"].has_complete_list_two_sided_quotes
    assert by_ticker["KXTEST-SECOND"].list_immediate_cross_spread == Decimal("0.01")
    assert by_ticker["KXTEST-VALID"].has_complete_list_two_sided_quotes
    assert by_ticker["KXTEST-VALID"].yes_bid_size == Decimal("12.50")
    assert not by_ticker["KXTEST-INCOMPLETE"].has_complete_list_two_sided_quotes
    assert by_ticker["KXTEST-INCOMPLETE"].list_immediate_cross_spread is None


def test_event_enrichment_persists_exact_event_series_and_market_evidence(
    postgres_engine: Engine,
) -> None:
    clock = VirtualCaptureClock()
    client = KalshiFixtureOrderbookCaptureClient(clock)
    request = BoundedOrderbookCaptureRequest(
        session_id="postgres-event-enrichment-evidence",
        latest_open_market=True,
        observations=3,
        interval_seconds=5,
        maximum_depth=5,
        maximum_bundle_quantity="10",
        timeout_seconds=10,
    )
    preparation = CaptureBoundedPublicOrderbookSession(
        client=client,
        store=InMemoryPublicOrderbookCaptureStore(),
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
        normalizer=KalshiPublicMarketNormalizer(),
    )
    initial = preparation._refresh_exact_market(
        request=request,
        market_ticker=MARKET_TICKER,
        started_at=clock.wall_clock(),
    )
    candidate = CaptureMarketCandidate(
        market_snapshot_id=initial.market.market_snapshot_id,
        ingestion_run_id=initial.market.ingestion_run_id,
        provider_market_ticker=initial.market.provider_market_ticker,
        provider_event_ticker=initial.market.provider_event_ticker,
        status="open",
        observed_at=initial.market.observed_at,
        close_time=initial.snapshot.close_time,
        is_provisional=False,
        rule_content_hash=initial.market.rule_content_hash,
        data_quality_state=initial.market.data_quality_state,
    )

    enrichment = preparation.enrich_candidates(request=request, candidates=(candidate,))

    assert enrichment.selected is not None
    store = postgres_bounded_orderbook_capture_store(postgres_engine)
    refresh = enrichment.selected
    store.create_session(preparation._new_session(request, refresh, clock.wall_clock()), refresh)
    with postgres_engine.connect() as connection:
        endpoint_identities = tuple(
            connection.scalars(
                sa.text(
                    "SELECT endpoint_identity FROM raw_provider_response "
                    "WHERE ingestion_run_id = :run_id ORDER BY request_page_ordinal"
                ),
                {"run_id": refresh.market.ingestion_run_id},
            )
        )
        fee_count = connection.scalar(sa.text("SELECT count(*) FROM series_fee_snapshot"))

    assert endpoint_identities == ("KALSHI_GET_EVENTS", "KALSHI_GET_SERIES", "KALSHI_GET_MARKET")
    assert fee_count == 1


def test_capture_persists_each_attempt_builds_history_and_never_mutates_financial_state(
    postgres_engine: Engine,
) -> None:
    financial_tables = (
        "campaign",
        "ledger_journal",
        "risk_decision",
        "risk_reservation",
        "order",
        "execution_event",
        "position",
        "settlement_fact",
    )
    with postgres_engine.connect() as connection:
        before = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
            for table in financial_tables
        }

    clock = VirtualCaptureClock()
    first = _service(postgres_engine, clock).capture(_request())
    restarted = create_postgres_engine(PostgresSettings.from_env().for_test())
    try:
        replay = _service(restarted, clock).capture(_request())
    finally:
        restarted.dispose()

    assert first.session.state is OrderbookCaptureSessionState.COMPLETED
    assert first.session.attempted_observation_count == 5
    assert first.session.successful_observation_count == 4
    assert first.session.failed_observation_count == 1
    assert first.history is not None
    assert first.history.analysis.included_observation_count == 4
    assert replay.idempotent_replay
    assert replay.session == first.session
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM orderbook_capture_session")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM orderbook_capture_observation")) == 5
        assert connection.scalar(sa.text("SELECT count(*) FROM orderbook_history_analysis")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM orderbook_history_observation")) == 4
        for table, count in before.items():
            assert connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) == count
    with (
        postgres_engine.begin() as connection,
        pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"),
    ):
        connection.execute(
            sa.text("UPDATE orderbook_capture_observation SET result_code = 'MUTATED'")
        )


def test_retained_capture_evidence_blocks_downgrade_atomically(postgres_engine: Engine) -> None:
    clock = VirtualCaptureClock()
    _service(postgres_engine, clock).capture(_request("postgres-capture-downgrade-v1"))
    with postgres_engine.connect() as connection:
        before = {
            table: connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
            for table in (
                "orderbook_capture_session",
                "orderbook_capture_observation",
                "orderbook_history_analysis",
                "orderbook_history_observation",
                "ledger_journal",
                "ledger_posting",
            )
        }
    with pytest.raises(Exception, match="cannot downgrade 0012"):
        _downgrade(postgres_engine, REVISION_0011)
    assert _revision(postgres_engine) == REVISION_0012
    assert CAPTURE_TABLES.issubset(inspect(postgres_engine).get_table_names())
    with postgres_engine.connect() as connection:
        for table, count in before.items():
            assert connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) == count


def test_stale_running_capture_reconciliation_is_idempotent(postgres_engine: Engine) -> None:
    clock = VirtualCaptureClock()
    store = postgres_bounded_orderbook_capture_store(postgres_engine)
    request = _request("postgres-capture-stale-v1")
    service = _service(postgres_engine, clock)
    refresh = service._resolve_market(request, clock.wall_clock())
    store.create_session(service._new_session(request, refresh, clock.wall_clock()), refresh)
    store.transition_running(request.session_id)
    clock.advance(100)

    first = store.reconcile_stale(now=clock.wall_clock(), grace=timedelta(seconds=1))
    second = store.reconcile_stale(now=clock.wall_clock(), grace=timedelta(seconds=1))

    assert len(first) == 1
    assert first[0].state is OrderbookCaptureSessionState.INTERRUPTED
    assert second == ()
