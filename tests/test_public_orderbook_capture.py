from __future__ import annotations

from datetime import timedelta
from hashlib import sha256

import pytest

from apex.adapters.kalshi_capture_fixtures import (
    MARKET_TICKER,
    KalshiFixtureOrderbookCaptureClient,
    VirtualCaptureClock,
)
from apex.adapters.kalshi_public import (
    KALSHI_EVENTS_ENDPOINT_IDENTITY,
    KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY,
    KALSHI_SERIES_ENDPOINT_IDENTITY,
    KalshiPublicMarketNormalizer,
)
from apex.application.capture_public_orderbook_session import (
    BoundedOrderbookCaptureRequest,
    CaptureBoundedPublicOrderbookSession,
    CaptureMarketCandidate,
    InMemoryPublicOrderbookCaptureStore,
    OrderbookCaptureObservationState,
    OrderbookCaptureSessionState,
    PublicOrderbookCaptureError,
    PublicOrderbookCaptureTransientError,
)
from apex.domain.public_complement import ComplementState
from apex.domain.public_market import PublicMarketDataQualityState
from apex.domain.values import ContentHash


def _request(**changes: object) -> BoundedOrderbookCaptureRequest:
    values: dict[str, object] = {
        "session_id": "capture-unit-v1",
        "market_ticker": MARKET_TICKER,
        "observations": 5,
        "interval_seconds": 5,
        "maximum_depth": 5,
        "maximum_bundle_quantity": "10",
        "timeout_seconds": 10,
    }
    values.update(changes)
    return BoundedOrderbookCaptureRequest(**values)


def _service(
    store: InMemoryPublicOrderbookCaptureStore,
    clock: VirtualCaptureClock,
    client: KalshiFixtureOrderbookCaptureClient | None = None,
    discover_first=None,
) -> CaptureBoundedPublicOrderbookSession:
    return CaptureBoundedPublicOrderbookSession(
        client=client or KalshiFixtureOrderbookCaptureClient(clock),
        store=store,
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
        normalizer=KalshiPublicMarketNormalizer(),
        discover_first=discover_first,
    )


def _candidate_from_refresh(refresh) -> CaptureMarketCandidate:
    return CaptureMarketCandidate(
        market_snapshot_id=refresh.market.market_snapshot_id,
        ingestion_run_id=refresh.market.ingestion_run_id,
        provider_market_ticker=refresh.market.provider_market_ticker,
        provider_event_ticker=refresh.market.provider_event_ticker,
        status="open",
        observed_at=refresh.market.observed_at,
        close_time=refresh.snapshot.close_time,
        is_provisional=False,
        rule_content_hash=refresh.market.rule_content_hash,
        data_quality_state=refresh.market.data_quality_state,
    )


def _candidate(
    *,
    market_ticker: str,
    event_ticker: str,
    rule: str = "Candidate rule",
    is_provisional: bool = False,
    close_offset_hours: int = 4,
) -> CaptureMarketCandidate:
    return CaptureMarketCandidate(
        market_snapshot_id=f"snapshot-{market_ticker}-{event_ticker}",
        ingestion_run_id="retained-list-discovery",
        provider_market_ticker=market_ticker,
        provider_event_ticker=event_ticker,
        status="open",
        observed_at=VirtualCaptureClock().wall_clock(),
        close_time=VirtualCaptureClock().wall_clock() + timedelta(hours=close_offset_hours),
        is_provisional=is_provisional,
        rule_content_hash=ContentHash(sha256(rule.encode("utf-8")).hexdigest()),
        data_quality_state=PublicMarketDataQualityState.ELIGIBLE.value,
    )


