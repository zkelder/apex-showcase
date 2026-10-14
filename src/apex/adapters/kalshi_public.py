"""Narrow HTTPS-only adapter for Kalshi's unauthenticated public market feed."""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Protocol, cast
from urllib.parse import quote, urlencode, urlsplit

from apex.application.discover_public_events import CapturedPublicEventPage
from apex.application.discover_public_markets import (
    CapturedPublicMarketPage,
)
from apex.application.discover_public_markets import (
    PublicMarketDataError as KalshiPublicMarketDataError,
)
from apex.application.discover_public_markets import (
    PublicMarketHttpError as KalshiPublicMarketHttpError,
)
from apex.application.discover_public_markets import (
    PublicMarketResponseError as KalshiPublicMarketResponseError,
)
from apex.application.discover_public_markets import (
    PublicMarketTimeoutError as KalshiPublicMarketTimeoutError,
)
from apex.application.scan_public_binary_complements import CapturedKalshiPublicResponse
from apex.domain.public_event import (
    EventConsistencyReasonCode,
    EventEvidenceState,
    EventNestedMarketObservation,
    EventSettlementSource,
    NormalizedPublicEventSnapshot,
    PublicEventDataQualityState,
)
from apex.domain.public_market import (
    NormalizedPublicMarketSnapshot,
    PublicMarketProvider,
    PublicMarketReasonCode,
    parse_public_market_timestamp,
)
from apex.domain.values import ContentHash, ValueValidationError, deterministic_id, parse_decimal

KALSHI_PUBLIC_HOST = "external-api.kalshi.com"
KALSHI_MARKETS_PATH = "/trade-api/v2/markets"
KALSHI_EVENTS_PATH = "/trade-api/v2/events"
KALSHI_MARKET_ORDERBOOK_PATH = "/trade-api/v2/markets"
KALSHI_SERIES_PATH = "/trade-api/v2/series"
KALSHI_MARKETS_ENDPOINT_IDENTITY = "KALSHI_GET_MARKETS"
KALSHI_MARKET_ENDPOINT_IDENTITY = "KALSHI_GET_MARKET"
KALSHI_EVENTS_ENDPOINT_IDENTITY = "KALSHI_GET_EVENTS"
KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY = "KALSHI_GET_MARKET_ORDERBOOK"
KALSHI_SERIES_ENDPOINT_IDENTITY = "KALSHI_GET_SERIES"
KALSHI_SERIES_FEE_CHANGES_ENDPOINT_IDENTITY = "KALSHI_GET_SERIES_FEE_CHANGES"
KALSHI_PUBLIC_USER_AGENT = "Apex/0.1 public-market-research (read-only)"
KALSHI_MAX_RESPONSE_BYTES = 1_000_000
_CANONICAL_MARKET_STATUS = {
    # GET /markets filters use this vocabulary, while current REST payloads
    # expose the lifecycle states documented by the provider.
    "initialized": "unopened",
    "active": "open",
    "inactive": "paused",
    "closed": "closed",
    "determined": "closed",
    "disputed": "closed",
    "amended": "closed",
    "finalized": "settled",
    # Preserve compatibility with the API's filter vocabulary and older frozen
    # evidence while normalizing all accepted values to one internal form.
    "unopened": "unopened",
    "open": "open",
    "paused": "paused",
    "settled": "settled",
}
_SUPPORTED_EVENT_STATES = frozenset(("unopened", "open", "closed", "settled"))


class KalshiPublicMarketRedirectError(KalshiPublicMarketHttpError):
    """Redirects are rejected rather than followed to another endpoint or host."""


@dataclass(frozen=True, slots=True)
class KalshiHttpResponse:
    status: int
    content_type: str | None
    body: bytes


