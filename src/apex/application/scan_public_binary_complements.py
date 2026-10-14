"""Bounded public orderbook collection and fee-aware binary complement shadows."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Protocol, cast

from apex.domain.public_complement import (
    FEE_MODEL_IDENTITY,
    MAKER_COEFFICIENT,
    ROUNDING_IDENTITY,
    SCHEDULE_EFFECTIVE_DATE,
    SCHEDULE_IDENTITY,
    TAKER_COEFFICIENT,
    BinaryComplementEvaluation,
    ComplementFillSlice,
    ComplementState,
    OrderbookSide,
    PublicOrderbookLevel,
    PublicOrderbookSnapshot,
    SeriesFeeSnapshot,
    evaluate_binary_complement,
    rank_shadow_executable_quality,
)
from apex.domain.public_market import PublicMarketProvider, parse_public_market_timestamp
from apex.domain.values import (
    ContentHash,
    ValueValidationError,
    content_digest,
    deterministic_id,
    parse_decimal,
)

COMPLEMENT_EVALUATOR_VERSION = "public-binary-complement-v1"


class PublicBinaryComplementError(RuntimeError):
    """A bounded public binary-complement scan could not complete safely."""


class PublicBinaryComplementConflictError(PublicBinaryComplementError):
    """Immutable public complement evidence was reused with changed content."""


@dataclass(frozen=True, slots=True)
class CapturedKalshiPublicResponse:
    """Exact bounded public response supplied by the narrow provider adapter."""

    endpoint_identity: str
    received_at: datetime
    http_status: int
    response_content_type: str
    raw_payload: bytes
    document: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.http_status != 200 or not self.raw_payload:
            raise ValueError("public response must retain a successful non-empty response")
        if self.response_content_type.lower().split(";", 1)[0].strip() != "application/json":
            raise ValueError("public response must be JSON")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("public response receive time must be timezone-aware")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))

    @property
    def raw_payload_sha256(self) -> str:
        return sha256(self.raw_payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ComplementMarket:
    """Exact retained market lineage selected from one completed discovery run.

    The five trailing fields are optional retained-activity evidence (already
    captured on the same normalized market snapshot every caller already
    reads) used only to rank which markets are worth an orderbook request
    when a discovery run returns more candidates than the scan's bound.  They
    default to ``None`` so every existing caller that builds a
    ``ComplementMarket`` without activity evidence -- the foreground bounded
    orderbook-capture session and the deterministic complement fixtures --
    is unaffected.  A missing value is never treated as favorable: see
    ``_activity_rank_key`` below.
    """

    market_snapshot_id: str
    ingestion_run_id: str
    provider_market_ticker: str
    provider_event_ticker: str | None
    series_ticker: str | None
    status: str | None
    observed_at: datetime
    rule_content_hash: ContentHash | None
    data_quality_state: str
    open_interest: Decimal | None = None
    volume_24h: Decimal | None = None
    liquidity: Decimal | None = None
    volume: Decimal | None = None
    close_time: datetime | None = None

    def __post_init__(self) -> None:
        if (
            not self.market_snapshot_id
            or not self.ingestion_run_id
            or not self.provider_market_ticker
        ):
            raise ValueError("complement market identities must not be empty")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("complement market observation must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))
        if self.close_time is not None and (
            self.close_time.tzinfo is None or self.close_time.utcoffset() is None
        ):
            raise ValueError("complement market close time must be timezone-aware")
        if self.close_time is not None:
            object.__setattr__(self, "close_time", self.close_time.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class RawComplementResponse:
    raw_response_id: str
    ingestion_run_id: str
    request_ordinal: int
    response: CapturedKalshiPublicResponse


@dataclass(frozen=True, slots=True)
class BinaryComplementScanRequest:
    market_discovery_run_id: str
    market_tickers: tuple[str, ...] = ()
    market_limit: int = 20
    maximum_depth: int = 5
    maximum_bundle_quantity: Decimal = Decimal("10")
    timeout_seconds: int = 10

    def __post_init__(self) -> None:
        if not self.market_discovery_run_id:
            raise ValueError("completed market discovery run identity is required")
        if not 1 <= self.market_limit <= 100:
            raise ValueError("binary complement market limit must be between one and 100")
        if not 1 <= self.maximum_depth <= 100:
            raise ValueError("binary complement depth must be between one and 100")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueError("binary complement timeout must be between one and 60")
        if len(self.market_tickers) > self.market_limit or len(set(self.market_tickers)) != len(
            self.market_tickers
        ):
            raise ValueError("binary complement market tickers must be unique and bounded")
        object.__setattr__(
            self,
            "maximum_bundle_quantity",
            parse_decimal(self.maximum_bundle_quantity, allow_zero=False),
        )

    @property
    def canonical_configuration_hash(self) -> ContentHash:
        return ContentHash(
            content_digest(
                {
                    "market_discovery_run_id": self.market_discovery_run_id,
                    "market_tickers": self.market_tickers,
                    "market_limit": self.market_limit,
                    "maximum_depth": self.maximum_depth,
                    "maximum_bundle_quantity": self.maximum_bundle_quantity,
                    "timeout_seconds": self.timeout_seconds,
                    "evaluator_version": COMPLEMENT_EVALUATOR_VERSION,
                    "rounding_identity": ROUNDING_IDENTITY,
                }
            )
        )


@dataclass(frozen=True, slots=True)
class PublicBinaryComplementReport:
    market_discovery_run_id: str
    markets_evaluated: int
    valid_orderbooks: int
    invalid_orderbooks: int
    no_candidate_count: int
    ineligible_count: int
    candidate_count: int
    matched_quantity: Decimal
    total_gross_gap: Decimal
    total_modeled_fees: Decimal
    total_conservative_net_lower_bound: Decimal
    evaluations: tuple[BinaryComplementEvaluation, ...]
    ranked_priorities: tuple[BinaryComplementEvaluation, ...]
    idempotent_replay: bool


class PublicOrderbookClient(Protocol):
    def fetch_market(
        self, *, market_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse: ...

    def fetch_event(
        self, *, event_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse: ...

    def fetch_orderbook(
        self,
        *,
        market_ticker: str,
        maximum_depth: int,
        timeout_seconds: int,
    ) -> CapturedKalshiPublicResponse: ...

    def fetch_series(
        self, *, series_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse: ...

    def fetch_series_fee_changes(
        self, *, series_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse: ...


class PublicBinaryComplementStore(Protocol):
    def select_completed_open_markets(
        self, *, ingestion_run_id: str, market_tickers: tuple[str, ...], limit: int
    ) -> tuple[ComplementMarket, ...]: ...

    def append_raw_complement_response(self, value: RawComplementResponse) -> None: ...

    def append_complement_evidence(
        self,
        *,
        request: BinaryComplementScanRequest,
        orderbooks: tuple[PublicOrderbookSnapshot, ...],
        levels: tuple[PublicOrderbookLevel, ...],
        fees: tuple[SeriesFeeSnapshot, ...],
        evaluations: tuple[BinaryComplementEvaluation, ...],
        slices: tuple[ComplementFillSlice, ...],
    ) -> None: ...

    def report_for_configuration(
        self, request: BinaryComplementScanRequest
    ) -> PublicBinaryComplementReport | None: ...


def _raw_response_id(response: CapturedKalshiPublicResponse, *, run_id: str, ordinal: int) -> str:
    return deterministic_id(
        "raw_provider_response",
        {
            "ingestion_run_id": run_id,
            "endpoint_identity": response.endpoint_identity,
            "request_ordinal": ordinal,
            "raw_payload_sha256": response.raw_payload_sha256,
        },
    )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_public_market_timestamp(value)
    except ValueValidationError:
        return None


def _exact_series_ticker(
    response: CapturedKalshiPublicResponse, *, expected_market_ticker: str
) -> str:
    market = response.document.get("market")
    if not isinstance(market, Mapping):
        raise PublicBinaryComplementError("exact market response omitted market evidence")
    market_ticker = market.get("ticker")
    if market_ticker != expected_market_ticker:
        raise PublicBinaryComplementError("exact market response changed market identity")
    series_ticker = market.get("series_ticker")
    if (
        not isinstance(series_ticker, str)
        or not series_ticker
        or len(series_ticker) > 300
        or any(character.isspace() for character in series_ticker)
    ):
        raise PublicBinaryComplementError("exact market response has no bounded series ticker")
    return series_ticker


def _exact_event_series_ticker(
    response: CapturedKalshiPublicResponse, *, market: ComplementMarket
) -> str:
    event = response.document.get("event")
    if not isinstance(event, Mapping):
        raise PublicBinaryComplementError("exact event response omitted event evidence")
    if event.get("event_ticker") != market.provider_event_ticker:
        raise PublicBinaryComplementError("exact event response changed event identity")
    nested = event.get("markets")
    if not isinstance(nested, list):
        raise PublicBinaryComplementError("exact event response omitted nested markets")
    matches = tuple(
        item
        for item in nested
        if isinstance(item, Mapping)
        and item.get("ticker") == market.provider_market_ticker
        and item.get("event_ticker") == market.provider_event_ticker
    )
    if len(matches) != 1:
        raise PublicBinaryComplementError("exact event response has ambiguous market membership")
    nested_rule = matches[0].get("rules_primary")
    if (
        not isinstance(nested_rule, str)
        or ContentHash(sha256(nested_rule.encode("utf-8")).hexdigest()) != market.rule_content_hash
    ):
        raise PublicBinaryComplementError("exact event response changed market rule lineage")
    series_ticker = event.get("series_ticker")
    if (
        not isinstance(series_ticker, str)
        or not series_ticker
        or len(series_ticker) > 300
        or any(character.isspace() for character in series_ticker)
    ):
        raise PublicBinaryComplementError("exact event response has no bounded series ticker")
    return series_ticker


def _optional_decimal(value: object) -> Decimal | None:
    try:
        return parse_decimal(cast(str | int | Decimal, value))
    except (TypeError, ValueValidationError):
        return None


def _normalize_orderbook(
    *,
    response: CapturedKalshiPublicResponse,
    raw_response_id: str,
    market: ComplementMarket,
    maximum_depth: int,
) -> tuple[PublicOrderbookSnapshot, tuple[PublicOrderbookLevel, ...]]:
    data_quality = "VALID"
    levels: list[PublicOrderbookLevel] = []
    raw_book = response.document.get("orderbook_fp")
    if not isinstance(raw_book, Mapping):
        data_quality = "INVALID"
        raw_book = {}
    snapshot_payload = {
        "raw_response_id": raw_response_id,
        "market_snapshot_id": market.market_snapshot_id,
        "provider_market_ticker": market.provider_market_ticker,
        "maximum_depth": maximum_depth,
        "orderbook": raw_book,
    }
    snapshot_id = deterministic_id("market_orderbook_snapshot", snapshot_payload)
    for side, field in (
        (OrderbookSide.YES_BID, "yes_dollars"),
        (OrderbookSide.NO_BID, "no_dollars"),
    ):
        raw_levels = raw_book.get(field)
        if not isinstance(raw_levels, list):
            data_quality = "INVALID"
            continue
        for ordinal, raw_level in enumerate(raw_levels[:maximum_depth]):
            if not isinstance(raw_level, list) or len(raw_level) not in {2, 3}:
                data_quality = "INVALID"
                continue
            bid_price = _optional_decimal(raw_level[0])
            quantity = _optional_decimal(raw_level[1])
            order_count = raw_level[2] if len(raw_level) == 3 else None
            if (
                bid_price is None
                or quantity is None
                or (
                    order_count is not None
                    and (isinstance(order_count, bool) or not isinstance(order_count, int))
                )
            ):
                data_quality = "INVALID"
                continue
            try:
                source_hash = ContentHash(content_digest((side, ordinal, tuple(raw_level))))
                levels.append(
                    PublicOrderbookLevel(
                        level_id=deterministic_id(
                            "market_orderbook_level",
                            {"snapshot_id": snapshot_id, "side": side, "ordinal": ordinal},
                        ),
                        orderbook_snapshot_id=snapshot_id,
                        side=side,
                        ordinal=ordinal,
                        bid_price=bid_price,
                        quantity=quantity,
                        order_count=cast(int | None, order_count),
                        source_hash=source_hash,
                    )
                )
            except ValueValidationError:
                data_quality = "INVALID"
    yes_count = sum(value.side is OrderbookSide.YES_BID for value in levels)
    no_count = sum(value.side is OrderbookSide.NO_BID for value in levels)
    return (
        PublicOrderbookSnapshot(
            orderbook_snapshot_id=snapshot_id,
            ingestion_run_id=market.ingestion_run_id,
            raw_response_id=raw_response_id,
            market_snapshot_id=market.market_snapshot_id,
            provider=PublicMarketProvider.KALSHI,
            provider_market_ticker=market.provider_market_ticker,
            provider_event_ticker=market.provider_event_ticker,
            series_ticker=market.series_ticker or "MISSING",
            observed_at=response.received_at,
            maximum_depth=maximum_depth,
            yes_bid_level_count=yes_count,
            no_bid_level_count=no_count,
            data_quality_state=data_quality,
            canonical_source_hash=ContentHash(content_digest(snapshot_payload)),
        ),
        tuple(levels),
    )


def _fee_snapshot(
    *,
    response: CapturedKalshiPublicResponse,
    raw_response_id: str,
    run_id: str,
    series_ticker: str,
    fallback: Mapping[str, object] | None = None,
) -> SeriesFeeSnapshot:
    series = response.document.get("series")
    values: Mapping[str, object] = series if isinstance(series, Mapping) else (fallback or {})
    raw_fee_type = values.get("fee_type")
    fee_type = raw_fee_type if isinstance(raw_fee_type, str) else None
    multiplier = _optional_decimal(values.get("fee_multiplier"))
    effective_at = _parse_timestamp(values.get("last_updated_ts") or values.get("scheduled_ts"))
    payload = {
        "raw_response_id": raw_response_id,
        "series_ticker": series_ticker,
        "fee_type": fee_type,
        "fee_multiplier": multiplier,
        "effective_at": effective_at,
        "schedule_identity": SCHEDULE_IDENTITY,
    }
    return SeriesFeeSnapshot(
        fee_snapshot_id=deterministic_id("series_fee_snapshot", payload),
        ingestion_run_id=run_id,
        raw_response_id=raw_response_id,
        provider=PublicMarketProvider.KALSHI,
        series_ticker=series_ticker,
        observed_at=response.received_at,
        fee_type=fee_type,
        fee_multiplier=multiplier,
        effective_at=effective_at,
        fee_model_identity=FEE_MODEL_IDENTITY,
        taker_coefficient=TAKER_COEFFICIENT,
        maker_coefficient=MAKER_COEFFICIENT,
        rounding_identity=ROUNDING_IDENTITY,
        schedule_identity=SCHEDULE_IDENTITY,
        schedule_effective_date=SCHEDULE_EFFECTIVE_DATE,
        source_hash=ContentHash(content_digest(payload)),
    )


def _active_fee_change(
    *, entries: object, series_ticker: str, observed_at: datetime
) -> Mapping[str, object] | None:
    """Choose only the latest exact series fee change effective at capture time."""

    if not isinstance(entries, list):
        return None
    candidates: list[tuple[datetime, Mapping[str, object]]] = []
    for value in entries:
        if not isinstance(value, Mapping) or value.get("series_ticker") != series_ticker:
            continue
        effective_at = _parse_timestamp(value.get("scheduled_ts"))
        if effective_at is not None and effective_at <= observed_at:
            candidates.append((effective_at, value))
    return max(candidates, key=lambda value: value[0])[1] if candidates else None


class ScanPublicBinaryComplements:
    """Collect bounded public evidence and rank only positive conservative shadows."""

    def __init__(
        self,
        *,
        client: PublicOrderbookClient,
        store: PublicBinaryComplementStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    @staticmethod
    def _report(
        *,
        request: BinaryComplementScanRequest,
        evaluations: tuple[BinaryComplementEvaluation, ...],
        valid_orderbooks: int,
        invalid_orderbooks: int,
        idempotent_replay: bool,
    ) -> PublicBinaryComplementReport:
        counts = {state: 0 for state in ComplementState}
        for value in evaluations:
            counts[value.state] += 1
        return PublicBinaryComplementReport(
            market_discovery_run_id=request.market_discovery_run_id,
            markets_evaluated=len(evaluations),
            valid_orderbooks=valid_orderbooks,
            invalid_orderbooks=invalid_orderbooks,
            no_candidate_count=counts[ComplementState.NO_CANDIDATE],
            ineligible_count=counts[ComplementState.INELIGIBLE],
            candidate_count=counts[ComplementState.CANDIDATE],
            matched_quantity=sum((value.matched_quantity for value in evaluations), Decimal(0)),
            total_gross_gap=sum((value.gross_complement_gap for value in evaluations), Decimal(0)),
            total_modeled_fees=sum((value.modeled_taker_fees for value in evaluations), Decimal(0)),
            total_conservative_net_lower_bound=sum(
                (value.conservative_net_lower_bound for value in evaluations), Decimal(0)
            ),
            evaluations=evaluations,
            ranked_priorities=rank_shadow_executable_quality(evaluations),
            idempotent_replay=idempotent_replay,
        )

    def scan(self, request: BinaryComplementScanRequest) -> PublicBinaryComplementReport:
        existing = self._store.report_for_configuration(request)
        if existing is not None:
            return PublicBinaryComplementReport(
                **{
                    field: getattr(existing, field)
                    for field in existing.__dataclass_fields__
                    if field != "idempotent_replay"
                },
                idempotent_replay=True,
            )
        markets = self._store.select_completed_open_markets(
            ingestion_run_id=request.market_discovery_run_id,
            market_tickers=request.market_tickers,
            limit=request.market_limit,
        )
        if not markets:
            raise PublicBinaryComplementError(
                "no eligible open markets exist in the completed scan"
            )
        enriched_markets: list[ComplementMarket] = []
        for ordinal, market in enumerate(markets):
            if market.series_ticker is not None:
                enriched_markets.append(market)
                continue
            if market.provider_event_ticker is None:
                response = self._client.fetch_market(
                    market_ticker=market.provider_market_ticker,
                    timeout_seconds=request.timeout_seconds,
                )
                request_ordinal = 5_000 + ordinal
                series_ticker = _exact_series_ticker(
                    response,
                    expected_market_ticker=market.provider_market_ticker,
                )
            else:
                response = self._client.fetch_event(
                    event_ticker=market.provider_event_ticker,
                    timeout_seconds=request.timeout_seconds,
                )
                request_ordinal = 6_000 + ordinal
                series_ticker = _exact_event_series_ticker(response, market=market)
            raw_id = _raw_response_id(
                response,
                run_id=request.market_discovery_run_id,
                ordinal=request_ordinal,
            )
            self._store.append_raw_complement_response(
                RawComplementResponse(
                    raw_response_id=raw_id,
                    ingestion_run_id=request.market_discovery_run_id,
                    request_ordinal=request_ordinal,
                    response=response,
                )
            )
            enriched_markets.append(
                replace(
                    market,
                    series_ticker=series_ticker,
                )
            )
        markets = tuple(enriched_markets)

        orderbooks_and_levels: list[
            tuple[PublicOrderbookSnapshot, tuple[PublicOrderbookLevel, ...]]
        ] = []
        for ordinal, market in enumerate(markets):
            orderbook_response = self._client.fetch_orderbook(
                market_ticker=market.provider_market_ticker,
                maximum_depth=request.maximum_depth,
                timeout_seconds=request.timeout_seconds,
            )
            orderbook_raw_id = _raw_response_id(
                orderbook_response,
                run_id=request.market_discovery_run_id,
                ordinal=10_000 + ordinal,
            )
            self._store.append_raw_complement_response(
                RawComplementResponse(
                    raw_response_id=orderbook_raw_id,
                    ingestion_run_id=request.market_discovery_run_id,
                    request_ordinal=10_000 + ordinal,
                    response=orderbook_response,
                )
            )
            orderbooks_and_levels.append(
                _normalize_orderbook(
                    response=orderbook_response,
                    raw_response_id=orderbook_raw_id,
                    market=market,
                    maximum_depth=request.maximum_depth,
                )
            )
        fees: dict[str, SeriesFeeSnapshot] = {}
        for ordinal, series_ticker in enumerate(
            sorted({cast(str, value.series_ticker) for value in markets})
        ):
            response = self._client.fetch_series(
                series_ticker=series_ticker, timeout_seconds=request.timeout_seconds
            )
            raw_id = _raw_response_id(
                response, run_id=request.market_discovery_run_id, ordinal=20_000 + ordinal * 2
            )
            self._store.append_raw_complement_response(
                RawComplementResponse(
                    raw_response_id=raw_id,
                    ingestion_run_id=request.market_discovery_run_id,
                    request_ordinal=20_000 + ordinal * 2,
                    response=response,
                )
            )
            candidate = _fee_snapshot(
                response=response,
                raw_response_id=raw_id,
                run_id=request.market_discovery_run_id,
                series_ticker=series_ticker,
            )
            # The documented fee-change endpoint is a bounded fallback only
            # when current series metadata cannot express the active fee.
            if candidate.fee_type is None or candidate.fee_multiplier is None:
                changes = self._client.fetch_series_fee_changes(
                    series_ticker=series_ticker, timeout_seconds=request.timeout_seconds
                )
                changes_raw_id = _raw_response_id(
                    changes,
                    run_id=request.market_discovery_run_id,
                    ordinal=20_001 + ordinal * 2,
                )
                self._store.append_raw_complement_response(
                    RawComplementResponse(
                        raw_response_id=changes_raw_id,
                        ingestion_run_id=request.market_discovery_run_id,
                        request_ordinal=20_001 + ordinal * 2,
                        response=changes,
                    )
                )
                selected = _active_fee_change(
                    entries=changes.document.get("series_fee_change_arr"),
                    series_ticker=series_ticker,
                    observed_at=changes.received_at,
                )
                candidate = _fee_snapshot(
                    response=changes,
                    raw_response_id=changes_raw_id,
                    run_id=request.market_discovery_run_id,
                    series_ticker=series_ticker,
                    fallback=selected,
                )
            fees[series_ticker] = candidate

        evaluations: list[BinaryComplementEvaluation] = []
        all_slices: list[ComplementFillSlice] = []
        for market, (orderbook, levels) in zip(markets, orderbooks_and_levels, strict=True):
            evaluation, slices = evaluate_binary_complement(
                orderbook=orderbook,
                fee=fees.get(cast(str, market.series_ticker)),
                levels=levels,
                market_is_open=market.status == "open" and market.data_quality_state == "ELIGIBLE",
                rule_content_hash=market.rule_content_hash,
                maximum_bundle_quantity=request.maximum_bundle_quantity,
                evaluated_at=self._clock().astimezone(UTC),
                evaluator_version=COMPLEMENT_EVALUATOR_VERSION,
            )
            evaluations.append(evaluation)
            all_slices.extend(slices)
        orderbooks = tuple(value[0] for value in orderbooks_and_levels)
        all_levels = tuple(level for _, levels in orderbooks_and_levels for level in levels)
        self._store.append_complement_evidence(
            request=request,
            orderbooks=orderbooks,
            levels=all_levels,
            fees=tuple(fees[key] for key in sorted(fees)),
            evaluations=tuple(evaluations),
            slices=tuple(all_slices),
        )
        return self._report(
            request=request,
            evaluations=tuple(evaluations),
            valid_orderbooks=sum(value.data_quality_state == "VALID" for value in orderbooks),
            invalid_orderbooks=sum(value.data_quality_state != "VALID" for value in orderbooks),
            idempotent_replay=False,
        )


def _descending_activity(value: Decimal | None) -> tuple[bool, Decimal]:
    """Order a retained activity measure highest-first; a missing value sorts last.

    Mirrors ``scan_public_threshold_ladders._activity_key``'s own
    ``descending`` helper exactly: ``None`` never competes with a genuine
    zero-or-positive measurement, so an evidence gap can never look more
    attractive than retained evidence.
    """

    return (value is None, Decimal(0) if value is None else -value)


def _activity_rank_key(market: ComplementMarket) -> tuple[object, ...]:
    """Rank retained candidates by genuine activity, deterministic ticker tiebreak.

    Selecting which of a discovery run's open markets receive one of this
    scan's bounded orderbook requests previously used alphabetical ticker
    order -- no connection to whether a market has any real trading
    activity at all.  This reuses the exact field-priority idiom the
    sibling ``KALSHI_RELATED_MARKET_CONSISTENCY_V1`` threshold-ladder scan
    already established and has run safely in production
    (``scan_public_threshold_ladders._activity_key``): open interest, then
    24-hour volume, then displayed liquidity, then all-time volume, each
    missing-safe via ``_descending_activity``; close time and the ticker
    itself remain only as final deterministic tiebreaks.  Ranking here never
    changes which markets are structurally eligible -- it only changes which
    already-eligible markets a bounded scan spends its orderbook-request
    budget on first.
    """

    return (
        _descending_activity(market.open_interest),
        _descending_activity(market.volume_24h),
        _descending_activity(market.liquidity),
        _descending_activity(market.volume),
        market.close_time is None,
        market.close_time or datetime.max.replace(tzinfo=UTC),
        market.provider_market_ticker,
    )


class InMemoryPublicBinaryComplementStore:
    """Deterministic test store; it intentionally contains no financial repositories."""

    def __init__(self, markets: tuple[ComplementMarket, ...]) -> None:
        self._markets = markets
        self._raw: dict[str, RawComplementResponse] = {}
        self._evaluations: dict[str, PublicBinaryComplementReport] = {}

    def select_completed_open_markets(
        self, *, ingestion_run_id: str, market_tickers: tuple[str, ...], limit: int
    ) -> tuple[ComplementMarket, ...]:
        values = tuple(
            value
            for value in self._markets
            if value.ingestion_run_id == ingestion_run_id
            and (not market_tickers or value.provider_market_ticker in market_tickers)
            and value.status == "open"
            and value.data_quality_state == "ELIGIBLE"
        )
        return tuple(sorted(values, key=_activity_rank_key)[:limit])

    def append_raw_complement_response(self, value: RawComplementResponse) -> None:
        existing = self._raw.get(value.raw_response_id)
        if existing is not None and existing.response.raw_payload != value.response.raw_payload:
            raise PublicBinaryComplementConflictError("raw public response identity conflicts")
        self._raw[value.raw_response_id] = value

    def append_complement_evidence(
        self,
        *,
        request: BinaryComplementScanRequest,
        orderbooks: tuple[PublicOrderbookSnapshot, ...],
        levels: tuple[PublicOrderbookLevel, ...],
        fees: tuple[SeriesFeeSnapshot, ...],
        evaluations: tuple[BinaryComplementEvaluation, ...],
        slices: tuple[ComplementFillSlice, ...],
    ) -> None:
        del orderbooks, levels, fees, slices
        request_key = request.canonical_configuration_hash.value
        if request_key in self._evaluations:
            raise PublicBinaryComplementConflictError("complement evaluation identity conflicts")
        self._evaluations[request_key] = ScanPublicBinaryComplements._report(
            request=request,
            evaluations=evaluations,
            valid_orderbooks=len(evaluations),
            invalid_orderbooks=0,
            idempotent_replay=False,
        )

    def report_for_configuration(
        self, request: BinaryComplementScanRequest
    ) -> PublicBinaryComplementReport | None:
        return self._evaluations.get(request.canonical_configuration_hash.value)