class _EventLineageClient(KalshiFixtureOrderbookCaptureClient):
    """Mocked exact endpoints used to prove bounded event enrichment only."""

    def __init__(
        self,
        clock: VirtualCaptureClock,
        *,
        events: dict[str, dict[str, object]],
        market_event_ticker: str,
        market_rule: str = "Candidate rule",
    ) -> None:
        super().__init__(clock)
        self.events = events
        self.market_event_ticker = market_event_ticker
        self.market_rule = market_rule
        self.event_calls: list[str] = []
        self.series_calls: list[str] = []
        self.market_calls: list[str] = []

    def fetch_event(self, *, event_ticker: str, timeout_seconds: int):
        assert timeout_seconds == 10
        self.event_calls.append(event_ticker)
        self._clock.advance(0.25)  # type: ignore[attr-defined]
        return self._response(  # type: ignore[attr-defined]
            endpoint_identity=KALSHI_EVENTS_ENDPOINT_IDENTITY,
            document={"event": self.events[event_ticker]},
        )

    def fetch_series(self, *, series_ticker: str, timeout_seconds: int):
        assert timeout_seconds == 10
        self.series_calls.append(series_ticker)
        self._clock.advance(0.25)  # type: ignore[attr-defined]
        return self._response(  # type: ignore[attr-defined]
            endpoint_identity=KALSHI_SERIES_ENDPOINT_IDENTITY,
            document={
                "series": {
                    "ticker": series_ticker,
                    "fee_type": "quadratic",
                    "fee_multiplier": "1",
                    "last_updated_ts": "2026-07-20T14:00:00Z",
                }
            },
        )

    def fetch_market(self, *, market_ticker: str, timeout_seconds: int):
        assert timeout_seconds == 10
        self.market_calls.append(market_ticker)
        self._clock.advance(0.25)  # type: ignore[attr-defined]
        return self._response(  # type: ignore[attr-defined]
            endpoint_identity="KALSHI_GET_MARKET",
            document={
                "market": {
                    "ticker": market_ticker,
                    "event_ticker": self.market_event_ticker,
                    "status": "active",
                    "open_time": "2026-07-20T13:00:00Z",
                    "close_time": "2026-07-20T18:00:00Z",
                    "rules_primary": self.market_rule,
                }
            },
        )


def _event(
    *, event_ticker: str, series_ticker: str | None, market_ticker: str | None
) -> dict[str, object]:
    event: dict[str, object] = {"event_ticker": event_ticker}
    if series_ticker is not None:
        event["series_ticker"] = series_ticker
    event["markets"] = (
        [
            {
                "ticker": market_ticker,
                "event_ticker": event_ticker,
                "rules_primary": "Candidate rule",
            }
        ]
        if market_ticker is not None
        else []
    )
    return event


def test_event_enrichment_uses_authoritative_series_identity_without_ticker_parsing() -> None:
    clock = VirtualCaptureClock()
    candidate = _candidate(
        market_ticker="MARKET-TEXT-CANNOT-DERIVE-SERIES",
        event_ticker="EXACT-EVENT",
    )
    client = _EventLineageClient(
        clock,
        events={
            "EXACT-EVENT": _event(
                event_ticker="EXACT-EVENT",
                series_ticker="AUTHORITATIVE-SERIES-ONLY",
                market_ticker=candidate.provider_market_ticker,
            )
        },
        market_event_ticker="EXACT-EVENT",
    )

    report = _service(InMemoryPublicOrderbookCaptureStore(), clock, client).enrich_candidates(
        request=_request(
            session_id="event-series-authority", market_ticker=None, latest_open_market=True
        ),
        candidates=(candidate,),
    )

    assert report.selected is not None
    assert report.selected.market.series_ticker == "AUTHORITATIVE-SERIES-ONLY"
    assert client.event_calls == ["EXACT-EVENT"]
    assert client.series_calls == ["AUTHORITATIVE-SERIES-ONLY"]
    assert client.market_calls == ["MARKET-TEXT-CANNOT-DERIVE-SERIES"]
    assert tuple(item.response.endpoint_identity for item in report.selected.raw_responses) == (
        KALSHI_EVENTS_ENDPOINT_IDENTITY,
        KALSHI_SERIES_ENDPOINT_IDENTITY,
        "KALSHI_GET_MARKET",
    )


def test_generic_shared_orderbook_capture_does_not_start_complement_evaluation() -> None:
    clock = VirtualCaptureClock()
    candidate = _candidate(market_ticker=MARKET_TICKER, event_ticker="EXACT-EVENT")
    client = _EventLineageClient(
        clock,
        events={
            "EXACT-EVENT": _event(
                event_ticker="EXACT-EVENT",
                series_ticker="SERIES",
                market_ticker=MARKET_TICKER,
            )
        },
        market_event_ticker="EXACT-EVENT",
    )
    store = InMemoryPublicOrderbookCaptureStore()
    service = _service(store, clock, client)

    enrichment = service.enrich_candidate(
        request=_request(
            session_id="generic-shared-book", market_ticker=None, latest_open_market=True
        ),
        candidate=candidate,
    )

    assert enrichment.selected is not None
    evidence = service.capture_shared_orderbook(
        refresh=enrichment.selected,
        maximum_depth=5,
        timeout_seconds=10,
        request_ordinal=10001,
    )

    assert evidence.orderbook.provider_market_ticker == MARKET_TICKER
    assert evidence.orderbook.yes_bid_level_count >= 0
    assert evidence.raw.response.endpoint_identity == KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY
    # Generic shared capture neither creates an orderbook-capture session nor
    # calculates a binary-complement evaluation.
    assert store._reports == {}


