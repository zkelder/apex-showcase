"""Synthetic provider-shaped data for the bounded public orderbook sweep only."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from apex.adapters.kalshi_capture_fixtures import VirtualCaptureClock
from apex.adapters.kalshi_public import (
    KALSHI_EVENTS_ENDPOINT_IDENTITY,
    KALSHI_MARKET_ENDPOINT_IDENTITY,
    KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY,
    KALSHI_SERIES_ENDPOINT_IDENTITY,
)
from apex.application.capture_public_orderbook_session import CaptureMarketCandidate
from apex.application.discover_public_markets import (
    CapturedPublicMarketPage,
    InMemoryPublicMarketDiscoveryStore,
)
from apex.application.scan_public_binary_complements import CapturedKalshiPublicResponse
from apex.domain.public_market import PublicMarketDataQualityState

FIXTURE_AT = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _FixtureMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    scenario: str
    open_interest: str | None
    volume_24h: str | None
    liquidity: str | None
    volume: str | None
    is_provisional: bool = False
    event_membership_valid: bool = True
    fee_evidence_valid: bool = True


_MARKETS = (
    _FixtureMarket(
        "SWEEP-HIGH-NON-CANDIDATE",
        "SWEEP-EVENT-A",
        "SWEEP-SERIES-A",
        "non_candidate",
        "1000",
        "1000",
        "1000",
        "1000",
    ),
    _FixtureMarket(
        "SWEEP-LOW-PERSISTING-CANDIDATE",
        "SWEEP-EVENT-B",
        "SWEEP-SERIES-B",
        "positive",
        "900",
        "900",
        "900",
        "900",
    ),
    _FixtureMarket(
        "SWEEP-FEES-ELIMINATE",
        "SWEEP-EVENT-C",
        "SWEEP-SERIES-C",
        "fees_eliminate",
        "800",
        "800",
        "800",
        "800",
    ),
    _FixtureMarket(
        "SWEEP-ROUNDING-ELIMINATE",
        "SWEEP-EVENT-D",
        "SWEEP-SERIES-D",
        "rounding_eliminate",
        "700",
        "700",
        "700",
        "700",
    ),
    _FixtureMarket(
        "SWEEP-EMPTY",
        "SWEEP-EVENT-E",
        "SWEEP-SERIES-E",
        "empty",
        "600",
        "600",
        "600",
        "600",
    ),
    _FixtureMarket(
        "SWEEP-ONE-SIDED",
        "SWEEP-EVENT-F",
        "SWEEP-SERIES-F",
        "one_sided",
        "500",
        "500",
        "500",
        "500",
    ),
    _FixtureMarket(
        "SWEEP-FAILED-SESSION",
        "SWEEP-EVENT-G",
        "SWEEP-SERIES-G",
        "failed",
        "400",
        "400",
        "400",
        "400",
    ),
    _FixtureMarket(
        "SWEEP-EVENT-REJECTED",
        "SWEEP-EVENT-REJECTED",
        "SWEEP-SERIES-H",
        "non_candidate",
        "390",
        "390",
        "390",
        "390",
        event_membership_valid=False,
    ),
    _FixtureMarket(
        "SWEEP-FEE-REJECTED",
        "SWEEP-EVENT-I",
        "SWEEP-SERIES-FEE-REJECTED",
        "non_candidate",
        "380",
        "380",
        "380",
        "380",
        fee_evidence_valid=False,
    ),
    _FixtureMarket(
        "SWEEP-TIE-A",
        "SWEEP-EVENT-TIE",
        "SWEEP-SERIES-TIE",
        "positive",
        "300",
        "300",
        "300",
        "300",
    ),
    _FixtureMarket(
        "SWEEP-TIE-B",
        "SWEEP-EVENT-TIE",
        "SWEEP-SERIES-TIE",
        "non_candidate",
        "300",
        "300",
        "300",
        "300",
    ),
    _FixtureMarket(
        "SWEEP-TIE-C-CAPPED",
        "SWEEP-EVENT-TIE",
        "SWEEP-SERIES-TIE",
        "non_candidate",
        "300",
        "300",
        "300",
        "300",
    ),
    _FixtureMarket(
        "SWEEP-SERIES-CAP-A",
        "SWEEP-EVENT-J",
        "SWEEP-SERIES-SHARED",
        "non_candidate",
        "250",
        "250",
        "250",
        "250",
    ),
    _FixtureMarket(
        "SWEEP-SERIES-CAP-B",
        "SWEEP-EVENT-K",
        "SWEEP-SERIES-SHARED",
        "non_candidate",
        "240",
        "240",
        "240",
        "240",
    ),
    _FixtureMarket(
        "SWEEP-SERIES-CAP-C",
        "SWEEP-EVENT-L",
        "SWEEP-SERIES-SHARED",
        "non_candidate",
        "230",
        "230",
        "230",
        "230",
    ),
    _FixtureMarket(
        "SWEEP-SERIES-CAP-D",
        "SWEEP-EVENT-M",
        "SWEEP-SERIES-SHARED",
        "non_candidate",
        "220",
        "220",
        "220",
        "220",
    ),
    _FixtureMarket(
        "SWEEP-MISSING-ACTIVITY",
        "SWEEP-EVENT-N",
        "SWEEP-SERIES-N",
        "non_candidate",
        None,
        None,
        None,
        None,
    ),
)


class KalshiFixtureOrderbookSweepClient:
    """No-network pages and exact endpoints covering the sweep's bounded scenarios."""

    def __init__(self, clock: VirtualCaptureClock) -> None:
        self._clock = clock
        self._markets = {value.ticker: value for value in _MARKETS}
        self.market_calls: list[str] = []
        self.orderbook_calls: list[str] = []

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
            "open_time": "2026-07-21T13:00:00Z",
            "close_time": "2026-07-21T22:00:00Z",
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.55",
            "no_bid_dollars": "0.45",
            "no_ask_dollars": "0.55",
            "yes_bid_size_fp": "10",
            "yes_ask_size_fp": "10",
            "rules_primary": f"Synthetic exact rule for {value.ticker}",
            "is_provisional": value.is_provisional,
        }
        if value.open_interest is not None:
            document["open_interest_fp"] = value.open_interest
        if value.volume_24h is not None:
            document["volume_24h_fp"] = value.volume_24h
        if value.liquidity is not None:
            document["liquidity_dollars"] = value.liquidity
        if value.volume is not None:
            document["volume_fp"] = value.volume
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
            raise ValueError("fixture discovery request is invalid")
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
        value = values[0]
        self._clock.advance(0.1)
        markets: list[dict[str, object]] = []
        if value.event_membership_valid:
            markets = [
                {
                    "ticker": item.ticker,
                    "event_ticker": item.event_ticker,
                    "series_ticker": item.series_ticker,
                    "rules_primary": self._market_document(item)["rules_primary"],
                }
                for item in values
            ]
        return self._response(
            endpoint_identity=KALSHI_EVENTS_ENDPOINT_IDENTITY,
            document={
                "event": {
                    "event_ticker": event_ticker,
                    "series_ticker": value.series_ticker,
                    "markets": markets,
                }
            },
        )

    def fetch_market(
        self, *, market_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if timeout_seconds < 1 or market_ticker not in self._markets:
            raise ValueError("fixture exact market request is invalid")
        self._clock.advance(0.1)
        self.market_calls.append(market_ticker)
        return self._response(
            endpoint_identity=KALSHI_MARKET_ENDPOINT_IDENTITY,
            document={"market": self._market_document(self._markets[market_ticker])},
        )

    def fetch_orderbook(
        self, *, market_ticker: str, maximum_depth: int, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if (
            market_ticker not in self._markets
            or not 1 <= maximum_depth <= 20
            or timeout_seconds < 1
        ):
            raise ValueError("fixture orderbook request is invalid")
        self._clock.advance(0.1)
        self.orderbook_calls.append(market_ticker)
        scenario = self._markets[market_ticker].scenario
        yes: list[list[str]]
        no: list[list[str]]
        if scenario == "failed":
            raise ValueError("synthetic market-specific capture failure")
        if scenario == "empty":
            yes, no = [], []
        elif scenario == "one_sided":
            yes, no = [["0.53", "10"]], []
        elif scenario == "non_candidate":
            yes, no = [["0.50", "10"]], [["0.50", "10"]]
        elif scenario == "fees_eliminate":
            yes, no = [["0.51", "10"]], [["0.51", "10"]]
        elif scenario == "rounding_eliminate":
            yes, no = [["0.525", "0.01"]], [["0.525", "0.01"]]
        else:
            yes, no = [["0.53", "10"]], [["0.53", "10"]]
        return self._response(
            endpoint_identity=KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY,
            document={"orderbook_fp": {"yes_dollars": yes, "no_dollars": no}},
        )

    def fetch_series(
        self, *, series_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if timeout_seconds < 1:
            raise ValueError("fixture series request is invalid")
        values = tuple(value for value in _MARKETS if value.series_ticker == series_ticker)
        if not values:
            raise ValueError("fixture series is not registered")
        self._clock.advance(0.1)
        series: dict[str, object] = {
            "ticker": series_ticker,
            "last_updated_ts": "2026-07-21T14:00:00Z",
        }
        if values[0].fee_evidence_valid:
            series.update({"fee_type": "quadratic", "fee_multiplier": "1"})
        return self._response(
            endpoint_identity=KALSHI_SERIES_ENDPOINT_IDENTITY,
            document={"series": series},
        )

    def fetch_series_fee_changes(
        self, *, series_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if timeout_seconds < 1:
            raise ValueError("fixture series fee request is invalid")
        self._clock.advance(0.1)
        return self._response(
            endpoint_identity="KALSHI_GET_SERIES_FEE_CHANGES",
            document={"series_fee_change_arr": []},
        )


def fixture_capture_candidates(
    store: InMemoryPublicMarketDiscoveryStore, ingestion_run_id: str, limit: int
) -> tuple[CaptureMarketCandidate, ...]:
    """Read normal discovery evidence into the same capture-candidate seam as PostgreSQL."""

    values: list[CaptureMarketCandidate] = []
    for snapshot in store.snapshots.values():
        raw = store.raw_responses.get(snapshot.raw_response_id)
        if (
            raw is None
            or raw.ingestion_run_id != ingestion_run_id
            or snapshot.provider_market_ticker is None
            or snapshot.event_ticker is None
            or snapshot.status != "open"
            or snapshot.rule_content_hash is None
        ):
            continue
        evaluation = next(
            (
                value
                for value in store.evaluations.values()
                if value.snapshot_id == snapshot.snapshot_id
            ),
            None,
        )
        if evaluation is None or evaluation.state is not PublicMarketDataQualityState.ELIGIBLE:
            continue
        markets = raw.page.document.get("markets")
        item = (
            markets[snapshot.provider_payload_ordinal]
            if isinstance(markets, list) and snapshot.provider_payload_ordinal < len(markets)
            else None
        )
        values.append(
            CaptureMarketCandidate(
                market_snapshot_id=snapshot.snapshot_id,
                ingestion_run_id=ingestion_run_id,
                provider_market_ticker=snapshot.provider_market_ticker,
                provider_event_ticker=snapshot.event_ticker,
                status=snapshot.status,
                observed_at=snapshot.observed_at,
                close_time=snapshot.close_time,
                is_provisional=isinstance(item, dict) and item.get("is_provisional") is not False,
                rule_content_hash=snapshot.rule_content_hash,
                data_quality_state=evaluation.state.value,
                volume=snapshot.volume,
                volume_24h=snapshot.volume_24h,
                open_interest=snapshot.open_interest,
                liquidity=snapshot.liquidity,
            )
        )
    return tuple(values[:limit])
