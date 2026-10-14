from __future__ import annotations

from datetime import timedelta
from hashlib import sha256

import pytest

from apex.adapters.kalshi_capture_fixtures import VirtualCaptureClock
from apex.adapters.kalshi_public import KalshiPublicMarketNormalizer
from apex.adapters.kalshi_sweep_fixtures import (
    KalshiFixtureOrderbookSweepClient,
    fixture_capture_candidates,
)
from apex.application.capture_public_orderbook_session import (
    CaptureBoundedPublicOrderbookSession,
    CaptureMarketCandidate,
    InMemoryPublicOrderbookCaptureStore,
)
from apex.application.discover_public_markets import (
    DiscoverPublicPredictionMarkets,
    InMemoryPublicMarketDiscoveryStore,
    PublicMarketResponseError,
)
from apex.application.sweep_public_kalshi_orderbooks import (
    PublicKalshiOrderbookSweepReport,
    PublicKalshiOrderbookSweepRequest,
    PublicKalshiOrderbookSweepState,
    PublicOrderbookShape,
    ShadowComplementResearchLabel,
    SweepPublicKalshiOrderbooks,
    _candidate_key,
)
from apex.domain.public_market import PublicMarketDataQualityState
from apex.domain.values import ContentHash


def _sweep(
    *,
    client: KalshiFixtureOrderbookSweepClient | None = None,
    **changes: object,
) -> tuple[PublicKalshiOrderbookSweepReport, KalshiFixtureOrderbookSweepClient]:
    clock = VirtualCaptureClock()
    fixture_client = client or KalshiFixtureOrderbookSweepClient(clock)
    discovery_store = InMemoryPublicMarketDiscoveryStore()
    capture = CaptureBoundedPublicOrderbookSession(
        client=fixture_client,
        store=InMemoryPublicOrderbookCaptureStore(),
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
        normalizer=KalshiPublicMarketNormalizer(),
    )
    values: dict[str, object] = {
        "sweep_id": "sweep-unit-v1",
        "market_count": 13,
        "maximum_pages": 3,
        "market_limit": 50,
        "maximum_enrichments": 20,
        "observations": 3,
        "interval_seconds": 5,
        "maximum_depth": 5,
        "maximum_bundle_quantity": "10",
        "timeout_seconds": 10,
    }
    values.update(changes)
    report = SweepPublicKalshiOrderbooks(
        discovery=DiscoverPublicPredictionMarkets(
            client=fixture_client,
            normalizer=KalshiPublicMarketNormalizer(),
            store=discovery_store,
            clock=clock.wall_clock,
        ),
        capture=capture,
        candidate_selector=lambda run_id, limit: fixture_capture_candidates(
            discovery_store, run_id, limit
        ),
    ).sweep(PublicKalshiOrderbookSweepRequest(**values))
    return report, fixture_client


def _candidate(
    ticker: str, *, open_interest: str | None, volume: str | None = None
) -> CaptureMarketCandidate:
    clock = VirtualCaptureClock()
    return CaptureMarketCandidate(
        market_snapshot_id=f"snapshot-{ticker}",
        ingestion_run_id="sweep-ranking-run",
        provider_market_ticker=ticker,
        provider_event_ticker=f"event-{ticker}",
        status="open",
        observed_at=clock.wall_clock(),
        close_time=clock.wall_clock() + timedelta(hours=2),
        is_provisional=False,
        rule_content_hash=ContentHash(sha256(ticker.encode()).hexdigest()),
        data_quality_state=PublicMarketDataQualityState.ELIGIBLE.value,
        open_interest=open_interest,
        volume=volume,
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"market_count": 2},
        {"market_count": 21},
        {"maximum_pages": 6},
        {"market_limit": 49},
        {"maximum_enrichments": 2},
        {"maximum_enrichments": 61},
        {"observations": 2},
        {"observations": 6},
        {"interval_seconds": 4},
        {"interval_seconds": 61},
        {"maximum_depth": 21},
        {"maximum_bundle_quantity": "101"},
        {"timeout_seconds": 31},
    ),
)
def test_sweep_request_rejects_unbounded_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        PublicKalshiOrderbookSweepRequest(sweep_id="invalid-sweep", **changes)


def test_candidate_ranking_uses_decimal_activity_without_turning_missing_into_zero() -> None:
    ranked = tuple(
        sorted(
            (
                _candidate("MISSING", open_interest=None),
                _candidate("ZERO", open_interest="0"),
                _candidate("HIGH", open_interest="10"),
                _candidate("TIE-A", open_interest="1", volume="1"),
                _candidate("TIE-B", open_interest="1", volume="1"),
            ),
            key=_candidate_key,
        )
    )

    assert tuple(value.provider_market_ticker for value in ranked) == (
        "HIGH",
        "TIE-A",
        "TIE-B",
        "ZERO",
        "MISSING",
    )
    assert ranked[-1].open_interest is None
    assert ranked[-2].open_interest == 0


