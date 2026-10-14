"""Bounded unauthenticated Coinbase product and daily-observation adapter."""

from __future__ import annotations

import http.client
import json
import ssl
import time
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, cast
from urllib.parse import urlencode

from apex.application.crypto_universe import (
    CapturedObservationPage,
    CapturedProductBook,
    CapturedProductInput,
    CapturedProductPage,
)
from apex.domain.crypto_universe import CryptoDailyObservation
from apex.domain.research_evidence import EvidenceClass
from apex.domain.values import canonical_decimal, parse_decimal

COINBASE_PUBLIC_HOST = "api.coinbase.com"
PRODUCTS_PATH = "/api/v3/brokerage/market/products"
PRODUCT_BOOK_PATH = "/api/v3/brokerage/market/product_book"
CANDLES_PREFIX = "/api/v3/brokerage/market/products/"
MAX_RESPONSE_BYTES = 2_000_000
MAX_TIMEOUT_SECONDS = 15
MAX_CANDLES_PER_REQUEST = 300
MAX_PRODUCT_BOOK_DEPTH = 50
PRODUCT_SORT_ORDER = "PRODUCTS_SORT_ORDER_VOLUME_24H_DESCENDING"
BOOK_DECIMAL_QUANTUM = Decimal("0.00000001")


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _runtime(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _read(path: str) -> tuple[bytes, datetime, int]:
    started = time.monotonic()
    connection = http.client.HTTPSConnection(
        COINBASE_PUBLIC_HOST,
        timeout=MAX_TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/json",
                "User-Agent": "Apex/crypto-universe-research-v1",
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if response.status != 200:
            raise ValueError(f"COINBASE_PUBLIC_HTTP_{response.status}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("COINBASE_PUBLIC_RESPONSE_BYTES_EXCEEDED")
        return raw, _now(), _runtime(started)
    finally:
        connection.close()


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    return parse_decimal(cast(Any, value))


def _parse_product_page(
    raw: bytes,
    maximum_products: int,
) -> tuple[tuple[CapturedProductInput, ...], str | None, bool]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("COINBASE_PRODUCTS_JSON_INVALID") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        raise ValueError("COINBASE_PRODUCTS_SCHEMA_INVALID")
    values: list[CapturedProductInput] = []
    for row in payload["products"][:maximum_products]:
        if not isinstance(row, dict):
            continue
        try:
            product_id = str(row["product_id"])
            base = str(row["base_currency_id"])
            quote = str(row["quote_currency_id"])
            product_type = str(row.get("product_type", "UNKNOWN_PRODUCT_TYPE"))
            restrictions: list[str] = []
            if row.get("trading_disabled", False) is True:
                restrictions.append("TRADING_DISABLED")
            if row.get("is_disabled", False) is True:
                restrictions.append("PRODUCT_DISABLED")
            if row.get("view_only", False) is True:
                restrictions.append("VIEW_ONLY")
            if row.get("cancel_only", False) is True:
                restrictions.append("CANCEL_ONLY")
            status = str(row.get("status", "UNKNOWN"))
            if status.lower() != "online":
                restrictions.append("STATUS_NOT_ONLINE")
            quote_volume = _optional_decimal(row.get("approximate_quote_24h_volume"))
            if quote_volume is None:
                base_volume = _optional_decimal(row.get("volume_24h"))
                price = _optional_decimal(row.get("price"))
                quote_volume = (
                    base_volume * price
                    if base_volume is not None and price is not None
                    else base_volume
                )
            alias_to = row.get("alias_to", [])
            if not isinstance(alias_to, list):
                raise ValueError
            values.append(
                CapturedProductInput(
                    product_id=product_id,
                    base_asset=base,
                    quote_asset=quote,
                    market_type=product_type,
                    trading_status="ACTIVE" if not restrictions else "RESTRICTED",
                    product_status="ONLINE" if status.lower() == "online" else status.upper(),
                    price_increment=parse_decimal(row.get("price_increment") or "0"),
                    quantity_increment=parse_decimal(
                        cast(Any, row.get("base_increment", row.get("base_min_size")) or "0")
                    ),
                    minimum_quantity=_optional_decimal(row.get("base_min_size")),
                    minimum_notional=_optional_decimal(row.get("quote_min_size")),
                    public_volume_24h=quote_volume,
                    provider_alias=(None if row.get("alias") in (None, "") else str(row["alias"])),
                    provider_alias_to=tuple(sorted(str(item) for item in alias_to)),
                    restriction_reasons=tuple(sorted(set(restrictions))),
                )
            )
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    if not values:
        raise ValueError("COINBASE_PRODUCTS_NO_VALID_RECORDS")
    pagination = payload.get("pagination")
    if pagination is None:
        return tuple(values), None, False
    if not isinstance(pagination, dict) or not isinstance(pagination.get("has_next"), bool):
        raise ValueError("COINBASE_PRODUCTS_PAGINATION_SCHEMA_INVALID")
    has_next = pagination["has_next"]
    raw_next = pagination.get("next_cursor")
    next_cursor = None if raw_next in (None, "") else str(raw_next)
    if has_next != bool(next_cursor):
        raise ValueError("COINBASE_PRODUCTS_PAGINATION_INCONSISTENT")
    return tuple(values), next_cursor, has_next


def _parse_products(raw: bytes, maximum_products: int) -> tuple[CapturedProductInput, ...]:
    return _parse_product_page(raw, maximum_products)[0]


def _parse_observations(
    raw: bytes, *, interval_start: datetime, interval_end: datetime
) -> tuple[tuple[CryptoDailyObservation, ...], int, int, int]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("COINBASE_CANDLES_JSON_INVALID") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise ValueError("COINBASE_CANDLES_SCHEMA_INVALID")
    accepted: dict[datetime, CryptoDailyObservation] = {}
    duplicates = 0
    rejected = 0
    for row in payload["candles"]:
        try:
            if not isinstance(row, dict):
                raise ValueError
            timestamp = datetime.fromtimestamp(int(row["start"]), tz=UTC)
            if not interval_start <= timestamp < interval_end:
                raise ValueError
            value = CryptoDailyObservation.create(
                bucket_start=timestamp,
                close=row["close"],
                volume=row["volume"],
            )
            if timestamp in accepted:
                duplicates += 1
                if accepted[timestamp] != value:
                    raise ValueError("COINBASE_CANDLE_DUPLICATE_CONFLICT")
                continue
            accepted[timestamp] = value
        except (KeyError, TypeError, ValueError, ArithmeticError):
            rejected += 1
    expected = (interval_end - interval_start).days
    return (
        tuple(accepted[key] for key in sorted(accepted)),
        max(0, expected - len(accepted)),
        duplicates,
        rejected,
    )


def _parse_product_book(
    raw: bytes, *, product_id: str, maximum_depth: int
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    """Normalize a bounded two-sided public book using exact Decimals."""

    if not 1 <= maximum_depth <= MAX_PRODUCT_BOOK_DEPTH:
        raise ValueError("COINBASE_PRODUCT_BOOK_DEPTH_BOUND_INVALID")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("COINBASE_PRODUCT_BOOK_JSON_INVALID") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("pricebook"), dict):
        raise ValueError("COINBASE_PRODUCT_BOOK_SCHEMA_INVALID")
    pricebook = payload["pricebook"]
    if str(pricebook.get("product_id", "")) != product_id:
        raise ValueError("COINBASE_PRODUCT_BOOK_ROUTE_MISMATCH")

    def levels(side: str) -> tuple[tuple[Decimal, Decimal], ...]:
        rows = pricebook.get(side)
        if not isinstance(rows, list) or not rows:
            raise ValueError("COINBASE_PRODUCT_BOOK_SIDE_MISSING")
        normalized: list[tuple[Decimal, Decimal]] = []
        for row in rows[:maximum_depth]:
            if not isinstance(row, dict):
                raise ValueError("COINBASE_PRODUCT_BOOK_LEVEL_INVALID")
            price = parse_decimal(cast(Any, row.get("price")))
            size = parse_decimal(cast(Any, row.get("size")))
            if price <= 0 or size <= 0:
                raise ValueError("COINBASE_PRODUCT_BOOK_LEVEL_INVALID")
            normalized.append((price, size))
        return tuple(normalized)

    bids = levels("bids")
    asks = levels("asks")
    best_bid = max(price for price, _ in bids)
    best_ask = min(price for price, _ in asks)
    if best_bid >= best_ask:
        raise ValueError("COINBASE_PRODUCT_BOOK_CROSSED")
    midpoint = ((best_bid + best_ask) / Decimal(2)).quantize(
        BOOK_DECIMAL_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    spread_bps = ((best_ask - best_bid) / midpoint * Decimal(10000)).quantize(
        BOOK_DECIMAL_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    bid_depth = sum((price * size for price, size in bids), Decimal(0)).quantize(
        BOOK_DECIMAL_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    ask_depth = sum((price * size for price, size in asks), Decimal(0)).quantize(
        BOOK_DECIMAL_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    return best_bid, best_ask, midpoint, spread_bps, bid_depth, ask_depth


class CoinbasePublicUniverseSource:
    evidence_class = EvidenceClass.HISTORICAL_PUBLIC
    network_access = True

    def capture_products(
        self,
        *,
        requested_at: datetime,
        maximum_products: int,
        cursor: str | None,
    ) -> CapturedProductPage:
        if not 1 <= maximum_products <= 100:
            raise ValueError("COINBASE_PRODUCT_REQUEST_BOUND_INVALID")
        parameters: dict[str, object] = {
            "limit": maximum_products,
            "product_type": "SPOT",
            "products_sort_order": PRODUCT_SORT_ORDER,
        }
        if cursor is not None:
            parameters["cursor"] = cursor
        path = f"{PRODUCTS_PATH}?{urlencode(parameters)}"
        raw, completed, runtime = _read(path)
        products, next_cursor, has_next = _parse_product_page(raw, maximum_products)
        return CapturedProductPage(
            completed,
            raw,
            products,
            f"https://{COINBASE_PUBLIC_HOST}{path}",
            runtime,
            cursor,
            next_cursor,
            has_next,
        )

    def capture_daily_observations(
        self,
        *,
        product_id: str,
        interval_start: datetime,
        interval_end: datetime,
        requested_at: datetime,
    ) -> CapturedObservationPage:
        expected = (interval_end - interval_start).days
        if not 91 <= expected <= MAX_CANDLES_PER_REQUEST:
            raise ValueError("COINBASE_CANDLE_REQUEST_BOUND_INVALID")
        query = urlencode(
            {
                "start": int(interval_start.timestamp()) - 1,
                "end": int(interval_end.timestamp()) - 1,
                "granularity": "ONE_DAY",
                "limit": expected,
            }
        )
        path = f"{CANDLES_PREFIX}{product_id}/candles?{query}"
        raw, completed, runtime = _read(path)
        candles, missing, duplicates, rejected = _parse_observations(
            raw, interval_start=interval_start, interval_end=interval_end
        )
        return CapturedObservationPage(
            completed,
            raw,
            candles,
            missing,
            duplicates,
            rejected,
            f"https://{COINBASE_PUBLIC_HOST}{path}",
            runtime,
        )

    def capture_product_book(
        self,
        *,
        product_id: str,
        maximum_depth: int,
        requested_at: datetime,
    ) -> CapturedProductBook:
        if not 1 <= maximum_depth <= MAX_PRODUCT_BOOK_DEPTH:
            raise ValueError("COINBASE_PRODUCT_BOOK_DEPTH_BOUND_INVALID")
        query = urlencode({"product_id": product_id, "limit": maximum_depth})
        path = f"{PRODUCT_BOOK_PATH}?{query}"
        raw, completed, runtime = _read(path)
        bid, ask, midpoint, spread, bid_depth, ask_depth = _parse_product_book(
            raw, product_id=product_id, maximum_depth=maximum_depth
        )
        return CapturedProductBook(
            completed,
            raw,
            product_id,
            bid,
            ask,
            midpoint,
            spread,
            bid_depth,
            ask_depth,
            f"https://{COINBASE_PUBLIC_HOST}{path}",
            runtime,
        )


class FixtureCoinbaseUniverseSource:
    evidence_class = EvidenceClass.FIXTURE
    network_access = False
    _PAGE_SIZE = 8

    _PRODUCTS = (
        ("USDT-USDC", "USDT", "USDC", "1000000000", "SPOT"),
        ("BTC-USD", "BTC", "USD", "900000000", "SPOT"),
        ("CBBTC-USD", "CBBTC", "USD", "800000000", "SPOT"),
        ("BTC-USDC", "BTC", "USDC", "700000000", "SPOT"),
        ("BTC3L-USD", "BTC3L", "USD", "650000000", "SPOT"),
        ("ETH-USD", "ETH", "USD", "600000000", "SPOT"),
        ("ETH-USDC", "ETH", "USDC", "500000000", "SPOT"),
        ("SOL-USD", "SOL", "USD", "300000000", "SPOT"),
        ("XRP-USD", "XRP", "USD", "250000000", "SPOT"),
        ("DOGE-USD", "DOGE", "USD", "200000000", "SPOT"),
        ("ADA-USD", "ADA", "USD", "180000000", "SPOT"),
        ("AVAX-USD", "AVAX", "USD", "160000000", "SPOT"),
        ("LINK-USD", "LINK", "USD", "140000000", "SPOT"),
        ("LTC-USD", "LTC", "USD", "120000000", "SPOT"),
        ("BCH-USD", "BCH", "USD", "110000000", "SPOT"),
        ("SUI-USD", "SUI", "USD", "100000000", "SPOT"),
        ("HBAR-USD", "HBAR", "USD", "90000000", "SPOT"),
        ("DOT-USD", "DOT", "USD", "80000000", "SPOT"),
        ("UNI-USD", "UNI", "USD", "70000000", "SPOT"),
        ("AAVE-USD", "AAVE", "USD", "60000000", "SPOT"),
        ("NEAR-USD", "NEAR", "USD", "50000000", "SPOT"),
        ("APT-USD", "APT", "USD", "40000000", "SPOT"),
        ("ICP-USD", "ICP", "USD", "30000000", "SPOT"),
        ("MKR-USD", "MKR", "USD", "25000000", "SPOT"),
        ("PEPE-USD", "PEPE", "USD", "20000000", "SPOT"),
        ("PEPE-USDC", "PEPE", "USDC", "18000000", "SPOT"),
        ("PUMP-USD", "PUMP", "USD", "15000000", "SPOT"),
        ("ETH-PERP", "ETH", "USD", "12000000", "FUTURE"),
        ("BTC-EUR", "BTC", "EUR", "10000000", "SPOT"),
        ("XYZ-USD", "XYZ", "USD", "500000", "SPOT"),
    )

    def capture_products(
        self,
        *,
        requested_at: datetime,
        maximum_products: int,
        cursor: str | None,
    ) -> CapturedProductPage:
        if cursor is None:
            offset = 0
        elif cursor.startswith("fixture-cursor-"):
            offset = int(cursor.removeprefix("fixture-cursor-"))
        else:
            raise ValueError("FIXTURE_PRODUCT_CURSOR_INVALID")
        page_size = min(maximum_products, self._PAGE_SIZE)
        page_rows = self._PRODUCTS[offset : offset + page_size]
        next_offset = offset + len(page_rows)
        has_next = next_offset < len(self._PRODUCTS)
        next_cursor = f"fixture-cursor-{next_offset}" if has_next else None
        rows = []
        for product_id, base, quote, volume, product_type in page_rows:
            alias_to: list[str] = []
            if product_id == "BTC-USD":
                alias_to = ["BTC-USDC"]
            elif product_id == "ETH-USD":
                alias_to = ["ETH-USDC"]
            rows.append(
                {
                    "product_id": product_id,
                    "base_currency_id": base,
                    "quote_currency_id": quote,
                    "product_type": product_type,
                    "status": "online",
                    "trading_disabled": False,
                    "price_increment": "0.01",
                    "base_increment": "0.00000001",
                    "base_min_size": "0.0001",
                    "quote_min_size": "1",
                    "approximate_quote_24h_volume": volume,
                    "alias": product_id,
                    "alias_to": alias_to,
                }
            )
        raw = json.dumps(
            {
                "products": rows,
                "pagination": {
                    "next_cursor": next_cursor or "",
                    "has_next": has_next,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        products, parsed_next_cursor, parsed_has_next = _parse_product_page(raw, maximum_products)
        return CapturedProductPage(
            requested_at,
            raw,
            products,
            f"fixture://coinbase-public-products/v2?page={offset // page_size + 1}",
            0,
            cursor,
            parsed_next_cursor,
            parsed_has_next,
        )

    def capture_daily_observations(
        self,
        *,
        product_id: str,
        interval_start: datetime,
        interval_end: datetime,
        requested_at: datetime,
    ) -> CapturedObservationPage:
        rows = []
        product_ordinal = sum(ord(value) for value in product_id)
        base_price = Decimal(20 + product_ordinal % 200)
        day = interval_start
        ordinal = 0
        while day < interval_end:
            cycle = Decimal((ordinal % 23) - 11) / Decimal(1000)
            trend = Decimal(ordinal) * Decimal((product_ordinal % 7) - 1) / Decimal(1000)
            close = max(Decimal("0.01"), base_price + trend + base_price * cycle)
            volume = Decimal(100000 + (product_ordinal % 80) * 10000 + ordinal * 20)
            if product_id == "XYZ-USD":
                volume = Decimal(2)
            rows.append(
                {
                    "start": str(int(day.timestamp())),
                    "close": canonical_decimal(close),
                    "volume": canonical_decimal(volume),
                }
            )
            ordinal += 1
            day += timedelta(days=1)
        raw = json.dumps(
            {"candles": list(reversed(rows))}, sort_keys=True, separators=(",", ":")
        ).encode()
        candles, missing, duplicates, rejected = _parse_observations(
            raw, interval_start=interval_start, interval_end=interval_end
        )
        return CapturedObservationPage(
            requested_at,
            raw,
            candles,
            missing,
            duplicates,
            rejected,
            f"fixture://coinbase-public-candles/{product_id}/v1",
            0,
        )

    def capture_product_book(
        self,
        *,
        product_id: str,
        maximum_depth: int,
        requested_at: datetime,
    ) -> CapturedProductBook:
        if not 1 <= maximum_depth <= MAX_PRODUCT_BOOK_DEPTH:
            raise ValueError("COINBASE_PRODUCT_BOOK_DEPTH_BOUND_INVALID")
        ordinal = sum(ord(value) for value in product_id)
        midpoint = Decimal(20 + ordinal % 200)
        rows = {
            "pricebook": {
                "product_id": product_id,
                "bids": [{"price": canonical_decimal(midpoint - Decimal("0.01")), "size": "25"}],
                "asks": [{"price": canonical_decimal(midpoint + Decimal("0.01")), "size": "24"}],
            }
        }
        raw = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        bid, ask, normalized_midpoint, spread, bid_depth, ask_depth = _parse_product_book(
            raw, product_id=product_id, maximum_depth=maximum_depth
        )
        return CapturedProductBook(
            requested_at,
            raw,
            product_id,
            bid,
            ask,
            normalized_midpoint,
            spread,
            bid_depth,
            ask_depth,
            f"fixture://coinbase-public-book/{product_id}/v1",
            0,
        )


__all__ = [
    "COINBASE_PUBLIC_HOST",
    "CoinbasePublicUniverseSource",
    "FixtureCoinbaseUniverseSource",
    "_parse_observations",
    "_parse_product_book",
    "_parse_products",
]
