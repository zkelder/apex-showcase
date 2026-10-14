from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

import pytest

from apex.adapters.kalshi_complement_fixtures import (
    FIXTURE_AT,
    MARKET_TICKER,
    KalshiFixtureBinaryComplementClient,
    fixture_market,
)
from apex.adapters.kalshi_public import (
    KALSHI_EVENTS_ENDPOINT_IDENTITY,
    KALSHI_MARKET_ENDPOINT_IDENTITY,
    KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY,
    KALSHI_PUBLIC_HOST,
    KalshiHttpResponse,
    KalshiPublicOrderbookDataClient,
)
from apex.application.scan_public_binary_complements import (
    BinaryComplementScanRequest,
    ComplementMarket,
    InMemoryPublicBinaryComplementStore,
    ScanPublicBinaryComplements,
    _activity_rank_key,
)
from apex.domain.public_complement import (
    ComplementReasonCode,
    ComplementState,
    _fill_fee,
)
from apex.domain.values import ContentHash


def _scan(
    scenario: str,
    *,
    depth: int = 5,
    maximum_bundle_quantity: str = "10",
):
    market = fixture_market(scenario=scenario)
    return ScanPublicBinaryComplements(
        client=KalshiFixtureBinaryComplementClient(scenario=scenario),
        store=InMemoryPublicBinaryComplementStore((market,)),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    ).scan(
        BinaryComplementScanRequest(
            market_discovery_run_id=market.ingestion_run_id,
            market_limit=1,
            maximum_depth=depth,
            maximum_bundle_quantity=Decimal(maximum_bundle_quantity),
        )
    )


def test_positive_fee_aware_complement_derives_exact_opposite_side_asks() -> None:
    report = _scan("positive")
    evaluation = report.evaluations[0]

    assert evaluation.state is ComplementState.CANDIDATE
    assert evaluation.matched_quantity == Decimal("10")
    assert evaluation.weighted_yes_ask == Decimal("0.47")
    assert evaluation.weighted_no_ask == Decimal("0.47")
    assert evaluation.gross_complement_gap == Decimal("0.60")
    assert evaluation.yes_modeled_taker_fee == Decimal("0.1744")
    assert evaluation.no_modeled_taker_fee == Decimal("0.1744")
    assert evaluation.modeled_taker_fees == Decimal("0.3488")
    assert evaluation.conservative_rounding_reserve == Decimal("0.0112")
    assert evaluation.total_modeled_acquisition_cost == Decimal("9.7600")
    assert evaluation.conservative_net_lower_bound == Decimal("0.2400")
    assert evaluation.net_lower_bound_per_contract == Decimal("0.0240")
    assert report.ranked_priorities == (evaluation,)


@pytest.mark.parametrize(
    ("scenario", "state", "reason"),
    (
        ("empty", ComplementState.INELIGIBLE, ComplementReasonCode.ORDERBOOK_MISSING_SIDE),
        ("one_sided", ComplementState.INELIGIBLE, ComplementReasonCode.ORDERBOOK_MISSING_SIDE),
        (
            "non_candidate",
            ComplementState.NO_CANDIDATE,
            ComplementReasonCode.GROSS_GAP_NOT_POSITIVE,
        ),
        (
            "fees_eliminate",
            ComplementState.NO_CANDIDATE,
            ComplementReasonCode.FEES_ELIMINATE_GROSS_GAP,
        ),
        (
            "rounding_eliminate",
            ComplementState.NO_CANDIDATE,
            ComplementReasonCode.ROUNDING_RESERVE_ELIMINATES_GAP,
        ),
        ("unsupported_fee", ComplementState.INELIGIBLE, ComplementReasonCode.FEE_MODEL_UNSUPPORTED),
        ("stale_fee", ComplementState.INELIGIBLE, ComplementReasonCode.FEE_EVIDENCE_STALE),
        ("rule_mismatch", ComplementState.INELIGIBLE, ComplementReasonCode.RULE_EVIDENCE_MISSING),
        ("malformed_price", ComplementState.INVALID, ComplementReasonCode.ORDERBOOK_LEVEL_INVALID),
        (
            "negative_quantity",
            ComplementState.INVALID,
            ComplementReasonCode.ORDERBOOK_LEVEL_INVALID,
        ),
    ),
)
def test_complement_states_fail_closed_for_incomplete_or_unsupported_evidence(
    scenario: str, state: ComplementState, reason: ComplementReasonCode
) -> None:
    evaluation = _scan(scenario).evaluations[0]
    assert evaluation.state is state
    assert reason in evaluation.reason_codes