class KalshiHttpsTransport(Protocol):
    def get(
        self,
        *,
        host: str,
        path: str,
        headers: Mapping[str, str],
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
        total_timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> KalshiHttpResponse: ...


class StdlibKalshiHttpsTransport:
    """Small standard-library transport with explicit limits and no redirect handling."""

    def get(
        self,
        *,
        host: str,
        path: str,
        headers: Mapping[str, str],
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
        total_timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> KalshiHttpResponse:
        parsed = urlsplit(path)
        segments = tuple(segment for segment in parsed.path.split("/") if segment)
        market_segments = tuple(segment for segment in KALSHI_MARKETS_PATH.split("/") if segment)
        event_segments = tuple(segment for segment in KALSHI_EVENTS_PATH.split("/") if segment)
        series_segments = tuple(segment for segment in KALSHI_SERIES_PATH.split("/") if segment)
        allowed_path = (
            parsed.scheme == ""
            and parsed.netloc == ""
            and parsed.fragment == ""
            and (
                (segments == market_segments and bool(parsed.query))
                or (segments == event_segments and bool(parsed.query))
                or (
                    len(segments) == len(event_segments) + 1
                    and segments[: len(event_segments)] == event_segments
                    and parsed.query == "with_nested_markets=true"
                )
                or (
                    len(segments) == len(market_segments) + 2
                    and segments[: len(market_segments)] == market_segments
                    and segments[-1] == "orderbook"
                    and parsed.query == ""
                )
                or (
                    len(segments) == len(market_segments) + 1
                    and segments[: len(market_segments)] == market_segments
                    and parsed.query == ""
                )
                or (
                    len(segments) == len(series_segments) + 1
                    and segments[: len(series_segments)] == series_segments
                    and segments[-1] != "fee_changes"
                    and parsed.query == ""
                )
                or (segments == (*series_segments, "fee_changes") and bool(parsed.query))
            )
        )
        if host != KALSHI_PUBLIC_HOST or not allowed_path:
            raise KalshiPublicMarketDataError("public transport endpoint is not allowlisted")
        started = time.monotonic()
        connection = http.client.HTTPSConnection(host, port=443, timeout=connect_timeout_seconds)
        try:
            connection.connect()
            if connection.sock is None:
                raise KalshiPublicMarketDataError("public HTTPS connection was not established")
            connection.sock.settimeout(read_timeout_seconds)
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            content_type = response.getheader("Content-Type")
            body = response.read(maximum_response_bytes + 1)
            if len(body) > maximum_response_bytes:
                raise KalshiPublicMarketResponseError("public market response exceeded byte limit")
        except TimeoutError as error:
            raise KalshiPublicMarketTimeoutError("public market request timed out") from error
        except OSError as error:
            raise KalshiPublicMarketDataError("public market transport failed") from error
        finally:
            connection.close()
        if time.monotonic() - started > total_timeout_seconds:
            raise KalshiPublicMarketTimeoutError("public market request exceeded total timeout")
        return KalshiHttpResponse(status=response.status, content_type=content_type, body=body)


class KalshiPublicMarketDataClient:
    """GET-only client for the one allowlisted public production market endpoint."""

    def __init__(
        self,
        *,
        transport: KalshiHttpsTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        maximum_response_bytes: int = KALSHI_MAX_RESPONSE_BYTES,
    ) -> None:
        if maximum_response_bytes < 1 or maximum_response_bytes > KALSHI_MAX_RESPONSE_BYTES:
            raise ValueError("public market maximum response bytes is out of bounds")
        self._transport = transport or StdlibKalshiHttpsTransport()
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._maximum_response_bytes = maximum_response_bytes

    def fetch(
        self,
        *,
        status: str,
        page_size: int,
        cursor: str | None,
        timeout_seconds: int,
        exclude_multivariate: bool,
    ) -> CapturedPublicMarketPage:
        if status != "open" or not exclude_multivariate:
            raise ValueError("public Kalshi discovery supports open non-multivariate markets only")
        if not 1 <= page_size <= 1000 or not 1 <= timeout_seconds <= 60:
            raise ValueError("public Kalshi request bounds are invalid")
        query: dict[str, str] = {
            "status": status,
            "limit": str(page_size),
            "mve_filter": "exclude",
        }
        if cursor is not None:
            query["cursor"] = cursor
        response = self._transport.get(
            host=KALSHI_PUBLIC_HOST,
            path=f"{KALSHI_MARKETS_PATH}?{urlencode(query)}",
            headers={
                "Accept": "application/json",
                "User-Agent": KALSHI_PUBLIC_USER_AGENT,
            },
            connect_timeout_seconds=timeout_seconds,
            read_timeout_seconds=timeout_seconds,
            total_timeout_seconds=timeout_seconds,
            maximum_response_bytes=self._maximum_response_bytes,
        )
        if 300 <= response.status < 400:
            raise KalshiPublicMarketRedirectError("public market redirect was rejected")
        if response.status != 200:
            raise KalshiPublicMarketHttpError(
                f"public market endpoint returned HTTP {response.status}"
            )
        content_type = response.content_type or ""
        if content_type.lower().split(";", 1)[0].strip() != "application/json":
            raise KalshiPublicMarketResponseError("public market endpoint did not return JSON")
        if len(response.body) > self._maximum_response_bytes:
            raise KalshiPublicMarketResponseError("public market response exceeded byte limit")
        try:
            decoded = json.loads(response.body.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise KalshiPublicMarketResponseError(
                "public market response JSON was malformed"
            ) from error
        if not isinstance(decoded, dict) or not isinstance(decoded.get("markets"), list):
            raise KalshiPublicMarketResponseError("public market response shape was unexpected")
        response_cursor = decoded.get("cursor")
        if response_cursor is not None and not isinstance(response_cursor, str):
            raise KalshiPublicMarketResponseError("public market response cursor was malformed")
        return CapturedPublicMarketPage(
            endpoint_identity=KALSHI_MARKETS_ENDPOINT_IDENTITY,
            request_cursor=cursor,
            received_at=self._clock().astimezone(UTC),
            http_status=response.status,
            response_content_type=content_type,
            raw_payload=response.body,
            response_cursor=response_cursor,
            document=cast(Mapping[str, object], decoded),
        )


class KalshiPublicOrderbookDataClient:
    """GET-only extension for exact public orderbooks and series fee evidence."""

    def __init__(
        self,
        *,
        transport: KalshiHttpsTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        maximum_response_bytes: int = KALSHI_MAX_RESPONSE_BYTES,
    ) -> None:
        if maximum_response_bytes < 1 or maximum_response_bytes > KALSHI_MAX_RESPONSE_BYTES:
            raise ValueError("public orderbook maximum response bytes is out of bounds")
        self._transport = transport or StdlibKalshiHttpsTransport()
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._maximum_response_bytes = maximum_response_bytes

    def _fetch(
        self,
        *,
        path: str,
        endpoint_identity: str,
        timeout_seconds: int,
    ) -> CapturedKalshiPublicResponse:
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("public orderbook timeout is out of bounds")
        response = self._transport.get(
            host=KALSHI_PUBLIC_HOST,
            path=path,
            headers={
                "Accept": "application/json",
                "User-Agent": KALSHI_PUBLIC_USER_AGENT,
            },
            connect_timeout_seconds=timeout_seconds,
            read_timeout_seconds=timeout_seconds,
            total_timeout_seconds=timeout_seconds,
            maximum_response_bytes=self._maximum_response_bytes,
        )
        if 300 <= response.status < 400:
            raise KalshiPublicMarketRedirectError("public orderbook redirect was rejected")
        if response.status != 200:
            raise KalshiPublicMarketHttpError(
                f"public orderbook endpoint returned HTTP {response.status}"
            )
        content_type = response.content_type or ""
        if content_type.lower().split(";", 1)[0].strip() != "application/json":
            raise KalshiPublicMarketResponseError("public orderbook endpoint did not return JSON")
        if len(response.body) > self._maximum_response_bytes:
            raise KalshiPublicMarketResponseError("public orderbook response exceeded byte limit")
        try:
            decoded = json.loads(response.body.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise KalshiPublicMarketResponseError(
                "public orderbook response JSON was malformed"
            ) from error
        if not isinstance(decoded, dict):
            raise KalshiPublicMarketResponseError("public orderbook response shape was unexpected")
        return CapturedKalshiPublicResponse(
            endpoint_identity=endpoint_identity,
            received_at=self._clock().astimezone(UTC),
            http_status=response.status,
            response_content_type=content_type,
            raw_payload=response.body,
            document=cast(Mapping[str, object], decoded),
        )

    @staticmethod
    def _ticker(value: str, *, label: str) -> str:
        if not value or len(value) > 300 or any(character.isspace() for character in value):
            raise ValueError(f"{label} must be a bounded provider identifier")
        return value

    def fetch_orderbook(
        self,
        *,
        market_ticker: str,
        maximum_depth: int,
        timeout_seconds: int,
    ) -> CapturedKalshiPublicResponse:
        ticker = self._ticker(market_ticker, label="market ticker")
        if not 1 <= maximum_depth <= 100:
            raise ValueError("public orderbook depth must be between one and 100")
        # The documented single-market endpoint is unauthenticated.  Depth is
        # bounded locally after capture because this endpoint has no documented
        # depth parameter.  The authenticated batch endpoint is deliberately
        # not a capability of this public-evidence adapter.
        response = self._fetch(
            path=f"{KALSHI_MARKET_ORDERBOOK_PATH}/{quote(ticker, safe='')}/orderbook",
            endpoint_identity=KALSHI_MARKET_ORDERBOOK_ENDPOINT_IDENTITY,
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(response.document.get("orderbook_fp"), Mapping):
            raise KalshiPublicMarketResponseError("public orderbook response omitted its book")
        return response

    def fetch_market(
        self, *, market_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        """Fetch one public market snapshot for fixed rule and series lineage.

        The request remains a single unauthenticated GET to the same allowlisted
        production host.  A capture session uses this once at its start; it does
        not repeatedly crawl the market-list endpoint while observing a book.
        """

        ticker = self._ticker(market_ticker, label="market ticker")
        response = self._fetch(
            path=f"{KALSHI_MARKETS_PATH}/{quote(ticker, safe='')}",
            endpoint_identity=KALSHI_MARKET_ENDPOINT_IDENTITY,
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(response.document.get("market"), Mapping):
            raise KalshiPublicMarketResponseError("public market response omitted its market")
        return response

    def fetch_event(
        self, *, event_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        """Fetch one exact event with its nested markets for series lineage only."""

        ticker = self._ticker(event_ticker, label="event ticker")
        response = self._fetch(
            path=(f"{KALSHI_EVENTS_PATH}/{quote(ticker, safe='')}?with_nested_markets=true"),
            endpoint_identity=KALSHI_EVENTS_ENDPOINT_IDENTITY,
            timeout_seconds=timeout_seconds,
        )
        event = response.document.get("event")
        if not isinstance(event, Mapping) or not isinstance(event.get("markets"), list):
            raise KalshiPublicMarketResponseError(
                "public event response omitted nested market evidence"
            )
        return response

    def fetch_series(
        self, *, series_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        ticker = self._ticker(series_ticker, label="series ticker")
        response = self._fetch(
            path=f"{KALSHI_SERIES_PATH}/{quote(ticker, safe='')}",
            endpoint_identity=KALSHI_SERIES_ENDPOINT_IDENTITY,
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(response.document.get("series"), Mapping):
            raise KalshiPublicMarketResponseError("public series response omitted its series")
        return response

    def fetch_series_fee_changes(
        self, *, series_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse:
        ticker = self._ticker(series_ticker, label="series ticker")
        query = urlencode({"series_ticker": ticker, "show_historical": "true"})
        response = self._fetch(
            path=f"{KALSHI_SERIES_PATH}/fee_changes?{query}",
            endpoint_identity=KALSHI_SERIES_FEE_CHANGES_ENDPOINT_IDENTITY,
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(response.document.get("series_fee_change_arr"), list):
            raise KalshiPublicMarketResponseError("public series fee response omitted its changes")
        return response


def _optional_string(value: object, *, reason_codes: list[PublicMarketReasonCode]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        reason_codes.append(PublicMarketReasonCode.PROVIDER_DATA_INVALID)
        return None
    return value


def _optional_timestamp(
    value: object, *, reason_codes: list[PublicMarketReasonCode]
) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        reason_codes.append(PublicMarketReasonCode.PROVIDER_DATA_INVALID)
        return None
    try:
        return parse_public_market_timestamp(value)
    except ValueValidationError:
        reason_codes.append(PublicMarketReasonCode.PROVIDER_DATA_INVALID)
        return None


def _optional_decimal(
    value: object,
    *,
    price_reason: PublicMarketReasonCode | None,
    reason_codes: list[PublicMarketReasonCode],
) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = parse_decimal(cast(str | int | Decimal, value))
    except (TypeError, ValueValidationError):
        reason_codes.append(price_reason or PublicMarketReasonCode.PROVIDER_DATA_INVALID)
        return None
    if price_reason is not None and not Decimal(0) <= parsed <= Decimal(1):
        reason_codes.append(price_reason)
    if price_reason is None and parsed < 0:
        reason_codes.append(PublicMarketReasonCode.NEGATIVE_SIZE)
    return parsed


def _event_optional_string(
    value: object, *, reason_codes: list[EventConsistencyReasonCode]
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        reason_codes.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
        return None
    return value


def _event_optional_timestamp(
    value: object, *, reason_codes: list[EventConsistencyReasonCode]
) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        reason_codes.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
        return None
    try:
        return parse_public_market_timestamp(value)
    except ValueValidationError:
        reason_codes.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
        return None


class KalshiPublicMarketNormalizer:
    """Map documented Kalshi market JSON into provider-neutral immutable snapshots."""

    def normalize(
        self,
        *,
        raw_response_id: str,
        page: CapturedPublicMarketPage,
    ) -> tuple[NormalizedPublicMarketSnapshot, ...]:
        markets = page.document.get("markets")
        if not isinstance(markets, list):
            raise KalshiPublicMarketResponseError("public market page omitted its market list")
        return tuple(
            self._normalize_market(
                raw_response_id=raw_response_id,
                observed_at=page.received_at,
                ordinal=ordinal,
                raw_market=raw_market,
            )
            for ordinal, raw_market in enumerate(markets)
        )

    @staticmethod
    def _normalize_market(
        *,
        raw_response_id: str,
        observed_at: datetime,
        ordinal: int,
        raw_market: object,
    ) -> NormalizedPublicMarketSnapshot:
        reasons: list[PublicMarketReasonCode] = []
        market = raw_market if isinstance(raw_market, Mapping) else {}
        if not isinstance(raw_market, Mapping):
            reasons.append(PublicMarketReasonCode.PROVIDER_DATA_INVALID)
        ticker = _optional_string(market.get("ticker"), reason_codes=reasons)
        if not ticker:
            reasons.append(PublicMarketReasonCode.PROVIDER_DATA_INVALID)
        provider_status = _optional_string(market.get("status"), reason_codes=reasons)
        status = _CANONICAL_MARKET_STATUS.get(provider_status) if provider_status else None
        if provider_status is not None and status is None:
            reasons.append(PublicMarketReasonCode.PROVIDER_DATA_INVALID)
        rules_primary = _optional_string(market.get("rules_primary"), reason_codes=reasons)
        rules_secondary = _optional_string(market.get("rules_secondary"), reason_codes=reasons)
        payload = {
            "raw_response_id": raw_response_id,
            "provider_payload_ordinal": ordinal,
            "ticker": ticker,
            "event_ticker": _optional_string(market.get("event_ticker"), reason_codes=reasons),
            "title": _optional_string(market.get("title"), reason_codes=reasons),
            "subtitle": _optional_string(market.get("subtitle"), reason_codes=reasons),
            "status": status,
            "open_time": _optional_timestamp(market.get("open_time"), reason_codes=reasons),
            "close_time": _optional_timestamp(market.get("close_time"), reason_codes=reasons),
            "latest_expiration_time": _optional_timestamp(
                market.get("latest_expiration_time"), reason_codes=reasons
            ),
            "yes_bid": _optional_decimal(
                market.get("yes_bid_dollars"),
                price_reason=PublicMarketReasonCode.YES_PRICE_INVALID,
                reason_codes=reasons,
            ),
            "yes_ask": _optional_decimal(
                market.get("yes_ask_dollars"),
                price_reason=PublicMarketReasonCode.YES_PRICE_INVALID,
                reason_codes=reasons,
            ),
            "no_bid": _optional_decimal(
                market.get("no_bid_dollars"),
                price_reason=PublicMarketReasonCode.NO_PRICE_INVALID,
                reason_codes=reasons,
            ),
            "no_ask": _optional_decimal(
                market.get("no_ask_dollars"),
                price_reason=PublicMarketReasonCode.NO_PRICE_INVALID,
                reason_codes=reasons,
            ),
            "yes_bid_size": _optional_decimal(
                market.get("yes_bid_size_fp"), price_reason=None, reason_codes=reasons
            ),
            "yes_ask_size": _optional_decimal(
                market.get("yes_ask_size_fp"), price_reason=None, reason_codes=reasons
            ),
            "volume": _optional_decimal(
                market.get("volume_fp"), price_reason=None, reason_codes=reasons
            ),
            "volume_24h": _optional_decimal(
                market.get("volume_24h_fp"), price_reason=None, reason_codes=reasons
            ),
            "open_interest": _optional_decimal(
                market.get("open_interest_fp"), price_reason=None, reason_codes=reasons
            ),
            "liquidity": _optional_decimal(
                market.get("liquidity_dollars"), price_reason=None, reason_codes=reasons
            ),
            "rules_primary": rules_primary,
            "rules_secondary": rules_secondary,
        }
        rule_content_hash = (
            ContentHash(sha256(rules_primary.encode("utf-8")).hexdigest())
            if rules_primary
            else None
        )
        snapshot_id = deterministic_id(
            "normalized_market_snapshot",
            {"snapshot": payload, "normalization_reason_codes": tuple(reasons)},
        )
        return NormalizedPublicMarketSnapshot(
            snapshot_id=snapshot_id,
            raw_response_id=raw_response_id,
            provider=PublicMarketProvider.KALSHI,
            provider_payload_ordinal=ordinal,
            observed_at=observed_at,
            provider_market_ticker=cast(str | None, payload["ticker"]),
            event_ticker=cast(str | None, payload["event_ticker"]),
            title=cast(str | None, payload["title"]),
            subtitle=cast(str | None, payload["subtitle"]),
            status=cast(str | None, payload["status"]),
            open_time=cast(datetime | None, payload["open_time"]),
            close_time=cast(datetime | None, payload["close_time"]),
            latest_expiration_time=cast(datetime | None, payload["latest_expiration_time"]),
            yes_bid=cast(Decimal | None, payload["yes_bid"]),
            yes_ask=cast(Decimal | None, payload["yes_ask"]),
            no_bid=cast(Decimal | None, payload["no_bid"]),
            no_ask=cast(Decimal | None, payload["no_ask"]),
            yes_bid_size=cast(Decimal | None, payload["yes_bid_size"]),
            yes_ask_size=cast(Decimal | None, payload["yes_ask_size"]),
            volume=cast(Decimal | None, payload["volume"]),
            volume_24h=cast(Decimal | None, payload["volume_24h"]),
            open_interest=cast(Decimal | None, payload["open_interest"]),
            liquidity=cast(Decimal | None, payload["liquidity"]),
            rules_primary=rules_primary,
            rules_secondary=rules_secondary,
            rule_content_hash=rule_content_hash,
            normalization_reason_codes=tuple(reasons),
        )


class KalshiPublicEventDataClient:
    """GET-only client for the one allowlisted public production event endpoint."""

    def __init__(
        self,
        *,
        transport: KalshiHttpsTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        maximum_response_bytes: int = KALSHI_MAX_RESPONSE_BYTES,
    ) -> None:
        if maximum_response_bytes < 1 or maximum_response_bytes > KALSHI_MAX_RESPONSE_BYTES:
            raise ValueError("public event maximum response bytes is out of bounds")
        self._transport = transport or StdlibKalshiHttpsTransport()
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._maximum_response_bytes = maximum_response_bytes

    def fetch_events(
        self,
        *,
        status: str,
        page_size: int,
        cursor: str | None,
        timeout_seconds: int,
        with_nested_markets: bool,
    ) -> CapturedPublicEventPage:
        if status != "open" or not with_nested_markets:
            raise ValueError("public Kalshi discovery supports open events with nested markets")
        if not 1 <= page_size <= 200 or not 1 <= timeout_seconds <= 60:
            raise ValueError("public Kalshi event request bounds are invalid")
        query: dict[str, str] = {
            "status": status,
            "limit": str(page_size),
            "with_nested_markets": "true",
        }
        if cursor is not None:
            query["cursor"] = cursor
        response = self._transport.get(
            host=KALSHI_PUBLIC_HOST,
            path=f"{KALSHI_EVENTS_PATH}?{urlencode(query)}",
            headers={
                "Accept": "application/json",
                "User-Agent": KALSHI_PUBLIC_USER_AGENT,
            },
            connect_timeout_seconds=timeout_seconds,
            read_timeout_seconds=timeout_seconds,
            total_timeout_seconds=timeout_seconds,
            maximum_response_bytes=self._maximum_response_bytes,
        )
        if 300 <= response.status < 400:
            raise KalshiPublicMarketRedirectError("public event redirect was rejected")
        if response.status != 200:
            raise KalshiPublicMarketHttpError(
                f"public event endpoint returned HTTP {response.status}"
            )
        content_type = response.content_type or ""
        if content_type.lower().split(";", 1)[0].strip() != "application/json":
            raise KalshiPublicMarketResponseError("public event endpoint did not return JSON")
        if len(response.body) > self._maximum_response_bytes:
            raise KalshiPublicMarketResponseError("public event response exceeded byte limit")
        try:
            decoded = json.loads(response.body.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise KalshiPublicMarketResponseError(
                "public event response JSON was malformed"
            ) from error
        if not isinstance(decoded, dict) or not isinstance(decoded.get("events"), list):
            raise KalshiPublicMarketResponseError("public event response shape was unexpected")
        response_cursor = decoded.get("cursor")
        if response_cursor is not None and not isinstance(response_cursor, str):
            raise KalshiPublicMarketResponseError("public event response cursor was malformed")
        return CapturedPublicEventPage(
            endpoint_identity=KALSHI_EVENTS_ENDPOINT_IDENTITY,
            request_cursor=cursor,
            received_at=self._clock().astimezone(UTC),
            http_status=response.status,
            response_content_type=content_type,
            raw_payload=response.body,
            response_cursor=response_cursor,
            document=cast(Mapping[str, object], decoded),
        )


class KalshiPublicEventNormalizer:
    """Map documented event and nested-market JSON into immutable neutral snapshots."""

    def normalize_events(
        self,
        *,
        raw_response_id: str,
        page: CapturedPublicEventPage,
    ) -> tuple[NormalizedPublicEventSnapshot, ...]:
        events = page.document.get("events")
        if not isinstance(events, list):
            raise KalshiPublicMarketResponseError("public event page omitted its event list")
        return tuple(
            self._normalize_event(
                raw_response_id=raw_response_id,
                observed_at=page.received_at,
                ordinal=ordinal,
                raw_event=raw_event,
                page=page,
            )
            for ordinal, raw_event in enumerate(events)
        )

    @staticmethod
    def _normalize_event(
        *,
        raw_response_id: str,
        observed_at: datetime,
        ordinal: int,
        raw_event: object,
        page: CapturedPublicEventPage,
    ) -> NormalizedPublicEventSnapshot:
        reasons: list[EventConsistencyReasonCode] = []
        event = raw_event if isinstance(raw_event, Mapping) else {}
        if not isinstance(raw_event, Mapping):
            reasons.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
        ticker = _event_optional_string(event.get("event_ticker"), reason_codes=reasons)
        if not ticker:
            reasons.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
        status = _event_optional_string(event.get("status"), reason_codes=reasons)
        if status is not None and status not in _SUPPORTED_EVENT_STATES:
            reasons.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
        mutually_exclusive = event.get("mutually_exclusive")
        if not isinstance(mutually_exclusive, bool):
            reasons.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
            mutually_exclusive = None
        settlement_sources: list[EventSettlementSource] = []
        raw_sources = event.get("settlement_sources")
        if not isinstance(raw_sources, list):
            reasons.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
        else:
            for raw_source in raw_sources:
                source = raw_source if isinstance(raw_source, Mapping) else {}
                name = _event_optional_string(source.get("name"), reason_codes=reasons)
                url = _event_optional_string(source.get("url"), reason_codes=reasons)
                if name and url:
                    settlement_sources.append(EventSettlementSource(name=name, url=url))
                else:
                    reasons.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
        raw_nested = event.get("markets")
        if not isinstance(raw_nested, list):
            reasons.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
            raw_nested = []
        fixture_hashes = (
            page.fixture_nested_settlement_hashes.get(ticker, {})
            if page.fixture_nested_settlement_hashes is not None and ticker is not None
            else {}
        )
        nested: list[EventNestedMarketObservation] = []
        for nested_ordinal, raw_market in enumerate(raw_nested):
            market = raw_market if isinstance(raw_market, Mapping) else {}
            nested_reasons: list[EventConsistencyReasonCode] = []
            if not isinstance(raw_market, Mapping):
                nested_reasons.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
                reasons.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
            market_ticker = _event_optional_string(
                market.get("ticker"), reason_codes=nested_reasons
            )
            nested_event_ticker = _event_optional_string(
                market.get("event_ticker"), reason_codes=nested_reasons
            )
            if not market_ticker or nested_event_ticker != ticker:
                nested_reasons.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
            rules_primary = _event_optional_string(
                market.get("rules_primary"), reason_codes=nested_reasons
            )
            source_hash: ContentHash | None = None
            fixture_hash = fixture_hashes.get(market_ticker) if market_ticker else None
            if fixture_hash is not None:
                try:
                    source_hash = ContentHash(fixture_hash)
                except ValueValidationError:
                    nested_reasons.append(EventConsistencyReasonCode.PROVIDER_DATA_INVALID)
            if nested_reasons:
                reasons.extend(nested_reasons)
            nested.append(
                EventNestedMarketObservation(
                    provider_market_ticker=market_ticker,
                    provider_event_ticker=nested_event_ticker,
                    ordinal=nested_ordinal,
                    rule_content_hash=(
                        ContentHash(sha256(rules_primary.encode("utf-8")).hexdigest())
                        if rules_primary
                        else None
                    ),
                    close_time=_event_optional_timestamp(
                        market.get("close_time"), reason_codes=reasons
                    ),
                    settlement_source_hash=source_hash,
                    reason_codes=tuple(nested_reasons),
                )
            )
        exhaustive = (
            page.fixture_exhaustiveness.get(ticker, EventEvidenceState.UNKNOWN)
            if page.fixture_exhaustiveness is not None and ticker is not None
            else EventEvidenceState.UNKNOWN
        )
        payload = {
            "raw_response_id": raw_response_id,
            "provider_payload_ordinal": ordinal,
            "event_ticker": ticker,
            "series_ticker": _event_optional_string(
                event.get("series_ticker"), reason_codes=reasons
            ),
            "title": _event_optional_string(event.get("title"), reason_codes=reasons),
            "subtitle": _event_optional_string(event.get("sub_title"), reason_codes=reasons),
            "category": _event_optional_string(event.get("category"), reason_codes=reasons),
            "mutually_exclusive": mutually_exclusive,
            "settlement_sources": tuple(settlement_sources),
            "strike_date": _event_optional_timestamp(
                event.get("strike_date"), reason_codes=reasons
            ),
            "strike_period": _event_optional_string(
                event.get("strike_period"), reason_codes=reasons
            ),
            "exhaustive_evidence": exhaustive,
            "nested_markets": tuple(nested),
        }
        canonical_reasons = tuple(sorted(set(reasons), key=lambda value: value.value))
        snapshot_id = deterministic_id(
            "normalized_event_snapshot",
            {"snapshot": payload, "normalization_reason_codes": canonical_reasons},
        )
        return NormalizedPublicEventSnapshot(
            snapshot_id=snapshot_id,
            raw_response_id=raw_response_id,
            provider=PublicMarketProvider.KALSHI,
            provider_payload_ordinal=ordinal,
            observed_at=observed_at,
            provider_event_ticker=cast(str | None, payload["event_ticker"]),
            series_ticker=cast(str | None, payload["series_ticker"]),
            title=cast(str | None, payload["title"]),
            subtitle=cast(str | None, payload["subtitle"]),
            category=cast(str | None, payload["category"]),
            mutually_exclusive=cast(bool | None, payload["mutually_exclusive"]),
            settlement_sources=tuple(settlement_sources),
            strike_date=cast(datetime | None, payload["strike_date"]),
            strike_period=cast(str | None, payload["strike_period"]),
            exhaustive_evidence=exhaustive,
            nested_markets=tuple(nested),
            data_quality_state=(
                PublicEventDataQualityState.INVALID
                if canonical_reasons
                else PublicEventDataQualityState.ELIGIBLE
            ),
            normalization_reason_codes=canonical_reasons,
        )
