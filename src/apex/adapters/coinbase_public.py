"""Bounded Coinbase public daily-candle source and deterministic fixture.

The genuine adapter can make exactly one unauthenticated HTTPS GET per call to
the allowlisted Advanced Trade public market-data route.  It has no credential,
account, balance, fee, WebSocket, or order capability.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import ssl
import time
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

from apex.application.crypto_research import (
    CapturedCoinbasePartition,
    CoinbaseCandleInput,
    CryptoSourceFailure,
)
from apex.domain.crypto_research import (
    COINBASE_GRANULARITY,
    COINBASE_PRODUCT,
    MAX_BUCKETS_PER_REQUEST,
    MAX_REQUEST_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    CryptoCapturePartition,
    CryptoHistoricalCapture,
    expected_bucket_starts,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.values import canonical_decimal, parse_decimal

COINBASE_PUBLIC_HOST = "api.coinbase.com"
COINBASE_PUBLIC_PATH_PREFIX = "/api/v3/brokerage/market/products/"
COINBASE_PUBLIC_PATH_SUFFIX = "/candles"
COINBASE_DECLARED_LIMITATIONS = (
    "RETROSPECTIVE_PROVIDER_HISTORY_NOT_VINTAGE_POINT_IN_TIME",
    "PUBLIC_CANDLES_MAY_BE_REVISED_BY_PROVIDER",
    "PROVIDER_BOUNDARY_CURSOR_USES_ONE_SECOND_PREDECESSORS",
    "NO_AUTHENTICATED_FEE_TIER_EVIDENCE",
    "DISPLAYED_DAILY_OHLCV_ONLY",
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _runtime_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _locator(partition: CryptoCapturePartition) -> str:
    query = urlencode(
        {
            # Observed Coinbase boundary semantics are exclusive at `start` and
            # inclusive at `end`. One second before each aligned boundary maps
            # the provider cursor to Apex's exact [start, end) interval; parsing
            # still rejects every record outside that interval.
            "start": str(int(partition.start.timestamp()) - 1),
            "end": str(int(partition.end.timestamp()) - 1),
            "granularity": COINBASE_GRANULARITY,
            "limit": str(partition.expected_buckets),
        }
    )
    return (
        f"https://{COINBASE_PUBLIC_HOST}{COINBASE_PUBLIC_PATH_PREFIX}"
        f"{COINBASE_PRODUCT}{COINBASE_PUBLIC_PATH_SUFFIX}?{query}"
    )


def _parse_candles(
    raw_payload: bytes,
    *,
    partition: CryptoCapturePartition,
) -> tuple[tuple[CoinbaseCandleInput, ...], tuple[datetime, ...], int, int]:
    try:
        decoded = json.loads(raw_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("COINBASE_PUBLIC_RESPONSE_JSON_INVALID") from error
    if not isinstance(decoded, dict) or set(decoded) != {"candles"}:
        raise ValueError("COINBASE_PUBLIC_RESPONSE_SCHEMA_INVALID")
    records = decoded["candles"]
    if not isinstance(records, list) or len(records) > MAX_BUCKETS_PER_REQUEST:
        raise ValueError("COINBASE_PUBLIC_RESPONSE_CANDLE_BOUND_INVALID")

    accepted: dict[datetime, CoinbaseCandleInput] = {}
    normalized_by_bucket: dict[datetime, tuple[str, ...]] = {}
    rejected = 0
    duplicates = 0
    for record in records:
        try:
            if not isinstance(record, dict) or set(record) != {
                "start",
                "low",
                "high",
                "open",
                "close",
                "volume",
            }:
                raise ValueError("COINBASE_PUBLIC_CANDLE_SCHEMA_INVALID")
            raw_start = record["start"]
            if isinstance(raw_start, bool) or not isinstance(raw_start, (str, int)):
                raise ValueError("COINBASE_PUBLIC_CANDLE_START_INVALID")
            seconds = int(raw_start)
            if str(seconds) != str(raw_start):
                raise ValueError("COINBASE_PUBLIC_CANDLE_START_INVALID")
            bucket = datetime.fromtimestamp(seconds, tz=UTC)
            if (
                bucket.hour
                or bucket.minute
                or bucket.second
                or bucket.microsecond
                or not partition.start <= bucket < partition.end
            ):
                raise ValueError("COINBASE_PUBLIC_CANDLE_BUCKET_INVALID")
            low = parse_decimal(record["low"])
            high = parse_decimal(record["high"])
            open_price = parse_decimal(record["open"])
            close = parse_decimal(record["close"])
            volume = parse_decimal(record["volume"])
            if (
                min(low, high, open_price, close) <= 0
                or volume < 0
                or high < max(low, open_price, close)
                or low > min(high, open_price, close)
            ):
                raise ValueError("COINBASE_PUBLIC_CANDLE_OHLC_INVALID")
            normalized = tuple(
                canonical_decimal(value) for value in (open_price, high, low, close, volume)
            )
            if bucket in accepted:
                duplicates += 1
                if normalized_by_bucket[bucket] != normalized:
                    raise ValueError("COINBASE_PUBLIC_DUPLICATE_CONTENT_CONFLICT")
                continue
            accepted[bucket] = CoinbaseCandleInput(
                bucket_start=bucket,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
            normalized_by_bucket[bucket] = normalized
        except ValueError as error:
            if str(error) == "COINBASE_PUBLIC_DUPLICATE_CONTENT_CONFLICT":
                raise
            rejected += 1
        except (ArithmeticError, TypeError):
            rejected += 1

    expected = set(expected_bucket_starts(partition.start, partition.end))
    missing = tuple(sorted(expected - set(accepted)))
    return (
        tuple(accepted[key] for key in sorted(accepted)),
        missing,
        rejected,
        duplicates,
    )


class CoinbasePublicCandleSource:
    evidence_class = EvidenceClass.HISTORICAL_PUBLIC
    network_access = True

    def capture_partition(
        self,
        *,
        capture: CryptoHistoricalCapture,
        partition: CryptoCapturePartition,
        requested_at: datetime,
    ) -> CapturedCoinbasePartition:
        if (
            capture.product_id != COINBASE_PRODUCT
            or capture.granularity != COINBASE_GRANULARITY
            or partition.expected_buckets > MAX_BUCKETS_PER_REQUEST
        ):
            raise ValueError("COINBASE_PUBLIC_REQUEST_SCOPE_INVALID")
        locator = _locator(partition)
        path = locator.removeprefix(f"https://{COINBASE_PUBLIC_HOST}")
        started = time.monotonic()
        response_status: int | None = None
        raw = b""
        try:
            connection = http.client.HTTPSConnection(
                COINBASE_PUBLIC_HOST,
                timeout=MAX_REQUEST_TIMEOUT_SECONDS,
                context=ssl.create_default_context(),
            )
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Apex/crypto-historical-research-v1",
                },
            )
            response = connection.getresponse()
            response_status = response.status
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            connection.close()
            completed_at = _utc_now()
            runtime = _runtime_ms(started)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("COINBASE_PUBLIC_RESPONSE_BYTES_EXCEEDED")
            if response_status != 200:
                raise ValueError(f"COINBASE_PUBLIC_HTTP_{response_status}")
            candles, missing, rejected, duplicates = _parse_candles(raw, partition=partition)
            return CapturedCoinbasePartition(
                requested_at=requested_at,
                completed_at=completed_at,
                response_status=response_status,
                raw_payload=raw,
                candles=candles,
                missing_bucket_starts=missing,
                rejected_records=rejected,
                duplicate_records=duplicates,
                runtime_milliseconds=runtime,
                source_locator=locator,
            )
        except (OSError, TimeoutError, http.client.HTTPException, ValueError) as error:
            raise CryptoSourceFailure(
                str(error),
                capability_unavailable=isinstance(
                    error, (OSError, TimeoutError, http.client.HTTPException)
                ),
                requested_at=requested_at,
                completed_at=_utc_now(),
                runtime_milliseconds=_runtime_ms(started),
                source_locator=locator,
                response_status=response_status,
                raw_payload=raw or None,
            ) from error


class FixtureCoinbaseCandleSource:
    """Provider-shaped deterministic evidence that is always classified FIXTURE."""

    evidence_class = EvidenceClass.FIXTURE
    network_access = False

    def capture_partition(
        self,
        *,
        capture: CryptoHistoricalCapture,
        partition: CryptoCapturePartition,
        requested_at: datetime,
    ) -> CapturedCoinbasePartition:
        if capture.product_id != COINBASE_PRODUCT:
            raise ValueError("COINBASE_FIXTURE_PRODUCT_INVALID")
        rows: list[dict[str, str]] = []
        for bucket in expected_bucket_starts(partition.start, partition.end):
            ordinal = (bucket - datetime(2024, 1, 1, tzinfo=UTC)).days
            cycle = Decimal((ordinal % 180) - 90)
            trend = Decimal(ordinal) * Decimal("7.5")
            close = Decimal("36000") + trend + cycle * Decimal("42")
            open_price = close - Decimal((ordinal % 11) - 5) * Decimal("12")
            high = max(open_price, close) + Decimal("180")
            low = min(open_price, close) - Decimal("160")
            volume = Decimal("1000") + Decimal(ordinal % 200)
            rows.append(
                {
                    "start": str(int(bucket.timestamp())),
                    "low": canonical_decimal(low),
                    "high": canonical_decimal(high),
                    "open": canonical_decimal(open_price),
                    "close": canonical_decimal(close),
                    "volume": canonical_decimal(volume),
                }
            )
        raw = json.dumps(
            {"candles": list(reversed(rows))},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        candles, missing, rejected, duplicates = _parse_candles(raw, partition=partition)
        completed = requested_at
        return CapturedCoinbasePartition(
            requested_at=requested_at,
            completed_at=completed,
            response_status=200,
            raw_payload=raw,
            candles=candles,
            missing_bucket_starts=missing,
            rejected_records=rejected,
            duplicate_records=duplicates,
            runtime_milliseconds=0,
            source_locator=(
                "fixture://coinbase-public-candles/"
                f"{partition.ordinal}/{hashlib.sha256(raw).hexdigest()}"
            ),
        )


__all__ = [
    "COINBASE_DECLARED_LIMITATIONS",
    "COINBASE_PUBLIC_HOST",
    "CoinbasePublicCandleSource",
    "FixtureCoinbaseCandleSource",
    "_parse_candles",
]