def test_depth_and_quantity_limits_bound_only_visible_exact_matching() -> None:
    quantity_limited = _scan("quantity_limited").evaluations[0]
    depth_limited = _scan("depth_limited", depth=1, maximum_bundle_quantity="20").evaluations[0]
    multiple_depth = _scan("multiple_depth").evaluations[0]

    assert quantity_limited.matched_quantity == Decimal("2")
    assert depth_limited.matched_quantity == Decimal("10")
    assert multiple_depth.matched_quantity == Decimal("6")


def test_zero_fee_multiplier_is_distinct_from_missing_fee_evidence() -> None:
    evaluation = _scan("zero_multiplier").evaluations[0]

    assert evaluation.state is ComplementState.CANDIDATE
    assert evaluation.modeled_taker_fees == Decimal(0)


def test_fee_change_fallback_ignores_a_future_scheduled_rate() -> None:
    evaluation = _scan("fee_changes_fallback").evaluations[0]

    assert evaluation.state is ComplementState.CANDIDATE
    assert evaluation.modeled_taker_fees == Decimal("0.3488")


def test_subpenny_fractional_fill_rounds_the_trade_fee_before_balance_alignment() -> None:
    # Each derived ask is 0.3301 for 0.03 contracts.  The documented quadratic
    # fee is first ceiled to a centicent (0.0005), then the buy balance is
    # ceiled to a cent; no public-book-only accumulator rebate is assumed.
    trade_fee, rounding_reserve = _fill_fee(
        quantity=Decimal("0.03"), ask_price=Decimal("0.3301"), multiplier=Decimal("1")
    )

    assert trade_fee == Decimal("0.0005")
    assert rounding_reserve == Decimal("0.009597")

    evaluation = _scan("subpenny_fractional").evaluations[0]
    assert evaluation.state is ComplementState.NO_CANDIDATE
    assert evaluation.matched_quantity == Decimal(0)
    assert ComplementReasonCode.ROUNDING_RESERVE_ELIMINATES_GAP in evaluation.reason_codes


def test_identical_evidence_replays_without_recollection() -> None:
    market = fixture_market()
    store = InMemoryPublicBinaryComplementStore((market,))
    request = BinaryComplementScanRequest(
        market_discovery_run_id=market.ingestion_run_id,
        market_limit=1,
        maximum_depth=5,
        maximum_bundle_quantity=Decimal("10"),
    )
    service = ScanPublicBinaryComplements(
        client=KalshiFixtureBinaryComplementClient(),
        store=store,
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )

    first = service.scan(request)
    replay = service.scan(request)

    assert not first.idempotent_replay
    assert replay.idempotent_replay
    assert replay.evaluations == first.evaluations


def test_missing_list_series_uses_one_exact_event_read_then_replays_without_requests() -> None:
    market = replace(fixture_market(), series_ticker=None)
    store = InMemoryPublicBinaryComplementStore((market,))
    request = BinaryComplementScanRequest(
        market_discovery_run_id=market.ingestion_run_id,
        market_limit=1,
        maximum_depth=5,
        maximum_bundle_quantity=Decimal("10"),
    )

    class CountingClient(KalshiFixtureBinaryComplementClient):
        def __init__(self) -> None:
            super().__init__()
            self.requests = 0

        def fetch_market(self, *, market_ticker: str, timeout_seconds: int):
            self.requests += 1
            return super().fetch_market(
                market_ticker=market_ticker, timeout_seconds=timeout_seconds
            )

        def fetch_event(self, *, event_ticker: str, timeout_seconds: int):
            self.requests += 1
            return super().fetch_event(event_ticker=event_ticker, timeout_seconds=timeout_seconds)

        def fetch_orderbook(self, *, market_ticker: str, maximum_depth: int, timeout_seconds: int):
            self.requests += 1
            return super().fetch_orderbook(
                market_ticker=market_ticker,
                maximum_depth=maximum_depth,
                timeout_seconds=timeout_seconds,
            )

        def fetch_series(self, *, series_ticker: str, timeout_seconds: int):
            self.requests += 1
            return super().fetch_series(
                series_ticker=series_ticker, timeout_seconds=timeout_seconds
            )

    client = CountingClient()
    service = ScanPublicBinaryComplements(
        client=client,
        store=store,
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )

    first = service.scan(request)
    requests_after_first = client.requests
    replay = service.scan(request)

    assert first.evaluations[0].state is ComplementState.CANDIDATE
    assert requests_after_first == 3
    assert replay.idempotent_replay
    assert client.requests == requests_after_first


