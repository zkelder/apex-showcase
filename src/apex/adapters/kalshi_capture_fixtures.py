"""Synthetic, no-network bounded public orderbook capture fixtures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from apex.adapters.kalshi_public import (
    KALSHI_EVENTS_ENDPOINT_IDENTITY,
    KALSHI_MARKET_ENDPOINT_IDENTITY,
    KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY,
    KALSHI_SERIES_ENDPOINT_IDENTITY,
)
from apex.application.capture_public_orderbook_session import (
    PublicOrderbookCaptureTransientError,
)
from apex.application.scan_public_binary_complements import CapturedKalshiPublicResponse

MARKET_TICKER = "CAPTURE-FIXTURE-MARKET"
SERIES_TICKER = "CAPTURE-FIXTURE-SERIES"
BASE_TIME = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)


@dataclass(slots=True)
class VirtualCaptureClock:
    now: datetime = BASE_TIME
    monotonic_seconds: float = 0.0

    def wall_clock(self) -> datetime:
        return self.now

    def monotonic_clock(self) -> float:
        return self.monotonic_seconds

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("fixture sleep cannot be negative")
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.monotonic_seconds += seconds
        self.now += timedelta(seconds=seconds)


class KalshiFixtureOrderbookCaptureClient:
    """Five sequential attempts: no-candidate, two candidates, transient failure, no-candidate."""

    def __init__(self, clock: VirtualCaptureClock) -> None:
        self._clock = clock
        self._orderbook_calls = 0

    def _response(
        self, *, endpoint_identity: str, document: dict[str, object]
    ) -> CapturedKalshiPublicResponse:
        return CapturedKalshiPublicResponse(
            endpoint_identity=endpoint_identity,
            received_at=self._clock.wall_clock(),
            http_status=200,
            response_content_type="application/json",
            raw_payload=json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
            document=document,
        )

    def fetch_market(
        self, *, market_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if market_ticker != MARKET_TICKER or timeout_seconds < 1:
            raise ValueError("fixture market request is invalid")
        self._clock.advance(0.25)
        return self._response(
            endpoint_identity=KALSHI_MARKET_ENDPOINT_IDENTITY,
            document={
                "market": {
                    "ticker": MARKET_TICKER,
                    "event_ticker": "CAPTURE-FIXTURE-EVENT",
                    "series_ticker": SERIES_TICKER,
                    "status": "open",
                    "open_time": "2026-07-20T13:00:00Z",
                    "close_time": "2026-07-20T18:00:00Z",
                    "yes_bid_dollars": "0.45",
                    "yes_ask_dollars": "0.55",
                    "no_bid_dollars": "0.45",
                    "no_ask_dollars": "0.55",
                    "yes_bid_size_fp": "10.00",
                    "yes_ask_size_fp": "10.00",
                    "rules_primary": "Synthetic bounded capture rule",
                }
            },
        )

    def fetch_event(
        self, *, event_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if event_ticker != "CAPTURE-FIXTURE-EVENT" or timeout_seconds < 1:
            raise ValueError("fixture event request is invalid")
        self._clock.advance(0.25)
        return self._response(
            endpoint_identity=KALSHI_EVENTS_ENDPOINT_IDENTITY,
            document={
                "event": {
                    "event_ticker": "CAPTURE-FIXTURE-EVENT",
                    "series_ticker": SERIES_TICKER,
                    "markets": [
                        {
                            "ticker": MARKET_TICKER,
                            "event_ticker": "CAPTURE-FIXTURE-EVENT",
                            "rules_primary": "Synthetic bounded capture rule",
                        }
                    ],
                }
            },
        )

    def fetch_orderbook(
        self, *, market_ticker: str, maximum_depth: int, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if market_ticker != MARKET_TICKER or not 1 <= maximum_depth <= 20 or timeout_seconds < 1:
            raise ValueError("fixture orderbook request is invalid")
        ordinal = self._orderbook_calls
        self._orderbook_calls += 1
        self._clock.advance(1)
        if ordinal == 3:
            raise PublicOrderbookCaptureTransientError("synthetic transient provider failure")
        bid = "0.53" if ordinal in {1, 2} else "0.50"
        return self._response(
            endpoint_identity=KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY,
            document={
                "orderbook_fp": {
                    "yes_dollars": [[bid, "10.00"]],
                    "no_dollars": [[bid, "10.00"]],
                }
            },
        )

    def fetch_series(
        self, *, series_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if series_ticker != SERIES_TICKER or timeout_seconds < 1:
            raise ValueError("fixture series request is invalid")
        self._clock.advance(0.25)
        return self._response(
            endpoint_identity=KALSHI_SERIES_ENDPOINT_IDENTITY,
            document={
                "series": {
                    "ticker": SERIES_TICKER,
                    "fee_type": "quadratic",
                    "fee_multiplier": "1",
                    "last_updated_ts": "2026-07-20T14:00:00Z",
                }
            },
        )

    def fetch_series_fee_changes(self, **kwargs: object) -> CapturedKalshiPublicResponse:
        raise AssertionError("fixture current series fee evidence is exact")
