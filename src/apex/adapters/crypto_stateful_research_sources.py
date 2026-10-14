"""Bounded public and deterministic fixture daily-open evidence adapters."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlencode

from apex.adapters.coinbase_universe_public import CANDLES_PREFIX, _read
from apex.application.crypto_stateful_research import CapturedExecutionHistoryPage
from apex.domain.crypto_stateful_research import CryptoExecutionCandle


def _parse_open_close(
    raw: bytes, *, interval_start: datetime, interval_end: datetime
) -> tuple[CryptoExecutionCandle, ...]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("COINBASE_EXECUTION_CANDLES_JSON_INVALID") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise ValueError("COINBASE_EXECUTION_CANDLES_SCHEMA_INVALID")
    accepted: dict[datetime, CryptoExecutionCandle] = {}
    for row in payload["candles"]:
        if not isinstance(row, dict):
            raise ValueError("COINBASE_EXECUTION_CANDLE_ROW_INVALID")
        try:
            timestamp = datetime.fromtimestamp(int(row["start"]), tz=UTC)
            candle = CryptoExecutionCandle.create(
                bucket_start=timestamp,
                open_price=row["open"],
                close_price=row["close"],
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as error:
            raise ValueError("COINBASE_EXECUTION_CANDLE_VALUE_INVALID") from error
        if not interval_start <= timestamp < interval_end or timestamp in accepted:
            raise ValueError("COINBASE_EXECUTION_CANDLE_BUCKET_INVALID")
        accepted[timestamp] = candle
    expected = (interval_end - interval_start).days
    if len(accepted) != expected:
        raise ValueError("COINBASE_EXECUTION_CANDLE_COVERAGE_INCOMPLETE")
    return tuple(accepted[value] for value in sorted(accepted))


class CoinbasePublicExecutionHistorySource:
    """Read only the existing public daily-candle route; never use credentials."""

    evidence_class = "HISTORICAL_PUBLIC"
    network_access = True

    def capture(
        self,
        *,
        product_id: str,
        interval_start: datetime,
        interval_end: datetime,
        requested_at: datetime,
    ) -> CapturedExecutionHistoryPage:
        expected = (interval_end - interval_start).days
        if not 91 <= expected <= 300 or interval_end > requested_at.astimezone(UTC):
            raise ValueError("COINBASE_EXECUTION_CANDLE_REQUEST_BOUND_INVALID")
        query = urlencode(
            {
                "start": int(interval_start.timestamp()) - 1,
                "end": int(interval_end.timestamp()) - 1,
                "granularity": "ONE_DAY",
                "limit": expected,
            }
        )
        path = f"{CANDLES_PREFIX}{product_id}/candles?{query}"
        raw, completed_at, _ = _read(path)
        return CapturedExecutionHistoryPage(
            candles=_parse_open_close(
                raw, interval_start=interval_start, interval_end=interval_end
            ),
            raw_payload=raw,
            captured_at=completed_at,
        )


class FixtureExecutionHistorySource:
    """Deterministic test source built from exact predeclared candle rows."""

    evidence_class = "HISTORICAL_PUBLIC"
    network_access = False

    def __init__(self, candles: dict[str, tuple[CryptoExecutionCandle, ...]]) -> None:
        self._candles = candles
        self.request_count = 0

    def capture(
        self,
        *,
        product_id: str,
        interval_start: datetime,
        interval_end: datetime,
        requested_at: datetime,
    ) -> CapturedExecutionHistoryPage:
        self.request_count += 1
        values = tuple(
            value
            for value in self._candles[product_id]
            if interval_start <= value.bucket_start < interval_end
        )
        payload = json.dumps(
            {
                "candles": [
                    {
                        "start": str(int(value.bucket_start.timestamp())),
                        "open": str(value.open),
                        "close": str(value.close),
                    }
                    for value in values
                ]
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return CapturedExecutionHistoryPage(values, payload, requested_at.astimezone(UTC))