class _RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> KalshiHttpResponse:
        self.calls.append(kwargs)
        return KalshiHttpResponse(
            status=200,
            content_type="application/json",
            body=b'{"orderbook_fp":{"yes_dollars":[],"no_dollars":[]}}',
        )


def test_public_orderbook_client_is_fixed_get_only_and_sends_no_authentication_headers() -> None:
    transport = _RecordingTransport()
    client = KalshiPublicOrderbookDataClient(
        transport=transport,
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )

    response = client.fetch_orderbook(
        market_ticker=MARKET_TICKER, maximum_depth=5, timeout_seconds=10
    )

    assert response.endpoint_identity == KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY
    call = transport.calls[0]
    assert call["host"] == KALSHI_PUBLIC_HOST
    assert call["path"] == "/trade-api/v2/markets/COMPLEMENT-MARKET/orderbook"
    headers = {str(key).lower() for key in dict(call["headers"])}
    assert not {"authorization", "cookie", "x-kalshi-access-key"}.intersection(headers)


def test_public_single_market_refresh_is_allowlisted_get_only_and_sends_no_authentication() -> None:
    transport = _RecordingTransport()
    transport_response = KalshiHttpResponse(
        status=200,
        content_type="application/json",
        body=(b'{"market":{"ticker":"COMPLEMENT-MARKET","series_ticker":"SERIES"}}'),
    )
    transport.get = lambda **kwargs: (  # type: ignore[method-assign]
        transport.calls.append(kwargs) or transport_response
    )
    client = KalshiPublicOrderbookDataClient(
        transport=transport,
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )

    response = client.fetch_market(market_ticker=MARKET_TICKER, timeout_seconds=10)

    assert response.endpoint_identity == KALSHI_MARKET_ENDPOINT_IDENTITY
    call = transport.calls[0]
    assert call["host"] == KALSHI_PUBLIC_HOST
    assert call["path"] == "/trade-api/v2/markets/COMPLEMENT-MARKET"
    assert {str(key).lower() for key in dict(call["headers"])} == {"accept", "user-agent"}


def test_public_exact_event_enrichment_is_allowlisted_get_only_and_sends_no_authentication() -> (
    None
):
    transport = _RecordingTransport()
    transport_response = KalshiHttpResponse(
        status=200,
        content_type="application/json",
        body=(
            b'{"event":{"event_ticker":"COMPLEMENT-EVENT","series_ticker":"SERIES",'
            b'"markets":[{"ticker":"COMPLEMENT-MARKET",'
            b'"event_ticker":"COMPLEMENT-EVENT"}]}}'
        ),
    )
    transport.get = lambda **kwargs: (  # type: ignore[method-assign]
        transport.calls.append(kwargs) or transport_response
    )
    client = KalshiPublicOrderbookDataClient(
        transport=transport,
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )

    response = client.fetch_event(event_ticker="COMPLEMENT-EVENT", timeout_seconds=10)

    assert response.endpoint_identity == KALSHI_EVENTS_ENDPOINT_IDENTITY
    call = transport.calls[0]
    assert call["host"] == KALSHI_PUBLIC_HOST
    assert call["path"] == "/trade-api/v2/events/COMPLEMENT-EVENT?with_nested_markets=true"
    assert {str(key).lower() for key in dict(call["headers"])} == {"accept", "user-agent"}


def _activity_market(
    ticker: str,
    *,
    open_interest: str | None = None,
    volume_24h: str | None = None,
    liquidity: str | None = None,
    volume: str | None = None,
    close_time: datetime | None = None,
    ingestion_run_id: str = "activity-run",
) -> ComplementMarket:
    return ComplementMarket(
        market_snapshot_id=f"snapshot-{ticker}",
        ingestion_run_id=ingestion_run_id,
        provider_market_ticker=ticker,
        provider_event_ticker="ACTIVITY-EVENT",
        series_ticker="ACTIVITY-SERIES",
        status="open",
        observed_at=FIXTURE_AT,
        rule_content_hash=ContentHash(sha256(b"activity-rule-v1").hexdigest()),
        data_quality_state="ELIGIBLE",
        open_interest=None if open_interest is None else Decimal(open_interest),
        volume_24h=None if volume_24h is None else Decimal(volume_24h),
        liquidity=None if liquidity is None else Decimal(liquidity),
        volume=None if volume is None else Decimal(volume),
        close_time=close_time,
    )


