"""Synthetic provider-shaped evidence for threshold-ladder research only."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from apex.adapters.kalshi_capture_fixtures import VirtualCaptureClock
from apex.adapters.kalshi_public import (
    KALSHI_EVENTS_ENDPOINT_IDENTITY,
    KALSHI_MARKET_ENDPOINT_IDENTITY,
    KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY,
    KALSHI_SERIES_ENDPOINT_IDENTITY,
    KalshiPublicEventNormalizer,
    KalshiPublicMarketNormalizer,
)
from apex.adapters.kalshi_sweep_fixtures import fixture_capture_candidates
from apex.application.capture_public_orderbook_session import (
    CaptureBoundedPublicOrderbookSession,
    InMemoryPublicOrderbookCaptureStore,
)
from apex.application.discover_public_markets import (
    CapturedPublicMarketPage,
    DiscoverPublicPredictionMarkets,
    InMemoryPublicMarketDiscoveryStore,
)
from apex.application.scan_public_binary_complements import CapturedKalshiPublicResponse
from apex.application.scan_public_threshold_ladders import (
    InMemoryPublicThresholdLadderStore,
    PublicThresholdLadderScanReport,
    PublicThresholdLadderScanRequest,
    ScanPublicThresholdLadders,
)

FIXTURE_AT = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _FixtureMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    strike_type: str
    strike: str
    functional_strike: str
    scenario: str
    open_interest: str


_MARKETS = (
    _FixtureMarket(
        "THRESH-GT-10",
        "THRESH-GT",
        "THRESH-GT-S",
        "greater",
        "10",
        "temperature",
        "candidate",
        "1000",
    ),
    _FixtureMarket(
        "THRESH-GT-20",
        "THRESH-GT",
        "THRESH-GT-S",
        "greater",
        "20",
        "temperature",
        "candidate",
        "1000",
    ),
    _FixtureMarket(
        "THRESH-LT-20", "THRESH-LT", "THRESH-LT-S", "less", "20", "temperature", "fees", "900"
    ),
    _FixtureMarket(
        "THRESH-LT-10", "THRESH-LT", "THRESH-LT-S", "less", "10", "temperature", "fees", "900"
    ),
    _FixtureMarket(
        "THRESH-ROUND-10",
        "THRESH-ROUND",
        "THRESH-ROUND-S",
        "greater",
        "10",
        "temperature",
        "rounding",
        "800",
    ),
    _FixtureMarket(
        "THRESH-ROUND-20",
        "THRESH-ROUND",
        "THRESH-ROUND-S",
        "greater",
        "20",
        "temperature",
        "rounding",
        "800",
    ),
    _FixtureMarket(
        "THRESH-SKEW-10",
        "THRESH-SKEW",
        "THRESH-SKEW-S",
        "greater",
        "10",
        "temperature",
        "candidate",
        "700",
    ),
    _FixtureMarket(
        "THRESH-SKEW-20",
        "THRESH-SKEW",
        "THRESH-SKEW-S",
        "greater",
        "20",
        "temperature",
        "skew",
        "700",
    ),
    _FixtureMarket(
        "THRESH-MISSING-10",
        "THRESH-MISSING",
        "THRESH-MISSING-S",
        "greater",
        "10",
        "temperature",
        "candidate",
        "600",
    ),
    _FixtureMarket(
        "THRESH-MISSING-20",
        "THRESH-MISSING",
        "THRESH-MISSING-S",
        "greater",
        "20",
        "temperature",
        "missing",
        "600",
    ),
    _FixtureMarket(
        "THRESH-RANGE-A",
        "THRESH-RANGE",
        "THRESH-RANGE-S",
        "range",
        "10",
        "temperature",
        "candidate",
        "500",
    ),
    _FixtureMarket(
        "THRESH-RANGE-B",
        "THRESH-RANGE",
        "THRESH-RANGE-S",
        "range",
        "20",
        "temperature",
        "candidate",
        "500",
    ),
    _FixtureMarket(
        "THRESH-RULE-A",
        "THRESH-RULE",
        "THRESH-RULE-S",
        "greater",
        "10",
        "temperature",
        "candidate",
        "400",
    ),
    _FixtureMarket(
        "THRESH-RULE-B",
        "THRESH-RULE",
        "THRESH-RULE-S",
        "greater",
        "20",
        "rainfall",
        "candidate",
        "400",
    ),
    _FixtureMarket(
        "THRESH-SOURCE-A",
        "THRESH-SOURCE",
        "THRESH-SOURCE-S",
        "greater",
        "10",
        "temperature",
        "candidate",
        "300",
    ),
    _FixtureMarket(
        "THRESH-SOURCE-B",
        "THRESH-SOURCE",
        "THRESH-SOURCE-S",
        "greater",
        "20",
        "temperature",
        "candidate",
        "300",
    ),
    _FixtureMarket(
        "THRESH-ONE",
        "THRESH-ONE",
        "THRESH-ONE-S",
        "greater",
        "10",
        "temperature",
        "candidate",
        "200",
    ),
)


class KalshiThresholdFixtureClient:
    """GET-shaped fixture client with no network, credentials, or account path."""

    def __init__(self, clock: VirtualCaptureClock) -> None:
        self._clock = clock
        self._markets = {value.ticker: value for value in _MARKETS}
        self._event_calls: Counter[str] = Counter()
        self.calls: list[tuple[str, str]] = []

    def _response(
        self, *, endpoint_identity: str, document: Mapping[str, object]
    ) -> CapturedKalshiPublicResponse:
        return CapturedKalshiPublicResponse(
            endpoint_identity=endpoint_identity,
            received_at=self._clock.wall_clock(),
            http_status=200,
            response_content_type="application/json",
            raw_payload=json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
            document=document,
        )

    @staticmethod
    def _market_document(value: _FixtureMarket) -> dict[str, object]:
        document: dict[str, object] = {
            "ticker": value.ticker,
            "event_ticker": value.event_ticker,
            "series_ticker": value.series_ticker,
            "status": "active",
            "market_type": "binary",
            "strike_type": value.strike_type,
            "functional_strike": value.functional_strike,
            "response_price_units": "usd_cent",
            "price_level_structure": "binary",
            "open_time": "2026-07-22T13:00:00Z",
            "close_time": "2026-07-22T22:00:00Z",
            "yes_bid_dollars": "0.50",
            "yes_ask_dollars": "0.50",
            "no_bid_dollars": "0.50",
            "no_ask_dollars": "0.50",
            "yes_bid_size_fp": "10",
            "yes_ask_size_fp": "10",
            "volume_fp": value.open_interest,
            "volume_24h_fp": value.open_interest,
            "open_interest_fp": value.open_interest,
            "liquidity_dollars": value.open_interest,
            "rules_primary": f"Synthetic exact threshold rule {value.ticker}",
            "rules_secondary": "Synthetic common settlement mechanics",
            "title": "Title similarity is not read",
            "subtitle": "Ticker and title are not relationship evidence",
            "is_provisional": False,
        }
        if value.strike_type == "greater":
            document["floor_strike"] = value.strike
        elif value.strike_type == "less":
            document["cap_strike"] = value.strike
        else:
            document["floor_strike"] = value.strike
            document["cap_strike"] = "20"
        return document

    def fetch(
        self,
        *,
        status: str,
        page_size: int,
        cursor: str | None,
        timeout_seconds: int,
        exclude_multivariate: bool,
    ) -> CapturedPublicMarketPage:
        if status != "open" or not exclude_multivariate or timeout_seconds < 1:
            raise ValueError("fixture market discovery request is invalid")
        start = int(cursor) if cursor is not None else 0
        values = _MARKETS[start : start + page_size]
        next_start = start + len(values)
        response_cursor = str(next_start) if next_start < len(_MARKETS) else None
        self._clock.advance(0.1)
        document: dict[str, object] = {
            "markets": [self._market_document(value) for value in values]
        }
        if response_cursor is not None:
            document["cursor"] = response_cursor
        self.calls.append(("GET_MARKETS", cursor or ""))
        return CapturedPublicMarketPage(
            endpoint_identity="KALSHI_GET_MARKETS",
            request_cursor=cursor,
            received_at=self._clock.wall_clock(),
            http_status=200,
            response_content_type="application/json",
            raw_payload=json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
            response_cursor=response_cursor,
            document=document,
        )

    def fetch_event(
        self, *, event_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if timeout_seconds < 1:
            raise ValueError("fixture event request is invalid")
        values = tuple(value for value in _MARKETS if value.event_ticker == event_ticker)
        if not values:
            raise ValueError("fixture event is not registered")
        self._clock.advance(0.1)
        self._event_calls[event_ticker] += 1
        source_url = (
            "https://fixture-source-b.invalid"
            if event_ticker == "THRESH-SOURCE" and self._event_calls[event_ticker] % 2 == 0
            else "https://fixture-source-a.invalid"
        )
        self.calls.append(("GET_EVENT", event_ticker))
        return self._response(
            endpoint_identity=KALSHI_EVENTS_ENDPOINT_IDENTITY,
            document={
                "event": {
                    "event_ticker": event_ticker,
                    "series_ticker": values[0].series_ticker,
                    "status": "open",
                    "mutually_exclusive": False,
                    "settlement_sources": [{"name": "fixture", "url": source_url}],
                    "markets": [self._market_document(value) for value in values],
                }
            },
        )

    def fetch_market(
        self, *, market_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if timeout_seconds < 1 or market_ticker not in self._markets:
            raise ValueError("fixture exact market request is invalid")
        self._clock.advance(0.1)
        self.calls.append(("GET_MARKET", market_ticker))
        return self._response(
            endpoint_identity=KALSHI_MARKET_ENDPOINT_IDENTITY,
            document={"market": self._market_document(self._markets[market_ticker])},
        )

    def fetch_series(
        self, *, series_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if timeout_seconds < 1 or not any(
            value.series_ticker == series_ticker for value in _MARKETS
        ):
            raise ValueError("fixture series request is invalid")
        self._clock.advance(0.1)
        self.calls.append(("GET_SERIES", series_ticker))
        return self._response(
            endpoint_identity=KALSHI_SERIES_ENDPOINT_IDENTITY,
            document={
                "series": {
                    "ticker": series_ticker,
                    "fee_type": "quadratic",
                    "fee_multiplier": "1",
                    "last_updated_ts": "2026-07-22T14:00:00Z",
                }
            },
        )

    def fetch_series_fee_changes(
        self, *, series_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        raise AssertionError("fixture series metadata always carries exact fee evidence")

    def fetch_orderbook(
        self, *, market_ticker: str, maximum_depth: int, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if (
            market_ticker not in self._markets
            or not 1 <= maximum_depth <= 20
            or timeout_seconds < 1
        ):
            raise ValueError("fixture orderbook request is invalid")
        value = self._markets[market_ticker]
        self._clock.advance(6 if value.scenario == "skew" else 0.1)
        self.calls.append(("GET_ORDERBOOK", market_ticker))
        if value.scenario == "missing":
            yes, no = [], [["0.60", "10"]]
        elif value.scenario == "fees":
            yes = no = [["0.51", "10"]]
        elif value.scenario == "rounding":
            yes = no = [["0.525", "0.01"]]
        else:
            yes = no = [["0.60", "10"]]
        return self._response(
            endpoint_identity=KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY,
            document={"orderbook_fp": {"yes_dollars": yes, "no_dollars": no}},
        )


def threshold_ladder_fixture(
    *, maximum_relationships: int = 100
) -> PublicThresholdLadderScanReport:
    """Run the normal discovery, enrichment, relationship, and capture paths offline."""

    clock = VirtualCaptureClock(now=FIXTURE_AT)
    client = KalshiThresholdFixtureClient(clock)
    discovery_store = InMemoryPublicMarketDiscoveryStore()
    capture = CaptureBoundedPublicOrderbookSession(
        client=client,
        store=InMemoryPublicOrderbookCaptureStore(),
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
        normalizer=KalshiPublicMarketNormalizer(),
    )
    normalizer = KalshiPublicMarketNormalizer()
    return ScanPublicThresholdLadders(
        discovery=DiscoverPublicPredictionMarkets(
            client=client,
            normalizer=normalizer,
            store=discovery_store,
            clock=clock.wall_clock,
        ),
        capture=capture,
        client=client,
        candidate_selector=lambda run_id, limit: fixture_capture_candidates(
            discovery_store, run_id, limit
        ),
        store=InMemoryPublicThresholdLadderStore(),
        normalizer=normalizer,
        event_normalizer=KalshiPublicEventNormalizer(),
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
    ).scan(
        PublicThresholdLadderScanRequest(
            scan_id=f"fixture-public-kalshi-threshold-ladder-{maximum_relationships}-v1",
            event_count=5,
            maximum_pages=3,
            market_limit=100,
            maximum_enrichments=20,
            maximum_markets_per_event=8,
            maximum_relationships=maximum_relationships,
            rounds=3,
            interval_seconds=10,
            maximum_depth=5,
            maximum_bundle_quantity="10",
            maximum_observation_skew_seconds=5,
            timeout_seconds=10,
        )
    )