def test_fixture_runs_normal_discovery_enrichment_capture_and_history_paths() -> None:
    report, client = _sweep()

    assert report.state is PublicKalshiOrderbookSweepState.COMPLETED_PARTIALLY
    assert report.selected_market_count == 13
    assert report.enrichment_attempt_count == 17
    assert report.failed_session_count == 1
    assert dict(report.enrichment_rejection_counts) == {
        "EVENT_DIVERSITY_CAP": 1,
        "EVENT_MARKET_MEMBERSHIP_MISSING": 1,
        "SERIES_DIVERSITY_CAP": 1,
        "SERIES_FEE_EVIDENCE_UNSUPPORTED": 1,
    }
    assert tuple(value.provider_market_ticker for value in report.markets[:2]) == (
        "SWEEP-HIGH-NON-CANDIDATE",
        "SWEEP-LOW-PERSISTING-CANDIDATE",
    )
    assert report.markets[-1].provider_market_ticker == "SWEEP-MISSING-ACTIVITY"
    assert report.markets[-1].open_interest is None
    assert report.markets[-1].volume_24h is None
    assert report.markets[-1].liquidity is None
    assert report.markets[-1].volume is None
    assert all(
        client.market_calls.count(value.provider_market_ticker) == 1 for value in report.markets
    )


def test_fixture_aggregates_book_shapes_session_failure_and_research_labels() -> None:
    report, client = _sweep()
    markets = {value.provider_market_ticker: value for value in report.markets}

    assert markets["SWEEP-EMPTY"].orderbook_shape is PublicOrderbookShape.EMPTY
    assert markets["SWEEP-ONE-SIDED"].orderbook_shape is PublicOrderbookShape.YES_ONLY
    assert markets["SWEEP-FAILED-SESSION"].orderbook_shape is None
    assert markets["SWEEP-LOW-PERSISTING-CANDIDATE"].longest_compatible_candidate_streak == 3
    assert client.orderbook_calls.index("SWEEP-FAILED-SESSION") < client.orderbook_calls.index(
        "SWEEP-TIE-A"
    )
    labels = {value.provider_market_ticker: value.label for value in report.research_ranking}
    assert labels["SWEEP-LOW-PERSISTING-CANDIDATE"] is ShadowComplementResearchLabel.CANDIDATE
    assert labels["SWEEP-FEES-ELIMINATE"] is ShadowComplementResearchLabel.FEE_ELIMINATED
    assert labels["SWEEP-ROUNDING-ELIMINATE"] is ShadowComplementResearchLabel.ROUNDING_ELIMINATED
    assert labels["SWEEP-EMPTY"] is ShadowComplementResearchLabel.INSUFFICIENT_BOOK
    assert labels["SWEEP-FAILED-SESSION"] is ShadowComplementResearchLabel.INVALID_EVIDENCE
    assert report.research_ranking[0].provider_market_ticker == "SWEEP-LOW-PERSISTING-CANDIDATE"


def test_terminal_public_boundary_stops_before_any_market() -> None:
    class TerminalClient(KalshiFixtureOrderbookSweepClient):
        def fetch_event(self, **kwargs: object):
            raise PublicMarketResponseError("fixture global schema failure")

    clock = VirtualCaptureClock()
    report, client = _sweep(client=TerminalClient(clock), market_count=3, maximum_enrichments=3)

    assert client.orderbook_calls == []
    assert report.state is PublicKalshiOrderbookSweepState.FAILED_BEFORE_ANY_MARKET
    assert report.selected_market_count == 0
    assert report.markets == ()
    assert report.fatal_error_code == "TERMINAL_ENRICHMENT_BOUNDARY"


def test_terminal_orderbook_boundary_retains_its_failed_session_and_stops_sweep() -> None:
    class TerminalOrderbookClient(KalshiFixtureOrderbookSweepClient):
        def fetch_orderbook(self, **kwargs: object):
            raise PublicMarketResponseError("fixture public orderbook schema failure")

    clock = VirtualCaptureClock()
    report, client = _sweep(
        client=TerminalOrderbookClient(clock), market_count=3, maximum_enrichments=3
    )

    assert client.orderbook_calls == []
    assert report.state is PublicKalshiOrderbookSweepState.FAILED_BEFORE_ANY_MARKET
    assert len(report.markets) == 1
    assert report.markets[0].provider_market_ticker == "SWEEP-HIGH-NON-CANDIDATE"
    assert report.markets[0].successful_observation_count == 0
    assert report.fatal_error_code == "TERMINAL_CAPTURE_BOUNDARY"