def test_activity_rank_key_prefers_higher_open_interest_over_alphabetical_ticker() -> None:
    # "AAA" sorts first alphabetically but has far less real trading activity;
    # activity ranking must not be dominated by ticker order.
    quiet = _activity_market("AAA", open_interest="10")
    active = _activity_market("ZZZ", open_interest="1000")

    assert _activity_rank_key(active) < _activity_rank_key(quiet)
    assert sorted((quiet, active), key=_activity_rank_key) == [active, quiet]


def test_activity_rank_key_missing_value_never_outranks_a_genuine_zero() -> None:
    # A retained zero is real evidence; a missing measurement is an evidence
    # gap and must never look more attractive than it, matching the sibling
    # threshold-ladder scan's own `descending()` idiom.
    unknown = _activity_market("AAA", open_interest=None)
    genuinely_zero = _activity_market("ZZZ", open_interest="0")

    assert _activity_rank_key(genuinely_zero) < _activity_rank_key(unknown)
    assert sorted((unknown, genuinely_zero), key=_activity_rank_key) == [
        genuinely_zero,
        unknown,
    ]


def test_activity_rank_key_falls_through_the_full_field_priority_on_ties() -> None:
    tied_on_open_interest = _activity_market("AAA", open_interest="100", volume_24h="5")
    wins_on_volume_24h = _activity_market("ZZZ", open_interest="100", volume_24h="50")

    assert sorted((tied_on_open_interest, wins_on_volume_24h), key=_activity_rank_key) == [
        wins_on_volume_24h,
        tied_on_open_interest,
    ]

    tied_through_volume_24h = _activity_market(
        "AAA", open_interest="100", volume_24h="50", liquidity="1"
    )
    wins_on_liquidity = _activity_market("ZZZ", open_interest="100", volume_24h="50", liquidity="9")

    assert sorted((tied_through_volume_24h, wins_on_liquidity), key=_activity_rank_key) == [
        wins_on_liquidity,
        tied_through_volume_24h,
    ]


def test_activity_rank_key_ticker_is_the_final_deterministic_tiebreak() -> None:
    # With zero retained activity evidence at all, ranking must reproduce the
    # prior alphabetical-only order exactly -- a strict refinement, not a
    # wholesale behavior replacement, when nothing distinguishes candidates.
    first = _activity_market("AAA")
    second = _activity_market("BBB")

    assert sorted((second, first), key=_activity_rank_key) == [first, second]


def test_select_completed_open_markets_ranks_by_activity_not_alphabetically() -> None:
    markets = (
        _activity_market("AAA", open_interest="10"),
        _activity_market("BBB", open_interest="1000"),
        _activity_market("CCC", open_interest=None),
        _activity_market("DDD", open_interest="500"),
    )
    store = InMemoryPublicBinaryComplementStore(markets)

    selected = store.select_completed_open_markets(
        ingestion_run_id="activity-run", market_tickers=(), limit=4
    )

    assert [value.provider_market_ticker for value in selected] == [
        "BBB",
        "DDD",
        "AAA",
        "CCC",
    ]


def test_select_completed_open_markets_limit_keeps_the_most_active_candidates() -> None:
    # Previously, alphabetical-only selection under a bound below the
    # discovered count would keep "AAA"/"BBB" and silently drop "DDD" even
    # though "DDD" is the second-most active market discovered.
    markets = (
        _activity_market("AAA", open_interest="10"),
        _activity_market("BBB", open_interest="1000"),
        _activity_market("CCC", open_interest=None),
        _activity_market("DDD", open_interest="500"),
    )
    store = InMemoryPublicBinaryComplementStore(markets)

    selected = store.select_completed_open_markets(
        ingestion_run_id="activity-run", market_tickers=(), limit=2
    )

    assert [value.provider_market_ticker for value in selected] == ["BBB", "DDD"]