@pytest.mark.parametrize(
    ("event", "expected_code"),
    (
        (
            {"event_ticker": "WRONG-EVENT", "series_ticker": "SERIES", "markets": []},
            "EVENT_TICKER_MISMATCH",
        ),
        (
            _event(event_ticker="EXACT-EVENT", series_ticker=None, market_ticker=MARKET_TICKER),
            "EVENT_SERIES_IDENTITY_MISSING",
        ),
        (
            _event(event_ticker="EXACT-EVENT", series_ticker="SERIES", market_ticker=None),
            "EVENT_MARKET_MEMBERSHIP_MISSING",
        ),
    ),
)
def test_event_enrichment_rejects_invalid_exact_event_evidence(
    event: dict[str, object], expected_code: str
) -> None:
    clock = VirtualCaptureClock()
    candidate = _candidate(market_ticker=MARKET_TICKER, event_ticker="EXACT-EVENT")
    client = _EventLineageClient(
        clock,
        events={"EXACT-EVENT": event},
        market_event_ticker="EXACT-EVENT",
    )

    report = _service(InMemoryPublicOrderbookCaptureStore(), clock, client).enrich_candidates(
        request=_request(
            session_id=f"invalid-event-{expected_code}", market_ticker=None, latest_open_market=True
        ),
        candidates=(candidate,),
    )

    assert report.selected is None
    assert report.rejection_counts == ((expected_code, 1),)
    assert client.series_calls == []
    assert client.market_calls == []


@pytest.mark.parametrize(
    ("market_event_ticker", "market_rule", "expected_code"),
    (
        ("OTHER-EVENT", "Candidate rule", "MARKET_REFRESH_LINEAGE_MISMATCH"),
        ("EXACT-EVENT", "Changed primary rule", "RULE_EVIDENCE_CHANGED"),
    ),
)
def test_event_enrichment_rejects_changed_exact_market_lineage(
    market_event_ticker: str, market_rule: str, expected_code: str
) -> None:
    clock = VirtualCaptureClock()
    candidate = _candidate(market_ticker=MARKET_TICKER, event_ticker="EXACT-EVENT")
    client = _EventLineageClient(
        clock,
        events={
            "EXACT-EVENT": _event(
                event_ticker="EXACT-EVENT",
                series_ticker="SERIES",
                market_ticker=MARKET_TICKER,
            )
        },
        market_event_ticker=market_event_ticker,
        market_rule=market_rule,
    )

    report = _service(InMemoryPublicOrderbookCaptureStore(), clock, client).enrich_candidates(
        request=_request(
            session_id=f"changed-market-{expected_code}",
            market_ticker=None,
            latest_open_market=True,
        ),
        candidates=(candidate,),
    )

    assert report.selected is None
    assert report.rejection_counts == ((expected_code, 1),)
    assert client.series_calls == ["SERIES"]
    assert client.market_calls == [MARKET_TICKER]


def test_event_enrichment_continues_after_one_failure_and_stops_after_first_valid_candidate() -> (
    None
):
    clock = VirtualCaptureClock()
    rejected = _candidate(market_ticker=MARKET_TICKER, event_ticker="EVENT-MISSING")
    selected = _candidate(market_ticker=MARKET_TICKER, event_ticker="EVENT-VALID")
    never_attempted = _candidate(market_ticker=MARKET_TICKER, event_ticker="EVENT-NEVER")
    client = _EventLineageClient(
        clock,
        events={
            "EVENT-MISSING": _event(
                event_ticker="EVENT-MISSING", series_ticker="SERIES-MISSING", market_ticker=None
            ),
            "EVENT-VALID": _event(
                event_ticker="EVENT-VALID",
                series_ticker="SERIES-VALID",
                market_ticker=MARKET_TICKER,
            ),
            "EVENT-NEVER": _event(
                event_ticker="EVENT-NEVER",
                series_ticker="SERIES-NEVER",
                market_ticker=MARKET_TICKER,
            ),
        },
        market_event_ticker="EVENT-VALID",
    )

    report = _service(InMemoryPublicOrderbookCaptureStore(), clock, client).enrich_candidates(
        request=_request(
            session_id="continue-after-failure", market_ticker=None, latest_open_market=True
        ),
        candidates=(rejected, selected, never_attempted),
    )

    assert report.selected is not None
    assert report.selected.market.provider_event_ticker == "EVENT-VALID"
    assert report.event_enrichments_attempted == 2
    assert report.rejection_counts == (("EVENT_MARKET_MEMBERSHIP_MISSING", 1),)
    assert client.event_calls == ["EVENT-MISSING", "EVENT-VALID"]
    assert client.series_calls == ["SERIES-VALID"]


