"""Synthetic provider-shaped public orderbook and series fixtures only."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from apex.adapters.kalshi_public import (
    KALSHI_EVENTS_ENDPOINT_IDENTITY,
    KALSHI_MARKET_ENDPOINT_IDENTITY,
    KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY,
    KALSHI_SERIES_ENDPOINT_IDENTITY,
)
from apex.application.scan_public_binary_complements import (
    CapturedKalshiPublicResponse,
    ComplementMarket,
)
from apex.domain.values import ContentHash

FIXTURE_AT = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
MARKET_TICKER = "COMPLEMENT-MARKET"
SERIES_TICKER = "COMPLEMENT-SERIES"


def fixture_market(*, scenario: str = "positive") -> ComplementMarket:
    return ComplementMarket(
        market_snapshot_id=f"fixture-market-{scenario}",
        ingestion_run_id=f"fixture-run-{scenario}",
        provider_market_ticker=MARKET_TICKER,
        provider_event_ticker="COMPLEMENT-EVENT",
        series_ticker=SERIES_TICKER,
        status="open",
        observed_at=FIXTURE_AT,
        rule_content_hash=(
            None if scenario == "rule_mismatch" else ContentHash(sha256(b"rule-v1").hexdigest())
        ),
        data_quality_state="ELIGIBLE",
    )


def _levels(*, yes: list[list[str]], no: list[list[str]]) -> dict[str, object]:
    return {"orderbook_fp": {"yes_dollars": yes, "no_dollars": no}}


class KalshiFixtureBinaryComplementClient:
    """No-network fixed public payloads for all bounded complement edge cases."""

    def __init__(self, *, scenario: str = "positive") -> None:
        if scenario not in {
            "empty",
            "one_sided",
            "non_candidate",
            "fees_eliminate",
            "rounding_eliminate",
            "subpenny_fractional",
            "positive",
            "multiple_depth",
            "quantity_limited",
            "depth_limited",
            "fee_changes_fallback",
            "zero_multiplier",
            "unsupported_fee",
            "stale_fee",
            "rule_mismatch",
            "malformed_price",
            "negative_quantity",
        }:
            raise ValueError("fixture complement scenario is not registered")
        self.scenario = scenario

    @staticmethod
    def _response(
        *, endpoint_identity: str, document: Mapping[str, object], received_at: datetime
    ) -> CapturedKalshiPublicResponse:
        return CapturedKalshiPublicResponse(
            endpoint_identity=endpoint_identity,
            received_at=received_at,
            http_status=200,
            response_content_type="application/json",
            raw_payload=json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            document=document,
        )

    def fetch_orderbook(
        self,
        *,
        market_ticker: str,
        maximum_depth: int,
        timeout_seconds: int,
    ) -> CapturedKalshiPublicResponse:
        if market_ticker != MARKET_TICKER or not 1 <= maximum_depth <= 100 or timeout_seconds < 1:
            raise ValueError("fixture orderbook request is not registered")
        scenario = self.scenario
        if scenario == "empty":
            document = _levels(yes=[], no=[])
        elif scenario == "one_sided":
            document = _levels(yes=[["0.53", "10.00"]], no=[])
        elif scenario == "non_candidate":
            document = _levels(yes=[["0.50", "10.00"]], no=[["0.50", "10.00"]])
        elif scenario == "fees_eliminate":
            document = _levels(yes=[["0.51", "10.00"]], no=[["0.51", "10.00"]])
        elif scenario == "rounding_eliminate":
            document = _levels(yes=[["0.525", "0.01"]], no=[["0.525", "0.01"]])
        elif scenario == "subpenny_fractional":
            document = _levels(yes=[["0.6699", "0.03"]], no=[["0.6699", "0.03"]])
        elif scenario == "multiple_depth":
            document = _levels(
                yes=[["0.50", "3.00"], ["0.54", "7.00"]],
                no=[["0.49", "4.00"], ["0.53", "6.00"]],
            )
        elif scenario == "quantity_limited":
            document = _levels(yes=[["0.53", "2.00"]], no=[["0.53", "10.00"]])
        elif scenario == "depth_limited":
            document = _levels(
                yes=[["0.53", "10.00"], ["0.54", "2.00"]],
                no=[["0.53", "10.00"], ["0.54", "2.00"]],
            )
        elif scenario == "malformed_price":
            document = _levels(yes=[["not-a-price", "10.00"]], no=[["0.53", "10.00"]])
        elif scenario == "negative_quantity":
            document = _levels(yes=[["0.53", "-1.00"]], no=[["0.53", "10.00"]])
        else:
            document = _levels(yes=[["0.53", "10.00"]], no=[["0.53", "10.00"]])
        return self._response(
            endpoint_identity=KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY,
            document=document,
            received_at=FIXTURE_AT,
        )

    def fetch_market(
        self, *, market_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if market_ticker != MARKET_TICKER or timeout_seconds < 1:
            raise ValueError("fixture market request is not registered")
        return self._response(
            endpoint_identity=KALSHI_MARKET_ENDPOINT_IDENTITY,
            document={
                "market": {
                    "ticker": MARKET_TICKER,
                    "series_ticker": SERIES_TICKER,
                }
            },
            received_at=FIXTURE_AT,
        )

    def fetch_event(
        self, *, event_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if event_ticker != "COMPLEMENT-EVENT" or timeout_seconds < 1:
            raise ValueError("fixture event request is not registered")
        return self._response(
            endpoint_identity=KALSHI_EVENTS_ENDPOINT_IDENTITY,
            document={
                "event": {
                    "event_ticker": "COMPLEMENT-EVENT",
                    "series_ticker": SERIES_TICKER,
                    "markets": [
                        {
                            "ticker": MARKET_TICKER,
                            "event_ticker": "COMPLEMENT-EVENT",
                            "rules_primary": "rule-v1",
                        }
                    ],
                }
            },
            received_at=FIXTURE_AT,
        )

    def fetch_series(
        self, *, series_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if series_ticker != SERIES_TICKER or timeout_seconds < 1:
            raise ValueError("fixture series request is not registered")
        fee_type: object = "quadratic"
        multiplier: object = "1"
        if self.scenario == "zero_multiplier":
            multiplier = "0"
        if self.scenario == "unsupported_fee":
            fee_type = "flat"
        if self.scenario == "fee_changes_fallback":
            fee_type = None
            multiplier = None
        received_at = (
            FIXTURE_AT - timedelta(hours=1) if self.scenario == "stale_fee" else FIXTURE_AT
        )
        return self._response(
            endpoint_identity=KALSHI_SERIES_ENDPOINT_IDENTITY,
            document={
                "series": {
                    "ticker": SERIES_TICKER,
                    "fee_type": fee_type,
                    "fee_multiplier": multiplier,
                    "last_updated_ts": "2026-07-20T12:00:00Z",
                }
            },
            received_at=received_at,
        )

    def fetch_series_fee_changes(
        self, *, series_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        if self.scenario == "fee_changes_fallback" and series_ticker == SERIES_TICKER:
            return self._response(
                endpoint_identity="KALSHI_GET_SERIES_FEE_CHANGES",
                document={
                    "series_fee_change_arr": [
                        {
                            "series_ticker": SERIES_TICKER,
                            "fee_type": "quadratic",
                            "fee_multiplier": "1",
                            "scheduled_ts": "2026-07-20T11:00:00Z",
                        },
                        {
                            "series_ticker": SERIES_TICKER,
                            "fee_type": "flat",
                            "fee_multiplier": "99",
                            "scheduled_ts": "2026-07-20T13:00:00Z",
                        },
                    ]
                },
                received_at=FIXTURE_AT,
            )
        raise AssertionError("fixture series metadata carries exact current fee evidence")
