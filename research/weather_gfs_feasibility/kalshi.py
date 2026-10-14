"""Read-only, minimal Kalshi evidence retrieval for this isolated research spike."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from research.weather_gfs_feasibility.core import FeasibilityError, parse_utc, sha256_hex

KALSHI_HOST = "external-api.kalshi.com"
_RULE_DATE = re.compile(
    r"highest temperature recorded in Los Angeles Airport, CA for "
    r"(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4}) as reported by the "
    r"National Weather Service's Climatological Report \(Daily\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MarketEvidence:
    ticker: str
    event_ticker: str
    contract_date: date
    close_time: str
    rule: str
    rule_sha256: str
    source_response_sha256: str


@dataclass(frozen=True)
class HistoricalMarket:
    """One retained historical-market record, before research-specific filtering."""

    ticker: str
    event_ticker: str
    series_ticker: str
    contract_date: date
    close_time: str
    rule: str
    rule_sha256: str
    result: str | None
    status: str | None
    source_response_sha256: str


@dataclass(frozen=True)
class HistoricalCandle:
    ticker: str
    end_time: datetime
    probability: str
    volume: str | None
    source_response_sha256: str
    price_field: str = "price.close"


@dataclass(frozen=True)
class SeriesResolution:
    requested_series: str
    selected_series: str | None
    fallback_reason: str | None
    source_response_hashes: tuple[str, ...]


class KalshiPublicEvidence:
    """Unauthenticated public metadata access with one bounded transient retry."""

    def __init__(self, *, timeout_seconds: int, cache_directory: Path | None = None) -> None:
        if timeout_seconds <= 0:
            raise FeasibilityError("timeout seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._cache_directory = cache_directory

    def _get(self, path: str, query: dict[str, str] | None = None) -> tuple[dict[str, Any], str]:
        if not path.startswith("/"):
            raise FeasibilityError("Kalshi path must be absolute")
        suffix = f"?{urlencode(query)}" if query else ""
        url = f"https://{KALSHI_HOST}{path}{suffix}"
        cached = self._cached_json(url)
        if cached is not None:
            return cached
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                request = Request(
                    url,
                    headers={"User-Agent": "apex-gfs-feasibility/0.1 (research-only)"},
                )
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    body = response.read(10_000_000)
                    if response.read(1):
                        raise FeasibilityError("Kalshi response exceeds the bounded metadata size")
                parsed = json.loads(body)
                if not isinstance(parsed, dict):
                    raise FeasibilityError("Kalshi response must be a JSON object")
                source_hash = sha256_hex(body)
                self._store_json(url=url, body=body, source_hash=source_hash)
                return parsed, source_hash
            except HTTPError as error:
                if error.code >= 500 and attempt == 0:
                    last_error = error
                    continue
                raise FeasibilityError(f"Kalshi public API HTTP {error.code} for {path}") from error
            except (TimeoutError, URLError) as error:
                if attempt == 0:
                    last_error = error
                    continue
                raise FeasibilityError(f"Kalshi public API network failure for {path}") from error
        raise FeasibilityError(
            "Kalshi public request failed after one bounded retry"
        ) from last_error

    def _cached_json(self, url: str) -> tuple[dict[str, Any], str] | None:
        if self._cache_directory is None:
            return None
        digest = sha256_hex(url)
        body_path = self._cache_directory / "kalshi" / f"{digest}.json"
        metadata_path = self._cache_directory / "kalshi" / f"{digest}.metadata.json"
        if not body_path.is_file() or not metadata_path.is_file():
            return None
        try:
            body = body_path.read_bytes()
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            parsed = json.loads(body)
        except (OSError, json.JSONDecodeError):
            return None
        if (
            not isinstance(metadata, dict)
            or metadata.get("url") != url
            or metadata.get("sha256") != sha256_hex(body)
            or not isinstance(parsed, dict)
        ):
            return None
        return parsed, sha256_hex(body)

    def _store_json(self, *, url: str, body: bytes, source_hash: str) -> None:
        if self._cache_directory is None:
            return
        digest = sha256_hex(url)
        directory = self._cache_directory / "kalshi"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{digest}.json").write_bytes(body)
        (directory / f"{digest}.metadata.json").write_text(
            json.dumps(
                {"sha256": source_hash, "url": url},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    def verify_contract_terms(self, configuration: dict[str, object]) -> str:
        """Verify the reviewed immutable term document before a bulk archive run."""

        contract_terms = configuration.get("contract_terms")
        if not isinstance(contract_terms, dict):
            raise FeasibilityError("binding configuration is missing contract terms")
        url = contract_terms.get("url")
        expected_hash = contract_terms.get("sha256")
        if not isinstance(url, str) or not isinstance(expected_hash, str):
            raise FeasibilityError("binding configuration has incomplete contract-term evidence")
        cached = self._cached_terms(url)
        if cached is None:
            request = Request(
                url, headers={"User-Agent": "apex-gfs-feasibility/0.1 (research-only)"}
            )
            try:
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    body = response.read(10_000_001)
            except (HTTPError, TimeoutError, URLError) as error:
                raise FeasibilityError("Kalshi contract terms could not be verified") from error
            if len(body) > 10_000_000:
                raise FeasibilityError("Kalshi contract terms exceed the bounded document size")
            actual_hash = sha256_hex(body)
            self._store_terms(url=url, body=body, source_hash=actual_hash)
        else:
            actual_hash = cached
        if actual_hash != expected_hash:
            raise FeasibilityError("reviewed Kalshi contract terms hash no longer matches binding")
        return actual_hash

    def _cached_terms(self, url: str) -> str | None:
        if self._cache_directory is None:
            return None
        digest = sha256_hex(f"terms|{url}")
        body_path = self._cache_directory / "terms" / f"{digest}.bin"
        metadata_path = self._cache_directory / "terms" / f"{digest}.json"
        if not body_path.is_file() or not metadata_path.is_file():
            return None
        try:
            body = body_path.read_bytes()
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        actual_hash = sha256_hex(body)
        if not isinstance(metadata, dict) or metadata.get("url") != url:
            return None
        if metadata.get("sha256") != actual_hash:
            return None
        return actual_hash

    def _store_terms(self, *, url: str, body: bytes, source_hash: str) -> None:
        if self._cache_directory is None:
            return
        digest = sha256_hex(f"terms|{url}")
        directory = self._cache_directory / "terms"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{digest}.bin").write_bytes(body)
        (directory / f"{digest}.json").write_text(
            json.dumps({"sha256": source_hash, "url": url}, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )

    def series(self, ticker: str) -> tuple[dict[str, Any], str]:
        if not re.fullmatch(r"[A-Z0-9]+", ticker):
            raise FeasibilityError("series ticker must be uppercase alphanumeric")
        response, source_hash = self._get(f"/trade-api/v2/series/{ticker}")
        series = response.get("series")
        if not isinstance(series, dict):
            raise FeasibilityError("Kalshi series response is missing series metadata")
        return series, source_hash

    def resolve_daily_high_binding(
        self, *, requested_series: str, configuration: dict[str, object]
    ) -> SeriesResolution:
        requested, requested_hash = self.series(requested_series)
        expected_series = str(configuration["selected_series"])
        contract_terms = configuration.get("contract_terms")
        if not isinstance(contract_terms, dict):
            raise FeasibilityError("binding configuration is missing contract terms")
        expected_terms = contract_terms.get("url")
        if not isinstance(expected_terms, str):
            raise FeasibilityError("binding configuration contract terms have no URL")
        if requested_series == expected_series:
            if (
                requested.get("frequency") != "daily"
                or requested.get("contract_terms_url") != expected_terms
            ):
                return SeriesResolution(
                    requested_series,
                    None,
                    "configured daily-high series no longer matches",
                    (requested_hash,),
                )
            return SeriesResolution(requested_series, expected_series, None, (requested_hash,))
        fallback_reason: str | None = None
        if requested.get("frequency") != "daily":
            fallback_reason = (
                "requested series is not daily and cannot support daily-maximum reconstruction"
            )
        elif "high" not in str(requested.get("title", "")).lower():
            fallback_reason = "requested series is not an explicit daily-high contract"
        else:
            fallback_reason = "requested series is not the reviewed daily-high binding"
        selected, selected_hash = self.series(expected_series)
        if (
            selected.get("frequency") != "daily"
            or selected.get("contract_terms_url") != expected_terms
        ):
            return SeriesResolution(
                requested_series,
                None,
                "configured daily-high fallback no longer matches its reviewed contract terms",
                (requested_hash, selected_hash),
            )
        return SeriesResolution(
            requested_series,
            expected_series,
            fallback_reason,
            (requested_hash, selected_hash),
        )

    def historical_daily_high_events(
        self, *, series_ticker: str, maximum_pages: int = 16
    ) -> tuple[MarketEvidence, ...]:
        if maximum_pages <= 0:
            raise FeasibilityError("maximum history pages must be positive")
        cursor: str | None = None
        events: dict[str, MarketEvidence] = {}
        for _ in range(maximum_pages):
            query = {"series_ticker": series_ticker, "limit": "1000"}
            if cursor:
                query["cursor"] = cursor
            response, source_hash = self._get("/trade-api/v2/historical/markets", query)
            markets = response.get("markets")
            if not isinstance(markets, list):
                raise FeasibilityError("Kalshi historical markets response has no market list")
            for raw_market in markets:
                if not isinstance(raw_market, dict):
                    continue
                market = _daily_high_market(raw_market, source_hash)
                if market is None:
                    continue
                existing = events.get(market.event_ticker)
                if existing is None or market.ticker < existing.ticker:
                    events[market.event_ticker] = market
            next_cursor = response.get("cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return tuple(
            sorted(events.values(), key=lambda item: (item.contract_date, item.event_ticker))
        )

    def historical_daily_high_markets(
        self,
        *,
        series_ticker: str,
        start_date: date,
        end_date: date,
        maximum_pages: int = 16,
    ) -> tuple[HistoricalMarket, ...]:
        if maximum_pages <= 0:
            raise FeasibilityError("maximum history pages must be positive")
        if start_date > end_date:
            raise FeasibilityError("historical date range is invalid")
        cursor: str | None = None
        retained: dict[str, HistoricalMarket] = {}
        for _ in range(maximum_pages):
            query = {"series_ticker": series_ticker, "limit": "1000"}
            if cursor:
                query["cursor"] = cursor
            response, source_hash = self._get("/trade-api/v2/historical/markets", query)
            markets = response.get("markets")
            if not isinstance(markets, list):
                raise FeasibilityError("Kalshi historical markets response has no market list")
            for raw_market in markets:
                if not isinstance(raw_market, dict):
                    continue
                market = _historical_daily_high_market(
                    raw_market,
                    source_hash,
                    requested_series_ticker=series_ticker,
                )
                if market is None or not start_date <= market.contract_date <= end_date:
                    continue
                existing = retained.get(market.ticker)
                if existing is not None and existing != market:
                    raise FeasibilityError(
                        "duplicate historical market identity has conflicting evidence"
                    )
                retained[market.ticker] = market
            next_cursor = response.get("cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return tuple(sorted(retained.values(), key=lambda item: (item.contract_date, item.ticker)))

    def historical_candles(
        self,
        *,
        ticker: str,
        start_time: datetime,
        end_time: datetime,
    ) -> tuple[HistoricalCandle, ...]:
        if start_time.tzinfo is None or end_time.tzinfo is None or start_time >= end_time:
            raise FeasibilityError("historical candle range must be increasing and timezone-aware")
        query = {
            "start_ts": str(int(start_time.astimezone(UTC).timestamp())),
            "end_ts": str(int(end_time.astimezone(UTC).timestamp())),
            "period_interval": "60",
        }
        response, source_hash = self._get(
            f"/trade-api/v2/historical/markets/{ticker}/candlesticks", query
        )
        raw_candles = response.get("candlesticks")
        if not isinstance(raw_candles, list):
            raise FeasibilityError("Kalshi historical candles response has no candle list")
        candles: list[HistoricalCandle] = []
        for raw_candle in raw_candles:
            if not isinstance(raw_candle, dict):
                continue
            candle = _historical_candle(ticker, raw_candle, source_hash)
            if candle is not None:
                candles.append(candle)
        return tuple(sorted(candles, key=lambda item: item.end_time))


def _daily_high_market(raw_market: dict[str, Any], source_hash: str) -> MarketEvidence | None:
    ticker = raw_market.get("ticker")
    event_ticker = raw_market.get("event_ticker")
    close_time = raw_market.get("close_time")
    rule = raw_market.get("rules_primary")
    if not isinstance(ticker, str) or not ticker:
        return None
    if not isinstance(event_ticker, str) or not event_ticker:
        return None
    if not isinstance(close_time, str) or not close_time:
        return None
    if not isinstance(rule, str) or not rule:
        return None
    matches = tuple(_RULE_DATE.finditer(rule))
    if len(matches) != 1:
        return None
    match = matches[0]
    try:
        contract_date = datetime.strptime(match.group("date"), "%B %d, %Y").date()
        parse_utc(close_time)
    except ValueError:
        return None
    return MarketEvidence(
        ticker=ticker,
        event_ticker=event_ticker,
        contract_date=contract_date,
        close_time=close_time,
        rule=rule,
        rule_sha256=sha256(rule.encode("utf-8")).hexdigest(),
        source_response_sha256=source_hash,
    )


def _historical_daily_high_market(
    raw_market: dict[str, Any], source_hash: str, *, requested_series_ticker: str
) -> HistoricalMarket | None:
    basic = _daily_high_market(raw_market, source_hash)
    if basic is None:
        return None
    if not basic.ticker.startswith(basic.event_ticker + "-"):
        return None
    raw_series_ticker = raw_market.get("series_ticker")
    if raw_series_ticker is None:
        series_ticker = requested_series_ticker
    elif isinstance(raw_series_ticker, str) and raw_series_ticker:
        if raw_series_ticker != requested_series_ticker:
            raise FeasibilityError(
                "historical market response conflicts with requested series lineage"
            )
        series_ticker = raw_series_ticker
    else:
        return None
    result = raw_market.get("result")
    status = raw_market.get("status")
    return HistoricalMarket(
        ticker=basic.ticker,
        event_ticker=basic.event_ticker,
        series_ticker=series_ticker,
        contract_date=basic.contract_date,
        close_time=basic.close_time,
        rule=basic.rule,
        rule_sha256=basic.rule_sha256,
        result=result.lower() if isinstance(result, str) else None,
        status=status.lower() if isinstance(status, str) else None,
        source_response_sha256=source_hash,
    )


def _historical_candle(
    ticker: str, raw_candle: dict[str, Any], source_hash: str
) -> HistoricalCandle | None:
    raw_end = raw_candle.get("end_period_ts")
    if not isinstance(raw_end, (str, int)) or isinstance(raw_end, bool):
        return None
    try:
        end_time = datetime.fromtimestamp(int(raw_end), tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None
    price = raw_candle.get("price")
    if not isinstance(price, dict):
        return None
    direct = _direct_close_probability(price)
    if direct is None:
        direct = _two_sided_quote_midpoint(raw_candle)
    if direct is None:
        return None
    probability, price_field = direct
    raw_volume = raw_candle.get("volume_fp", raw_candle.get("volume"))
    volume = (
        str(raw_volume)
        if isinstance(raw_volume, (str, int)) and not isinstance(raw_volume, bool)
        else None
    )
    return HistoricalCandle(
        ticker=ticker,
        end_time=end_time,
        probability=probability,
        volume=volume,
        source_response_sha256=source_hash,
        price_field=price_field,
    )


def _direct_close_probability(price: dict[str, Any]) -> tuple[str, str] | None:
    raw_dollars = price.get("close_dollars")
    if isinstance(raw_dollars, (str, int)) and not isinstance(raw_dollars, bool):
        try:
            return _fixed_point_probability(
                str(raw_dollars), cents_when_whole=False
            ), "price.close_dollars"
        except FeasibilityError:
            return None
    raw_close = price.get("close")
    if isinstance(raw_close, (str, int)) and not isinstance(raw_close, bool):
        try:
            return _fixed_point_probability(str(raw_close), cents_when_whole=True), "price.close"
        except FeasibilityError:
            return None
    return None


def _two_sided_quote_midpoint(price: dict[str, Any]) -> tuple[str, str] | None:
    bid = price.get("yes_bid")
    ask = price.get("yes_ask")
    if not isinstance(bid, dict) or not isinstance(ask, dict):
        return None
    raw_bid, raw_ask = bid.get("close"), ask.get("close")
    if (
        not isinstance(raw_bid, (str, int))
        or isinstance(raw_bid, bool)
        or not isinstance(raw_ask, (str, int))
        or isinstance(raw_ask, bool)
    ):
        return None
    try:
        bid_value = Decimal(_fixed_point_probability(str(raw_bid), cents_when_whole=False))
        ask_value = Decimal(_fixed_point_probability(str(raw_ask), cents_when_whole=False))
    except FeasibilityError:
        return None
    if bid_value > ask_value:
        return None
    return format(
        (bid_value + ask_value) / Decimal("2"), "f"
    ), "midpoint(yes_bid.close,yes_ask.close)"


def _fixed_point_probability(raw: str, *, cents_when_whole: bool) -> str:
    if not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", raw):
        raise FeasibilityError("historical candle price is not a fixed-point decimal")
    value = Decimal(raw)
    if cents_when_whole and "." not in raw:
        value /= Decimal("100")
    if not Decimal("0") <= value <= Decimal("1"):
        raise FeasibilityError("historical candle price is outside the binary probability range")
    return format(value, "f")


def select_spanning_events(
    events: tuple[MarketEvidence, ...], count: int
) -> tuple[MarketEvidence, ...]:
    if count <= 0:
        raise FeasibilityError("sample date count must be positive")
    by_year: dict[int, list[MarketEvidence]] = {}
    for event in events:
        by_year.setdefault(event.contract_date.year, []).append(event)
    selected: list[MarketEvidence] = []
    used_events: set[str] = set()
    for year in (2024, 2025, 2026):
        candidates = by_year.get(year, [])
        for event in _seasonally_spread(candidates, 2):
            selected.append(event)
            used_events.add(event.event_ticker)
    if len(selected) < count:
        remaining = [event for event in events if event.event_ticker not in used_events]
        for event in _seasonally_spread(remaining, count - len(selected)):
            selected.append(event)
            used_events.add(event.event_ticker)
    return tuple(sorted(selected[:count], key=lambda item: (item.contract_date, item.event_ticker)))


def _seasonally_spread(events: list[MarketEvidence], count: int) -> tuple[MarketEvidence, ...]:
    if count <= 0 or not events:
        return ()
    ordered = sorted(events, key=lambda item: (item.contract_date, item.event_ticker))
    if len(ordered) <= count:
        return tuple(ordered)
    if count == 1:
        return (ordered[len(ordered) // 2],)
    indexes = [round(index * (len(ordered) - 1) / (count - 1)) for index in range(count)]
    return tuple(ordered[index] for index in indexes)