def test_event_enrichment_attempts_at_most_ten_candidates() -> None:
    clock = VirtualCaptureClock()
    candidates = tuple(
        _candidate(market_ticker=MARKET_TICKER, event_ticker=f"EVENT-{index:02d}")
        for index in range(12)
    )
    client = _EventLineageClient(
        clock,
        events={
            candidate.provider_event_ticker: _event(
                event_ticker=candidate.provider_event_ticker,
                series_ticker="SERIES",
                market_ticker=None,
            )
            for candidate in candidates
        },
        market_event_ticker="unreachable",
    )

    report = _service(InMemoryPublicOrderbookCaptureStore(), clock, client).enrich_candidates(
        request=_request(
            session_id="maximum-ten-events", market_ticker=None, latest_open_market=True
        ),
        candidates=candidates,
    )

    assert report.selected is None
    assert report.candidates_considered == 10
    assert report.event_enrichments_attempted == 10
    assert len(client.event_calls) == 10
    assert client.series_calls == []


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"market_ticker": None, "latest_open_market": False}, "choose exactly"),
        ({"market_ticker": MARKET_TICKER, "latest_open_market": True}, "choose exactly"),
        ({"observations": 2}, "between three"),
        ({"observations": 21}, "between three"),
        ({"interval_seconds": 4}, "between five"),
        ({"maximum_depth": 21}, "between one"),
        ({"maximum_bundle_quantity": "101"}, "not exceed"),
        ({"timeout_seconds": 31}, "between one"),
    ),
)
def test_capture_configuration_rejects_unbounded_or_unsafe_values(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _request(**changes)


def test_fixture_capture_is_bounded_idempotent_and_builds_history() -> None:
    clock = VirtualCaptureClock()
    store = InMemoryPublicOrderbookCaptureStore()
    first = _service(store, clock).capture(_request())
    replay = _service(store, clock).capture(_request())

    assert first.session.state is OrderbookCaptureSessionState.COMPLETED
    assert first.session.attempted_observation_count == 5
    assert first.session.successful_observation_count == 4
    assert first.session.failed_observation_count == 1
    assert first.history is not None
    assert first.history.analysis.included_observation_count == 4
    assert first.observations[3].state is OrderbookCaptureObservationState.PROVIDER_FAILED
    assert first.observations[0].state is OrderbookCaptureObservationState.SUCCEEDED
    assert first.observations[0].result_code == ComplementState.NO_CANDIDATE.value
    assert replay.idempotent_replay
    assert replay.session == first.session


@pytest.mark.parametrize(
    ("book", "expected_yes_levels", "expected_no_levels"),
    (
        ({"yes_dollars": [], "no_dollars": []}, 0, 0),
        ({"yes_dollars": [["0.50", "10.00"]], "no_dollars": []}, 1, 0),
    ),
)
def test_empty_and_one_sided_books_are_successful_evidence_not_selection_failures(
    book: dict[str, object], expected_yes_levels: int, expected_no_levels: int
) -> None:
    class SparseBookClient(KalshiFixtureOrderbookCaptureClient):
        def fetch_orderbook(self, **kwargs: object):
            self._orderbook_calls += 1  # type: ignore[attr-defined]
            self._clock.advance(1)  # type: ignore[attr-defined]
            return self._response(  # type: ignore[attr-defined]
                endpoint_identity=KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY,
                document={"orderbook_fp": book},
            )

    clock = VirtualCaptureClock()
    store = InMemoryPublicOrderbookCaptureStore()
    session_id = f"sparse-book-{expected_yes_levels}-{expected_no_levels}"
    report = _service(store, clock, SparseBookClient(clock)).capture(
        _request(session_id=session_id, observations=3)
    )

    assert report.session.state is OrderbookCaptureSessionState.COMPLETED
    assert report.session.successful_observation_count == 3
    assert report.history is not None
    assert all(
        item.state is OrderbookCaptureObservationState.SUCCEEDED for item in report.observations
    )
    assert all(item.result_code == ComplementState.INELIGIBLE.value for item in report.observations)
    assert all(
        evidence.orderbook.yes_bid_level_count == expected_yes_levels
        and evidence.orderbook.no_bid_level_count == expected_no_levels
        for evidence in store.session_evidence(session_id)
    )


def test_latest_selection_accepts_active_refresh_without_optional_market_quotes() -> None:
    class ActiveSparseMarketClient(KalshiFixtureOrderbookCaptureClient):
        def fetch_market(self, **kwargs: object):
            response = super().fetch_market(**kwargs)
            market = dict(response.document["market"])
            market["status"] = "active"
            market.pop("liquidity_dollars", None)
            for field in (
                "yes_bid_dollars",
                "yes_ask_dollars",
                "no_bid_dollars",
                "no_ask_dollars",
            ):
                market.pop(field)
            return self._response(  # type: ignore[attr-defined]
                endpoint_identity=response.endpoint_identity,
                document={"market": market},
            )

    clock = VirtualCaptureClock()
    client = ActiveSparseMarketClient(clock)
    preparation = _service(
        InMemoryPublicOrderbookCaptureStore(), clock, client
    )._refresh_exact_market(
        request=_request(session_id="active-sparse-preparation"),
        market_ticker=MARKET_TICKER,
        started_at=clock.wall_clock(),
    )
    report = _service(
        InMemoryPublicOrderbookCaptureStore(candidates=(_candidate_from_refresh(preparation),)),
        clock,
        client,
    ).capture(
        _request(
            session_id="active-sparse-selection",
            market_ticker=None,
            latest_open_market=True,
            observations=3,
        )
    )

    assert report.session.state is OrderbookCaptureSessionState.COMPLETED
    assert report.session.successful_observation_count == 3


def test_discover_first_selects_once_then_refreshes_the_exact_selected_market() -> None:
    clock = VirtualCaptureClock()
    client = KalshiFixtureOrderbookCaptureClient(clock)
    selected = _candidate_from_refresh(
        _service(InMemoryPublicOrderbookCaptureStore(), clock, client)._refresh_exact_market(
            request=_request(session_id="discover-first-preparation"),
            market_ticker=MARKET_TICKER,
            started_at=clock.wall_clock(),
        )
    )
    calls: list[tuple[str, int]] = []

    def discover_first(session_id: str, timeout_seconds: int):
        calls.append((session_id, timeout_seconds))
        return (selected,)

    report = _service(InMemoryPublicOrderbookCaptureStore(), clock, client, discover_first).capture(
        _request(
            session_id="discover-first-selection",
            market_ticker=None,
            latest_open_market=True,
            discover_first=True,
            observations=3,
        )
    )

    assert calls == [("discover-first-selection", 10)]
    assert report.session.provider_market_ticker == MARKET_TICKER
    assert report.session.state is OrderbookCaptureSessionState.COMPLETED


def test_refreshed_rule_lineage_conflict_stops_before_creating_a_session() -> None:
    class RuleChangedClient(KalshiFixtureOrderbookCaptureClient):
        def fetch_market(self, **kwargs: object):
            response = super().fetch_market(**kwargs)
            market = dict(response.document["market"])
            market["rules_primary"] = "Conflicting exact rule"
            return self._response(  # type: ignore[attr-defined]
                endpoint_identity=response.endpoint_identity,
                document={"market": market},
            )

    clock = VirtualCaptureClock()
    selected = _candidate_from_refresh(
        _service(InMemoryPublicOrderbookCaptureStore(), clock)._refresh_exact_market(
            request=_request(session_id="lineage-preparation"),
            market_ticker=MARKET_TICKER,
            started_at=clock.wall_clock(),
        )
    )
    store = InMemoryPublicOrderbookCaptureStore(candidates=(selected,))

    with pytest.raises(PublicOrderbookCaptureError, match="survived exact event enrichment"):
        _service(store, clock, RuleChangedClient(clock)).capture(
            _request(
                session_id="lineage-conflict",
                market_ticker=None,
                latest_open_market=True,
                observations=3,
            )
        )

    assert store.get_session("lineage-conflict") is None


def test_interruption_preserves_prior_observations_without_resume() -> None:
    class InterruptedClient(KalshiFixtureOrderbookCaptureClient):
        def fetch_orderbook(self, **kwargs: object):
            if self._orderbook_calls == 1:  # type: ignore[attr-defined]
                raise KeyboardInterrupt
            return super().fetch_orderbook(**kwargs)

    clock = VirtualCaptureClock()
    store = InMemoryPublicOrderbookCaptureStore()
    report = _service(store, clock, InterruptedClient(clock)).capture(_request())

    assert report.session.state is OrderbookCaptureSessionState.INTERRUPTED
    assert report.session.successful_observation_count == 1
    assert report.history is None
    assert _service(store, clock).capture(_request()).idempotent_replay


def test_maximum_transient_failure_count_terminates_after_prior_evidence() -> None:
    class TwoTransientFailuresClient(KalshiFixtureOrderbookCaptureClient):
        def fetch_orderbook(self, **kwargs: object):
            if self._orderbook_calls in {1, 2}:  # type: ignore[attr-defined]
                self._orderbook_calls += 1  # type: ignore[attr-defined]
                self._clock.advance(0.25)  # type: ignore[attr-defined]
                raise PublicOrderbookCaptureTransientError("synthetic transient provider failure")
            return super().fetch_orderbook(**kwargs)

    clock = VirtualCaptureClock()
    store = InMemoryPublicOrderbookCaptureStore()
    report = _service(store, clock, TwoTransientFailuresClient(clock)).capture(_request())

    assert report.session.state is OrderbookCaptureSessionState.FAILED
    assert report.session.successful_observation_count == 1
    assert report.session.failed_observation_count == 2
    assert report.session.last_error_code == "MAXIMUM_PROVIDER_FAILURES_EXCEEDED"
    assert len(report.observations) == 3
    assert report.history is None


def test_terminal_provider_failure_stops_without_erasing_prior_evidence() -> None:
    class TerminalFailureClient(KalshiFixtureOrderbookCaptureClient):
        def fetch_orderbook(self, **kwargs: object):
            if self._orderbook_calls == 1:  # type: ignore[attr-defined]
                self._orderbook_calls += 1  # type: ignore[attr-defined]
                self._clock.advance(0.25)  # type: ignore[attr-defined]
                raise ValueError("synthetic terminal provider failure")
            return super().fetch_orderbook(**kwargs)

    clock = VirtualCaptureClock()
    store = InMemoryPublicOrderbookCaptureStore()
    report = _service(store, clock, TerminalFailureClient(clock)).capture(_request())

    assert report.session.state is OrderbookCaptureSessionState.FAILED
    assert report.session.successful_observation_count == 1
    assert report.session.failed_observation_count == 1
    assert report.session.last_error_code == "PROVIDER_TERMINAL_FAILURE"
    assert len(report.observations) == 2
    assert report.history is None


def test_late_request_records_a_skipped_deadline_without_overlap() -> None:
    class LateFirstRequestClient(KalshiFixtureOrderbookCaptureClient):
        def fetch_orderbook(self, **kwargs: object):
            response = super().fetch_orderbook(**kwargs)
            if self._orderbook_calls == 1:  # type: ignore[attr-defined]
                self._clock.advance(9)  # type: ignore[attr-defined]
            return response

    clock = VirtualCaptureClock()
    report = _service(
        InMemoryPublicOrderbookCaptureStore(), clock, LateFirstRequestClient(clock)
    ).capture(_request(observations=4))

    assert report.session.state is OrderbookCaptureSessionState.COMPLETED
    assert report.observations[1].state is OrderbookCaptureObservationState.SKIPPED_DEADLINE
    assert report.session.successful_observation_count == 3
    assert report.session.failed_observation_count == 1


def test_stale_running_reconciliation_is_idempotent() -> None:
    clock = VirtualCaptureClock()
    store = InMemoryPublicOrderbookCaptureStore()
    request = _request()
    refresh_service = _service(store, clock)
    refresh = refresh_service._resolve_market(request, clock.wall_clock())
    store.create_session(
        refresh_service._new_session(request, refresh, clock.wall_clock()), refresh
    )
    store.transition_running(request.session_id)
    clock.advance(100)

    first = store.reconcile_stale(now=clock.wall_clock(), grace=request.maximum_duration / 100)
    second = store.reconcile_stale(now=clock.wall_clock(), grace=request.maximum_duration / 100)

    assert len(first) == 1
    assert first[0].state is OrderbookCaptureSessionState.INTERRUPTED
    assert second == ()
