"""Pure rules for bounded, authority-free multi-asset crypto discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from itertools import pairwise

from apex.domain.research_evidence import EvidenceClass
from apex.domain.values import content_digest, parse_decimal

COINBASE_PUBLIC_PRODUCTS_V1 = "COINBASE_ADVANCED_TRADE_PUBLIC_PRODUCTS_V1"
COINBASE_PUBLIC_PRODUCT_PARSER_V1 = "COINBASE_PUBLIC_PRODUCT_PARSER_V1"
CRYPTO_UNIVERSE_POLICY_V1 = "CRYPTO_RESEARCH_UNIVERSE_POLICY_V1"
CRYPTO_UNIVERSE_POLICY_V2 = "CRYPTO_UNIVERSE_SELECTION_QUALITY_POLICY_V1"
CRYPTO_DISCOVERY_V1 = "CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1"
CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1 = "CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1"
CRYPTO_RELATIVE_STRENGTH_CALIBRATION_V1 = "CRYPTO_RELATIVE_STRENGTH_WALK_FORWARD_V1"
CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1 = "CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1"
CRYPTO_REVERSAL_REGIME_CALIBRATION_V1 = "CRYPTO_REVERSAL_REGIME_WALK_FORWARD_V1"
CRYPTO_UNIVERSE_WARNING = "CRYPTO RESEARCH ONLY — NO CAPITAL AUTHORITY"
SUPPORTED_QUOTES = ("USD", "USDC")
MAX_CATALOG_PAGES = 10
MAX_PRODUCTS_PER_PAGE = 100
MAX_CONSIDERED_PRODUCTS = MAX_CATALOG_PAGES * MAX_PRODUCTS_PER_PAGE
MAX_CATALOG_BYTES = 20_000_000
MAX_CATALOG_RUNTIME_MILLISECONDS = 150_000
MAX_ELIGIBLE_PRODUCTS = 10
MAX_OBSERVED_PRODUCTS = 20
MAX_OPPORTUNITIES = 3
MIN_HISTORY_BUCKETS = 170
MAX_MISSING_BUCKETS = 2
MIN_MEAN_DAILY_QUOTE_VOLUME = Decimal("1000000")
FEE_BPS_PER_SIDE = Decimal("60")
SLIPPAGE_BPS_PER_SIDE = Decimal("10")
HYPOTHETICAL_HOLDING_DAYS = 30
MAX_RESEARCH_ALLOCATION = Decimal("0.10")
METRIC_QUANTUM = Decimal("0.00000001")
RELATIVE_STRENGTH_MINIMUM_SAMPLES = 30
RELATIVE_STRENGTH_HOLDING_DAYS = 30
RELATIVE_STRENGTH_FRESHNESS_SECONDS = 36 * 3600
RELATIVE_STRENGTH_MINIMUM_DEPTH = Decimal("1000")
REVERSAL_REGIME_PRIMARY_HOLDING_DAYS = 14
REVERSAL_REGIME_SECONDARY_HOLDING_DAYS = 30
CRYPTO_CALIBRATION_HISTORY_MINIMUM_DAYS = 365
CRYPTO_CALIBRATION_HISTORY_MAXIMUM_DAYS = 800
CRYPTO_CALIBRATION_MAXIMUM_REQUESTS_PER_ASSET = 12
STABLE_VALUE_BASE_ASSETS = frozenset(
    {"DAI", "EURC", "FDUSD", "GUSD", "PYUSD", "TUSD", "USD", "USDC", "USDS", "USDT"}
)
WRAPPED_DUPLICATE_BASE_ASSETS = frozenset({"CBETH", "CBBTC", "STETH", "WBTC", "WETH", "WSTETH"})
LEVERAGED_TOKEN_SUFFIXES = ("3L", "3S", "BULL", "BEAR", "UP", "DOWN")


class CryptoUniverseError(ValueError):
    pass


class ProductEligibility(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    EXCLUDED = "EXCLUDED"


class OpportunityStatus(StrEnum):
    RESEARCH_ONLY = "RESEARCH_ONLY"
    REJECTED = "REJECTED"


class CatalogPaginationStatus(StrEnum):
    COMPLETE = "COMPLETE"
    TRUNCATED = "TRUNCATED"
    LEGACY_DIAGNOSTIC = "LEGACY_DIAGNOSTIC"


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None:
        raise CryptoUniverseError(reason)
    return value.astimezone(UTC)


def _identity(value: str, reason: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 160:
        raise CryptoUniverseError(reason)
    return normalized


def _positive(value: object, reason: str) -> Decimal:
    try:
        parsed = parse_decimal(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, ArithmeticError) as error:
        raise CryptoUniverseError(reason) from error
    if parsed <= 0:
        raise CryptoUniverseError(reason)
    return parsed


def _nonnegative(value: object, reason: str) -> Decimal:
    try:
        parsed = parse_decimal(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, ArithmeticError) as error:
        raise CryptoUniverseError(reason) from error
    if parsed < 0:
        raise CryptoUniverseError(reason)
    return parsed


def metric(value: Decimal) -> Decimal:
    return value.quantize(METRIC_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class CryptoProductPageEvidence:
    page_ordinal: int
    request_cursor: str | None
    next_cursor: str | None
    has_next: bool
    product_ids: tuple[str, ...]
    product_count: int
    raw_evidence_hash: str
    raw_bytes: int
    completed_at: datetime
    source_locator: str
    runtime_milliseconds: int
    completion_status: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        page_ordinal: int,
        request_cursor: str | None,
        next_cursor: str | None,
        has_next: bool,
        product_ids: tuple[str, ...],
        raw_evidence_hash: str,
        raw_bytes: int,
        completed_at: datetime,
        source_locator: str,
        runtime_milliseconds: int,
    ) -> CryptoProductPageEvidence:
        if not 1 <= page_ordinal <= MAX_CATALOG_PAGES:
            raise CryptoUniverseError("CRYPTO_PRODUCT_PAGE_ORDINAL_INVALID")
        if not product_ids or len(product_ids) > MAX_PRODUCTS_PER_PAGE:
            raise CryptoUniverseError("CRYPTO_PRODUCT_PAGE_BOUND_INVALID")
        if raw_bytes <= 0 or raw_bytes > MAX_CATALOG_BYTES or runtime_milliseconds < 0:
            raise CryptoUniverseError("CRYPTO_PRODUCT_PAGE_EVIDENCE_INVALID")
        if has_next and not next_cursor:
            raise CryptoUniverseError("CRYPTO_PRODUCT_PAGE_CURSOR_MISSING")
        if not has_next and next_cursor:
            raise CryptoUniverseError("CRYPTO_PRODUCT_PAGE_CURSOR_INCONSISTENT")
        status = "CONTINUED" if has_next else "TERMINAL"
        payload = (
            page_ordinal,
            request_cursor,
            next_cursor,
            has_next,
            product_ids,
            len(product_ids),
            raw_evidence_hash,
            raw_bytes,
            _time(completed_at, "CRYPTO_PRODUCT_PAGE_TIME_INVALID"),
            source_locator,
            runtime_milliseconds,
            status,
        )
        return cls(*payload, content_digest(payload))


@dataclass(frozen=True, slots=True)
class PublicCryptoProduct:
    product_id: str
    provider: str
    base_asset: str
    quote_asset: str
    market_type: str
    trading_status: str
    product_status: str
    price_increment: Decimal
    quantity_increment: Decimal
    minimum_quantity: Decimal | None
    minimum_notional: Decimal | None
    public_volume_24h: Decimal | None
    captured_at: datetime
    source_contract_version: str
    raw_evidence_hash: str
    parser_version: str
    evidence_class: EvidenceClass
    canonical_hash: str
    canonical_asset_id: str = ""
    provider_alias: str | None = None
    provider_alias_to: tuple[str, ...] = ()
    restriction_reasons: tuple[str, ...] = ()
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        product_id: str,
        provider: str,
        base_asset: str,
        quote_asset: str,
        market_type: str,
        trading_status: str,
        product_status: str,
        price_increment: object,
        quantity_increment: object,
        minimum_quantity: object | None,
        minimum_notional: object | None,
        public_volume_24h: object | None,
        captured_at: datetime,
        source_contract_version: str,
        raw_evidence_hash: str,
        parser_version: str,
        evidence_class: EvidenceClass,
        provider_alias: str | None = None,
        provider_alias_to: tuple[str, ...] = (),
        restriction_reasons: tuple[str, ...] = (),
    ) -> PublicCryptoProduct:
        identity = _identity(product_id, "CRYPTO_PRODUCT_ID_INVALID")
        price = _nonnegative(price_increment, "CRYPTO_PRICE_INCREMENT_INVALID")
        quantity = _nonnegative(quantity_increment, "CRYPTO_QUANTITY_INCREMENT_INVALID")
        minimum_qty = (
            None
            if minimum_quantity is None
            else _positive(minimum_quantity, "CRYPTO_MINIMUM_QUANTITY_INVALID")
        )
        minimum_value = (
            None
            if minimum_notional is None
            else _positive(minimum_notional, "CRYPTO_MINIMUM_NOTIONAL_INVALID")
        )
        volume = (
            None
            if public_volume_24h is None
            else _nonnegative(public_volume_24h, "CRYPTO_PUBLIC_VOLUME_INVALID")
        )
        payload = (
            identity,
            provider,
            base_asset,
            quote_asset,
            market_type,
            trading_status,
            product_status,
            price,
            quantity,
            minimum_qty,
            minimum_value,
            volume,
            _time(captured_at, "CRYPTO_PRODUCT_CAPTURE_TIME_INVALID"),
            source_contract_version,
            raw_evidence_hash,
            parser_version,
            evidence_class,
            _identity(base_asset, "CRYPTO_BASE_ASSET_INVALID"),
            provider_alias,
            tuple(sorted(set(provider_alias_to))),
            tuple(sorted(set(restriction_reasons))),
            "NONE",
        )
        return cls(
            product_id=identity,
            provider=provider,
            base_asset=base_asset,
            quote_asset=quote_asset,
            market_type=market_type,
            trading_status=trading_status,
            product_status=product_status,
            price_increment=price,
            quantity_increment=quantity,
            minimum_quantity=minimum_qty,
            minimum_notional=minimum_value,
            public_volume_24h=volume,
            captured_at=_time(captured_at, "CRYPTO_PRODUCT_CAPTURE_TIME_INVALID"),
            source_contract_version=source_contract_version,
            raw_evidence_hash=raw_evidence_hash,
            parser_version=parser_version,
            evidence_class=evidence_class,
            canonical_hash=content_digest(payload),
            canonical_asset_id=_identity(base_asset, "CRYPTO_BASE_ASSET_INVALID"),
            provider_alias=provider_alias,
            provider_alias_to=tuple(sorted(set(provider_alias_to))),
            restriction_reasons=tuple(sorted(set(restriction_reasons))),
        )


@dataclass(frozen=True, slots=True)
class CryptoProductCapture:
    capture_id: str
    provider: str
    captured_at: datetime
    source_contract_version: str
    parser_version: str
    evidence_class: EvidenceClass
    raw_evidence_hash: str
    product_ids: tuple[str, ...]
    request_count: int
    limitations: tuple[str, ...]
    canonical_hash: str
    pages: tuple[CryptoProductPageEvidence, ...] = ()
    pagination_status: CatalogPaginationStatus = CatalogPaginationStatus.LEGACY_DIAGNOSTIC
    continuation_cursor: str | None = None
    total_raw_bytes: int = 0
    runtime_milliseconds: int = 0
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        capture_id: str,
        provider: str,
        captured_at: datetime,
        source_contract_version: str,
        parser_version: str,
        evidence_class: EvidenceClass,
        raw_evidence_hash: str,
        products: tuple[PublicCryptoProduct, ...],
        request_count: int,
        limitations: tuple[str, ...],
        pages: tuple[CryptoProductPageEvidence, ...],
        pagination_status: CatalogPaginationStatus,
        continuation_cursor: str | None,
        total_raw_bytes: int,
        runtime_milliseconds: int,
    ) -> CryptoProductCapture:
        if not 1 <= len(products) <= MAX_CONSIDERED_PRODUCTS:
            raise CryptoUniverseError("CRYPTO_PRODUCT_CAPTURE_BOUND_INVALID")
        if (
            not 1 <= request_count <= MAX_CATALOG_PAGES
            or len(pages) != request_count
            or total_raw_bytes > MAX_CATALOG_BYTES
            or runtime_milliseconds > MAX_CATALOG_RUNTIME_MILLISECONDS
        ):
            raise CryptoUniverseError("CRYPTO_PRODUCT_CAPTURE_PAGINATION_BOUND_INVALID")
        if pagination_status is CatalogPaginationStatus.COMPLETE and continuation_cursor:
            raise CryptoUniverseError("CRYPTO_PRODUCT_CAPTURE_COMPLETION_INVALID")
        if pagination_status is CatalogPaginationStatus.TRUNCATED and not continuation_cursor:
            raise CryptoUniverseError("CRYPTO_PRODUCT_CAPTURE_TRUNCATION_INVALID")
        product_ids = tuple(sorted(item.product_id for item in products))
        if len(set(product_ids)) != len(product_ids):
            raise CryptoUniverseError("CRYPTO_PRODUCT_CAPTURE_DUPLICATE")
        payload = (
            _identity(capture_id, "CRYPTO_PRODUCT_CAPTURE_ID_INVALID"),
            provider,
            _time(captured_at, "CRYPTO_PRODUCT_CAPTURE_TIME_INVALID"),
            source_contract_version,
            parser_version,
            evidence_class,
            raw_evidence_hash,
            product_ids,
            request_count,
            tuple(sorted(set(limitations))),
            pages,
            pagination_status,
            continuation_cursor,
            total_raw_bytes,
            runtime_milliseconds,
            "NONE",
        )
        return cls(
            capture_id=_identity(capture_id, "CRYPTO_PRODUCT_CAPTURE_ID_INVALID"),
            provider=provider,
            captured_at=_time(captured_at, "CRYPTO_PRODUCT_CAPTURE_TIME_INVALID"),
            source_contract_version=source_contract_version,
            parser_version=parser_version,
            evidence_class=evidence_class,
            raw_evidence_hash=raw_evidence_hash,
            product_ids=product_ids,
            request_count=request_count,
            limitations=tuple(sorted(set(limitations))),
            canonical_hash=content_digest(payload),
            pages=pages,
            pagination_status=pagination_status,
            continuation_cursor=continuation_cursor,
            total_raw_bytes=total_raw_bytes,
            runtime_milliseconds=runtime_milliseconds,
        )

    @property
    def catalog_complete(self) -> bool:
        return self.pagination_status is CatalogPaginationStatus.COMPLETE


@dataclass(frozen=True, slots=True)
class CryptoDailyObservation:
    bucket_start: datetime
    close: Decimal
    volume: Decimal

    @classmethod
    def create(
        cls, *, bucket_start: datetime, close: object, volume: object
    ) -> CryptoDailyObservation:
        timestamp = _time(bucket_start, "CRYPTO_OBSERVATION_TIME_INVALID")
        if timestamp.hour or timestamp.minute or timestamp.second or timestamp.microsecond:
            raise CryptoUniverseError("CRYPTO_OBSERVATION_BUCKET_INVALID")
        parsed_close = _positive(close, "CRYPTO_OBSERVATION_CLOSE_INVALID")
        parsed_volume = _nonnegative(volume, "CRYPTO_OBSERVATION_VOLUME_INVALID")
        return cls(timestamp, parsed_close, parsed_volume)


@dataclass(frozen=True, slots=True)
class CryptoHistoryPartition:
    ordinal: int
    interval_start: datetime
    interval_end: datetime
    accepted_buckets: int
    missing_buckets: int
    duplicate_records: int
    rejected_records: int
    raw_evidence_hash: str
    source_locator: str
    runtime_milliseconds: int

    @classmethod
    def create(
        cls,
        *,
        ordinal: int,
        interval_start: datetime,
        interval_end: datetime,
        accepted_buckets: int,
        missing_buckets: int,
        duplicate_records: int,
        rejected_records: int,
        raw_evidence_hash: str,
        source_locator: str,
        runtime_milliseconds: int,
    ) -> CryptoHistoryPartition:
        start = _time(interval_start, "CRYPTO_HISTORY_PARTITION_INTERVAL_INVALID")
        end = _time(interval_end, "CRYPTO_HISTORY_PARTITION_INTERVAL_INVALID")
        expected = (end - start).days
        if (
            ordinal < 1
            or end <= start
            or expected > 300
            or accepted_buckets < 0
            or missing_buckets < 0
            or accepted_buckets + missing_buckets != expected
            or duplicate_records < 0
            or rejected_records < 0
            or len(raw_evidence_hash) != 64
            or not source_locator
            or runtime_milliseconds < 0
        ):
            raise CryptoUniverseError("CRYPTO_HISTORY_PARTITION_INVALID")
        return cls(
            ordinal,
            start,
            end,
            accepted_buckets,
            missing_buckets,
            duplicate_records,
            rejected_records,
            raw_evidence_hash,
            source_locator,
            runtime_milliseconds,
        )


@dataclass(frozen=True, slots=True)
class CryptoProductObservation:
    observation_id: str
    source_capture_id: str
    product_id: str
    interval_start: datetime
    interval_end: datetime
    evidence_class: EvidenceClass
    candles: tuple[CryptoDailyObservation, ...]
    missing_buckets: int
    duplicate_records: int
    rejected_records: int
    request_count: int
    raw_evidence_hashes: tuple[str, ...]
    source_contract_version: str
    parser_version: str
    checkpoint_ordinal: int
    completed_at: datetime
    canonical_hash: str
    canonical_asset_id: str = ""
    authority: str = "NONE"
    coverage_partitions: tuple[CryptoHistoryPartition, ...] = ()
    coverage_status: str = "LEGACY_WINDOW"
    coverage_limitations: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        observation_id: str,
        source_capture_id: str,
        product_id: str,
        interval_start: datetime,
        interval_end: datetime,
        evidence_class: EvidenceClass,
        candles: tuple[CryptoDailyObservation, ...],
        missing_buckets: int,
        duplicate_records: int,
        rejected_records: int,
        request_count: int,
        raw_evidence_hashes: tuple[str, ...],
        source_contract_version: str,
        parser_version: str,
        checkpoint_ordinal: int,
        completed_at: datetime,
        canonical_asset_id: str,
        coverage_partitions: tuple[CryptoHistoryPartition, ...] = (),
        coverage_status: str = "LEGACY_WINDOW",
        coverage_limitations: tuple[str, ...] = (),
    ) -> CryptoProductObservation:
        start = _time(interval_start, "CRYPTO_OBSERVATION_INTERVAL_INVALID")
        end = _time(interval_end, "CRYPTO_OBSERVATION_INTERVAL_INVALID")
        ordered = tuple(sorted(candles, key=lambda item: item.bucket_start))
        if end <= start or not 1 <= request_count <= CRYPTO_CALIBRATION_MAXIMUM_REQUESTS_PER_ASSET:
            raise CryptoUniverseError("CRYPTO_OBSERVATION_BOUND_INVALID")
        if len({item.bucket_start for item in ordered}) != len(ordered):
            raise CryptoUniverseError("CRYPTO_OBSERVATION_DUPLICATE_BUCKET")
        if any(not start <= item.bucket_start < end for item in ordered):
            raise CryptoUniverseError("CRYPTO_OBSERVATION_OUTSIDE_INTERVAL")
        ordered_partitions = tuple(sorted(coverage_partitions, key=lambda item: item.ordinal))
        limitations = tuple(sorted(set(coverage_limitations)))
        if ordered_partitions and (
            len(ordered_partitions) != request_count
            or tuple(item.ordinal for item in ordered_partitions)
            != tuple(range(1, request_count + 1))
            or ordered_partitions[0].interval_start != start
            or ordered_partitions[-1].interval_end != end
            or any(
                left.interval_end != right.interval_start
                for left, right in pairwise(ordered_partitions)
            )
            or sum(item.accepted_buckets for item in ordered_partitions) != len(ordered)
            or sum(item.missing_buckets for item in ordered_partitions) != missing_buckets
            or sum(item.duplicate_records for item in ordered_partitions) != duplicate_records
            or sum(item.rejected_records for item in ordered_partitions) != rejected_records
            or tuple(item.raw_evidence_hash for item in ordered_partitions) != raw_evidence_hashes
        ):
            raise CryptoUniverseError("CRYPTO_HISTORY_PARTITION_LINEAGE_INVALID")
        if coverage_status not in {
            "LEGACY_WINDOW",
            "COMPLETE",
            "PARTIAL_LISTING_HISTORY",
            "INSUFFICIENT_HISTORY",
        }:
            raise CryptoUniverseError("CRYPTO_HISTORY_COVERAGE_STATUS_INVALID")
        base_payload = (
            _identity(observation_id, "CRYPTO_OBSERVATION_ID_INVALID"),
            _identity(source_capture_id, "CRYPTO_PRODUCT_CAPTURE_ID_INVALID"),
            _identity(product_id, "CRYPTO_PRODUCT_ID_INVALID"),
            start,
            end,
            evidence_class,
            ordered,
            missing_buckets,
            duplicate_records,
            rejected_records,
            request_count,
            raw_evidence_hashes,
            source_contract_version,
            parser_version,
            checkpoint_ordinal,
            _time(completed_at, "CRYPTO_OBSERVATION_COMPLETION_INVALID"),
            _identity(canonical_asset_id, "CRYPTO_BASE_ASSET_INVALID"),
        )
        payload = (
            (*base_payload, "NONE")
            if not ordered_partitions and coverage_status == "LEGACY_WINDOW" and not limitations
            else (*base_payload, ordered_partitions, coverage_status, limitations, "NONE")
        )
        return cls(
            observation_id=_identity(observation_id, "CRYPTO_OBSERVATION_ID_INVALID"),
            source_capture_id=_identity(source_capture_id, "CRYPTO_PRODUCT_CAPTURE_ID_INVALID"),
            product_id=_identity(product_id, "CRYPTO_PRODUCT_ID_INVALID"),
            interval_start=start,
            interval_end=end,
            evidence_class=evidence_class,
            candles=ordered,
            missing_buckets=missing_buckets,
            duplicate_records=duplicate_records,
            rejected_records=rejected_records,
            request_count=request_count,
            raw_evidence_hashes=raw_evidence_hashes,
            source_contract_version=source_contract_version,
            parser_version=parser_version,
            checkpoint_ordinal=checkpoint_ordinal,
            completed_at=_time(completed_at, "CRYPTO_OBSERVATION_COMPLETION_INVALID"),
            canonical_hash=content_digest(payload),
            canonical_asset_id=_identity(canonical_asset_id, "CRYPTO_BASE_ASSET_INVALID"),
            coverage_partitions=ordered_partitions,
            coverage_status=coverage_status,
            coverage_limitations=limitations,
        )

    @property
    def mean_daily_quote_volume(self) -> Decimal:
        if not self.candles:
            return Decimal(0)
        return metric(
            sum((item.close * item.volume for item in self.candles), Decimal(0))
            / Decimal(len(self.candles))
        )


def product_route_exclusion_reasons(product: PublicCryptoProduct) -> tuple[str, ...]:
    reasons: list[str] = []
    base = product.canonical_asset_id or product.base_asset
    if product.market_type != "SPOT":
        reasons.append("MARKET_TYPE_NOT_SPOT")
    if product.quote_asset not in SUPPORTED_QUOTES:
        reasons.append("QUOTE_CURRENCY_UNSUPPORTED")
    if (
        product.trading_status != "ACTIVE"
        or product.product_status != "ONLINE"
        or product.restriction_reasons
    ):
        reasons.append("PRODUCT_INACTIVE_OR_RESTRICTED")
    if base in STABLE_VALUE_BASE_ASSETS:
        reasons.append("STABLE_VALUE_BASE_ASSET_EXCLUDED")
    if base in WRAPPED_DUPLICATE_BASE_ASSETS:
        reasons.append("WRAPPED_DUPLICATE_BASE_ASSET_EXCLUDED")
    if base.endswith(LEVERAGED_TOKEN_SUFFIXES):
        reasons.append("LEVERAGED_OR_INVERSE_TOKEN_EXCLUDED")
    if product.price_increment <= 0 or product.quantity_increment <= 0:
        reasons.append("PRECISION_UNUSABLE")
    if product.minimum_quantity is None and product.minimum_notional is None:
        reasons.append("MINIMUM_SIZE_UNAVAILABLE")
    if product.public_volume_24h is None:
        reasons.append("PUBLIC_QUOTE_VOLUME_UNAVAILABLE")
    return tuple(sorted(set(reasons)))


def _history_reasons(
    observation: CryptoProductObservation | None,
    *,
    evidence_class: EvidenceClass,
) -> tuple[str, ...]:
    if observation is None:
        return ("HISTORICAL_OBSERVATION_UNAVAILABLE",)
    reasons: list[str] = []
    if len(observation.candles) < MIN_HISTORY_BUCKETS:
        reasons.append("HISTORICAL_COVERAGE_INSUFFICIENT")
    if observation.missing_buckets > MAX_MISSING_BUCKETS:
        reasons.append("MISSING_DATA_QUALITY_FAILED")
    if observation.rejected_records or observation.duplicate_records:
        reasons.append("MALFORMED_OR_DUPLICATE_DATA")
    if observation.mean_daily_quote_volume < MIN_MEAN_DAILY_QUOTE_VOLUME:
        reasons.append("LIQUIDITY_BELOW_POLICY_MINIMUM")
    if observation.evidence_class is not evidence_class:
        reasons.append("EVIDENCE_CLASS_MISMATCH")
    return tuple(sorted(set(reasons)))


@dataclass(frozen=True, slots=True)
class CryptoRouteSelectionDecision:
    product_id: str
    selected: bool
    reasons: tuple[str, ...]
    public_quote_volume_24h: Decimal
    history_quality: str
    supporting_observation_id: str | None
    provider_alias: str | None
    provider_alias_to: tuple[str, ...]
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class CryptoAssetGroupDecision:
    canonical_asset_id: str
    product_route_ids: tuple[str, ...]
    selected_product_id: str | None
    route_decisions: tuple[CryptoRouteSelectionDecision, ...]
    history_shortlist_rank: int | None
    history_shortlist_reasons: tuple[str, ...]
    provider_declared_aliases: tuple[str, ...]
    canonical_hash: str


def build_asset_groups(
    *,
    products: tuple[PublicCryptoProduct, ...],
    observations: tuple[CryptoProductObservation, ...],
    evidence_class: EvidenceClass,
) -> tuple[CryptoAssetGroupDecision, ...]:
    by_observation = {item.product_id: item for item in observations}
    grouped: dict[str, list[PublicCryptoProduct]] = {}
    for product in products:
        grouped.setdefault(product.canonical_asset_id or product.base_asset, []).append(product)
    provisional: list[tuple[str, PublicCryptoProduct | None, tuple[PublicCryptoProduct, ...]]] = []
    for asset_id, asset_routes in sorted(grouped.items()):
        ordered_routes = tuple(
            sorted(
                asset_routes,
                key=lambda product: (
                    bool(product_route_exclusion_reasons(product)),
                    0
                    if (
                        by_observation.get(product.product_id) is not None
                        and not _history_reasons(
                            by_observation[product.product_id],
                            evidence_class=evidence_class,
                        )
                    )
                    else 1,
                    -(product.public_volume_24h or Decimal(0)),
                    SUPPORTED_QUOTES.index(product.quote_asset)
                    if product.quote_asset in SUPPORTED_QUOTES
                    else len(SUPPORTED_QUOTES),
                    product.product_id,
                ),
            )
        )
        selected = (
            ordered_routes[0]
            if ordered_routes and not product_route_exclusion_reasons(ordered_routes[0])
            else None
        )
        provisional.append((asset_id, selected, ordered_routes))
    shortlist_order = sorted(
        (item for item in provisional if item[1] is not None),
        key=lambda item: (
            -(item[1].public_volume_24h or Decimal(0)) if item[1] is not None else Decimal(0),
            item[0],
            item[1].product_id if item[1] is not None else "",
        ),
    )
    shortlist_ranks = {
        asset_id: rank
        for rank, (asset_id, _, _) in enumerate(
            shortlist_order[:MAX_OBSERVED_PRODUCTS],
            1,
        )
    }
    values: list[CryptoAssetGroupDecision] = []
    for asset_id, selected, ordered_group_routes in provisional:
        route_values: list[CryptoRouteSelectionDecision] = []
        aliases: set[str] = set()
        for route in ordered_group_routes:
            observation = by_observation.get(route.product_id)
            route_reasons = list(product_route_exclusion_reasons(route))
            if route is selected:
                route_reasons.extend(
                    (
                        "SELECTED_CANONICAL_BASE_ASSET_ROUTE",
                        "SELECTED_BY_TRADABILITY_PRECISION_VOLUME_AND_TIE_BREAK",
                    )
                )
            elif not route_reasons:
                route_reasons.extend(
                    (
                        "ALTERNATE_ROUTE_FOR_SAME_CANONICAL_ASSET",
                        "LOWER_DETERMINISTIC_ROUTE_PRIORITY",
                    )
                )
            if route.provider_alias:
                aliases.add(route.provider_alias)
            aliases.update(route.provider_alias_to)
            history_reasons = _history_reasons(observation, evidence_class=evidence_class)
            history_quality = (
                "NOT_CAPTURED"
                if observation is None
                else ("PASSED" if not history_reasons else "FAILED")
            )
            route_payload = (
                route.product_id,
                route is selected,
                tuple(sorted(set(route_reasons))),
                route.public_volume_24h or Decimal(0),
                history_quality,
                observation.observation_id if observation else None,
                route.provider_alias,
                route.provider_alias_to,
            )
            route_values.append(
                CryptoRouteSelectionDecision(
                    product_id=route.product_id,
                    selected=route is selected,
                    reasons=tuple(sorted(set(route_reasons))),
                    public_quote_volume_24h=route.public_volume_24h or Decimal(0),
                    history_quality=history_quality,
                    supporting_observation_id=(observation.observation_id if observation else None),
                    provider_alias=route.provider_alias,
                    provider_alias_to=route.provider_alias_to,
                    canonical_hash=content_digest(route_payload),
                )
            )
        shortlist_rank = shortlist_ranks.get(asset_id)
        shortlist_reasons = (
            ("SELECTED_FOR_HISTORY_BY_PUBLIC_QUOTE_VOLUME",)
            if shortlist_rank is not None
            else (
                ("NO_POLICY_COMPATIBLE_ROUTE",)
                if selected is None
                else ("HISTORY_SHORTLIST_CAP_EXCEEDED",)
            )
        )
        group_payload = (
            asset_id,
            tuple(route.product_id for route in ordered_group_routes),
            selected.product_id if selected else None,
            tuple(route_values),
            shortlist_rank,
            shortlist_reasons,
            tuple(sorted(aliases)),
        )
        values.append(
            CryptoAssetGroupDecision(
                canonical_asset_id=asset_id,
                product_route_ids=tuple(route.product_id for route in ordered_group_routes),
                selected_product_id=selected.product_id if selected else None,
                route_decisions=tuple(route_values),
                history_shortlist_rank=shortlist_rank,
                history_shortlist_reasons=shortlist_reasons,
                provider_declared_aliases=tuple(sorted(aliases)),
                canonical_hash=content_digest(group_payload),
            )
        )
    return tuple(sorted(values, key=lambda item: item.canonical_asset_id))


@dataclass(frozen=True, slots=True)
class CryptoUniverseDecision:
    product_id: str
    status: ProductEligibility
    reasons: tuple[str, ...]
    history_buckets: int
    missing_buckets: int
    mean_daily_quote_volume: Decimal
    rank: int | None
    canonical_hash: str
    canonical_asset_id: str = ""
    representative_product_id: str | None = None


@dataclass(frozen=True, slots=True)
class CryptoUniverseSnapshot:
    snapshot_id: str
    source_capture_id: str
    policy_id: str
    policy_version: int
    considered_product_ids: tuple[str, ...]
    decisions: tuple[CryptoUniverseDecision, ...]
    eligible_product_ids: tuple[str, ...]
    excluded_product_ids: tuple[str, ...]
    evidence_class: EvidenceClass
    evidence_limitations: tuple[str, ...]
    manifest_hash: str
    created_at: datetime
    canonical_hash: str
    catalog_pagination_status: CatalogPaginationStatus = CatalogPaginationStatus.LEGACY_DIAGNOSTIC
    asset_groups: tuple[CryptoAssetGroupDecision, ...] = ()
    history_shortlist_asset_ids: tuple[str, ...] = ()
    eligible_asset_ids: tuple[str, ...] = ()
    selected_routes: tuple[tuple[str, str], ...] = ()
    authority: str = "NONE"


def materialize_universe(
    *,
    snapshot_id: str,
    capture: CryptoProductCapture,
    products: tuple[PublicCryptoProduct, ...],
    observations: tuple[CryptoProductObservation, ...],
    created_at: datetime,
) -> CryptoUniverseSnapshot:
    if not capture.catalog_complete:
        raise CryptoUniverseError("CRYPTO_CATALOG_INCOMPLETE_FOR_PRODUCTION_UNIVERSE")
    if any(item.source_capture_id != capture.capture_id for item in observations):
        raise CryptoUniverseError("CRYPTO_UNIVERSE_MIXED_OBSERVATION_LINEAGE")
    by_observation = {item.product_id: item for item in observations}
    groups = build_asset_groups(
        products=products,
        observations=observations,
        evidence_class=capture.evidence_class,
    )
    group_by_route = {route_id: group for group in groups for route_id in group.product_route_ids}
    preliminary: list[
        tuple[
            PublicCryptoProduct,
            tuple[str, ...],
            CryptoProductObservation | None,
            CryptoAssetGroupDecision,
        ]
    ] = []
    for product in sorted(products, key=lambda item: item.product_id):
        group = group_by_route[product.product_id]
        observation = by_observation.get(product.product_id)
        reasons = list(product_route_exclusion_reasons(product))
        if group.selected_product_id != product.product_id:
            reasons.append("ALTERNATE_ROUTE_NOT_SELECTED")
        elif group.history_shortlist_rank is None:
            reasons.append("HISTORY_SHORTLIST_CAP_EXCEEDED")
        else:
            reasons.extend(_history_reasons(observation, evidence_class=capture.evidence_class))
        preliminary.append((product, tuple(sorted(set(reasons))), observation, group))
    eligible_rows = sorted(
        (item for item in preliminary if not item[1]),
        key=lambda item: (
            -(item[2].mean_daily_quote_volume if item[2] is not None else Decimal(0)),
            item[3].canonical_asset_id,
            item[0].product_id,
        ),
    )
    ranks = {
        product.product_id: index + 1
        for index, (product, _, _, _) in enumerate(eligible_rows[:MAX_ELIGIBLE_PRODUCTS])
    }
    decisions: list[CryptoUniverseDecision] = []
    for product, retained_reasons, observation, group in preliminary:
        final_reasons = list(retained_reasons)
        if not retained_reasons and product.product_id not in ranks:
            final_reasons.append("ELIGIBLE_UNIVERSE_CAP_EXCEEDED")
        status = (
            ProductEligibility.ELIGIBLE
            if product.product_id in ranks
            else ProductEligibility.EXCLUDED
        )
        decision_payload = (
            product.product_id,
            status,
            tuple(final_reasons),
            len(observation.candles) if observation else 0,
            observation.missing_buckets if observation else 0,
            observation.mean_daily_quote_volume if observation else Decimal(0),
            ranks.get(product.product_id),
            group.canonical_asset_id,
            group.selected_product_id,
        )
        decisions.append(
            CryptoUniverseDecision(
                product_id=product.product_id,
                status=status,
                reasons=tuple(final_reasons),
                history_buckets=len(observation.candles) if observation else 0,
                missing_buckets=observation.missing_buckets if observation else 0,
                mean_daily_quote_volume=(
                    observation.mean_daily_quote_volume if observation else Decimal(0)
                ),
                rank=ranks.get(product.product_id),
                canonical_hash=content_digest(decision_payload),
                canonical_asset_id=group.canonical_asset_id,
                representative_product_id=group.selected_product_id,
            )
        )
    ordered_decisions = tuple(sorted(decisions, key=lambda item: item.product_id))
    eligible = tuple(
        product_id for product_id, _ in sorted(ranks.items(), key=lambda item: item[1])
    )
    eligible_assets = tuple(
        next(item.canonical_asset_id for item in ordered_decisions if item.product_id == product_id)
        for product_id in eligible
    )
    if len(set(eligible_assets)) != len(eligible_assets):
        raise CryptoUniverseError("CRYPTO_UNIVERSE_DUPLICATE_CANONICAL_ASSET")
    excluded = tuple(
        item.product_id for item in ordered_decisions if item.status is ProductEligibility.EXCLUDED
    )
    shortlist = tuple(
        item.canonical_asset_id
        for item in sorted(
            (group for group in groups if group.history_shortlist_rank is not None),
            key=lambda item: item.history_shortlist_rank or 0,
        )
    )
    selected_routes = tuple(
        (group.canonical_asset_id, group.selected_product_id)
        for group in groups
        if group.selected_product_id is not None
    )
    manifest = content_digest(
        (
            capture.canonical_hash,
            CRYPTO_UNIVERSE_POLICY_V2,
            1,
            tuple(item.canonical_hash for item in groups),
            tuple(item.canonical_hash for item in ordered_decisions),
        )
    )
    timestamp = _time(created_at, "CRYPTO_UNIVERSE_CREATION_TIME_INVALID")
    snapshot_payload = (
        _identity(snapshot_id, "CRYPTO_UNIVERSE_SNAPSHOT_ID_INVALID"),
        capture.capture_id,
        CRYPTO_UNIVERSE_POLICY_V2,
        1,
        tuple(sorted(capture.product_ids)),
        ordered_decisions,
        eligible,
        excluded,
        capture.evidence_class,
        tuple(sorted(set(capture.limitations))),
        manifest,
        timestamp,
        capture.pagination_status,
        groups,
        shortlist,
        eligible_assets,
        selected_routes,
        "NONE",
    )
    return CryptoUniverseSnapshot(
        snapshot_id=_identity(snapshot_id, "CRYPTO_UNIVERSE_SNAPSHOT_ID_INVALID"),
        source_capture_id=capture.capture_id,
        policy_id=CRYPTO_UNIVERSE_POLICY_V2,
        policy_version=1,
        considered_product_ids=tuple(sorted(capture.product_ids)),
        decisions=ordered_decisions,
        eligible_product_ids=eligible,
        excluded_product_ids=excluded,
        evidence_class=capture.evidence_class,
        evidence_limitations=tuple(sorted(set(capture.limitations))),
        manifest_hash=manifest,
        created_at=timestamp,
        canonical_hash=content_digest(snapshot_payload),
        catalog_pagination_status=capture.pagination_status,
        asset_groups=groups,
        history_shortlist_asset_ids=shortlist,
        eligible_asset_ids=eligible_assets,
        selected_routes=selected_routes,
    )


@dataclass(frozen=True, slots=True)
class CryptoCalibrationBucket:
    bucket: str
    sample_count: int
    expected_gross_return: Decimal | None
    uncertainty_lower: Decimal | None
    uncertainty_upper: Decimal | None
    canonical_hash: str
    modeled_round_trip_cost: Decimal = Decimal("0.014")
    expected_net_return: Decimal | None = None
    net_uncertainty_lower: Decimal | None = None
    net_uncertainty_upper: Decimal | None = None
    horizon_results: tuple[CryptoCalibrationHorizon, ...] = ()


@dataclass(frozen=True, slots=True)
class CryptoCalibrationHorizon:
    outcome_days: int
    sample_count: int
    expected_gross_return: Decimal | None
    expected_net_return: Decimal | None
    uncertainty_lower: Decimal | None
    uncertainty_upper: Decimal | None
    downside_probability: Decimal | None
    downside_p05: Decimal | None
    maximum_observed_loss: Decimal | None
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class CryptoCalibrationAssetCoverage:
    canonical_asset_id: str
    product_id: str
    observation_id: str
    requested_start: datetime
    requested_end: datetime
    accepted_buckets: int
    missing_buckets: int
    duplicate_records: int
    rejected_records: int
    request_count: int
    coverage_status: str
    limitations: tuple[str, ...]
    raw_evidence_hashes: tuple[str, ...]
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class CryptoCalibrationDecisionOutcome:
    product_id: str
    decision_at: datetime
    outcome_at: datetime
    rank: int
    bucket: str
    trailing_return_30d: Decimal
    trailing_return_90d: Decimal
    universe_median_90d: Decimal
    relative_strength: Decimal
    reversal_penalty: Decimal
    volatility_penalty: Decimal
    gross_research_score: Decimal
    forward_gross_return: Decimal
    included_in_bucket: bool
    exclusion_reasons: tuple[str, ...]
    canonical_hash: str
    trailing_return_7d: Decimal | None = None
    forward_gross_return_14d: Decimal | None = None
    forward_gross_return_30d: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CryptoRelativeStrengthCalibration:
    calibration_id: str
    method_id: str
    version: int
    dataset_start: datetime
    dataset_end: datetime
    decision_spacing_days: int
    lookback_days: int
    outcome_days: int
    observation_ids: tuple[str, ...]
    buckets: tuple[CryptoCalibrationBucket, ...]
    formula_hash: str
    canonical_hash: str
    authority: str = "NONE"
    asset_coverage: tuple[CryptoCalibrationAssetCoverage, ...] = ()
    usable_common_dates: tuple[datetime, ...] = ()
    decision_outcomes: tuple[CryptoCalibrationDecisionOutcome, ...] = ()
    outcome_horizons: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class CryptoCurrentMarketEvidence:
    product_id: str
    checkpoint_id: str
    observed_at: datetime
    product_status: str
    spread_bps: Decimal | None
    visible_ask_depth: Decimal | None
    required_capital: Decimal | None
    maximum_loss: Decimal | None
    maximum_sensible_allocation: Decimal | None
    evidence_complete: bool
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class CryptoDiscoveryResult:
    product_id: str
    rank: int
    trailing_return: Decimal
    realized_volatility: Decimal
    risk_adjusted_return: Decimal
    mean_daily_quote_volume: Decimal
    estimated_round_trip_cost: Decimal
    liquidity_penalty: Decimal
    gross_research_score: Decimal
    supporting_observation_id: str
    canonical_hash: str
    canonical_asset_id: str = ""
    alternate_product_route_ids: tuple[str, ...] = ()
    trailing_return_30d: Decimal | None = None
    trailing_return_90d: Decimal | None = None
    universe_median_90d: Decimal | None = None
    relative_strength: Decimal | None = None
    reversal_penalty: Decimal | None = None
    volatility_penalty: Decimal | None = None
    spread_penalty: Decimal | None = None
    depth_penalty: Decimal | None = None
    calibration_bucket: str | None = None
    expected_gross_return: Decimal | None = None
    expected_net_return: Decimal | None = None
    uncertainty_lower: Decimal | None = None
    uncertainty_upper: Decimal | None = None
    calibration_sample_count: int | None = None
    source_eligible: bool = False
    source_rejection_reasons: tuple[str, ...] = ()
    current_spread_bps: Decimal | None = None
    visible_ask_depth: Decimal | None = None
    required_capital: Decimal | None = None
    maximum_loss: Decimal | None = None
    maximum_sensible_allocation: Decimal | None = None
    supporting_checkpoint_id: str | None = None
    trailing_return_7d: Decimal | None = None
    downside_probability: Decimal | None = None
    downside_p05: Decimal | None = None
    maximum_observed_loss: Decimal | None = None
    calibration_horizons: tuple[CryptoCalibrationHorizon, ...] = ()


@dataclass(frozen=True, slots=True)
class CryptoDiscoveryRun:
    run_id: str
    universe_snapshot_id: str
    method_id: str
    method_version: int
    generated_at: datetime
    results: tuple[CryptoDiscoveryResult, ...]
    warnings: tuple[str, ...]
    result_hash: str
    canonical_hash: str
    source_pulse_id: str | None = None
    calibration: CryptoRelativeStrengthCalibration | None = None
    authority: str = "NONE"


def _annualized_volatility(candles: tuple[CryptoDailyObservation, ...]) -> Decimal:
    closes = tuple(item.close for item in candles[-91:])
    returns = tuple(
        closes[index] / closes[index - 1] - Decimal(1) for index in range(1, len(closes))
    )
    if len(returns) != 90:
        raise CryptoUniverseError("CRYPTO_DISCOVERY_HISTORY_INSUFFICIENT")
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        mean = sum(returns, Decimal(0)) / Decimal(len(returns))
        variance = sum(((item - mean) ** 2 for item in returns), Decimal(0)) / Decimal(
            len(returns) - 1
        )
        return metric(variance.sqrt() * Decimal(365).sqrt())


def run_discovery(
    *,
    run_id: str,
    snapshot: CryptoUniverseSnapshot,
    observations: tuple[CryptoProductObservation, ...],
    generated_at: datetime,
) -> CryptoDiscoveryRun:
    if any(item.source_capture_id != snapshot.source_capture_id for item in observations):
        raise CryptoUniverseError("CRYPTO_DISCOVERY_MIXED_OBSERVATION_LINEAGE")
    by_product = {item.product_id: item for item in observations}
    group_by_product = {
        group.selected_product_id: group
        for group in snapshot.asset_groups
        if group.selected_product_id is not None
    }
    rows: list[
        tuple[
            str,
            str,
            tuple[str, ...],
            Decimal,
            Decimal,
            Decimal,
            Decimal,
            Decimal,
            str,
        ]
    ] = []
    round_trip_cost = metric(
        Decimal(2) * (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / Decimal(10000)
    )
    for product_id in snapshot.eligible_product_ids:
        observation = by_product.get(product_id)
        if observation is None or len(observation.candles) < 91:
            raise CryptoUniverseError("CRYPTO_DISCOVERY_REQUIRED_HISTORY_UNAVAILABLE")
        group = group_by_product.get(product_id)
        canonical_asset_id = (
            group.canonical_asset_id
            if group is not None
            else (observation.canonical_asset_id or product_id.rsplit("-", 1)[0])
        )
        alternates = (
            tuple(route_id for route_id in group.product_route_ids if route_id != product_id)
            if group is not None
            else ()
        )
        recent = observation.candles[-91:]
        trailing = metric(recent[-1].close / recent[0].close - Decimal(1))
        volatility = _annualized_volatility(recent)
        adjusted = metric(trailing / max(volatility, METRIC_QUANTUM))
        volume = observation.mean_daily_quote_volume
        liquidity_penalty = metric(
            max(Decimal(0), MIN_MEAN_DAILY_QUOTE_VOLUME / volume - Decimal(1)) * Decimal("0.01")
        )
        score = metric(
            trailing - Decimal("0.25") * volatility - round_trip_cost - liquidity_penalty
        )
        rows.append(
            (
                canonical_asset_id,
                product_id,
                alternates,
                trailing,
                volatility,
                adjusted,
                volume,
                liquidity_penalty,
                observation.observation_id,
            )
        )
    rows.sort(
        key=lambda item: (
            -item[3] + Decimal("0.25") * item[4] + item[7],
            item[0],
            item[1],
        )
    )
    results: list[CryptoDiscoveryResult] = []
    for rank, row in enumerate(rows, 1):
        (
            canonical_asset_id,
            product_id,
            alternates,
            trailing,
            volatility,
            adjusted,
            volume,
            penalty,
            observation_id,
        ) = row
        score = metric(trailing - Decimal("0.25") * volatility - round_trip_cost - penalty)
        result_payload = (
            product_id,
            rank,
            trailing,
            volatility,
            adjusted,
            volume,
            round_trip_cost,
            penalty,
            score,
            observation_id,
            canonical_asset_id,
            alternates,
        )
        results.append(
            CryptoDiscoveryResult(
                product_id=product_id,
                rank=rank,
                trailing_return=trailing,
                realized_volatility=volatility,
                risk_adjusted_return=adjusted,
                mean_daily_quote_volume=volume,
                estimated_round_trip_cost=round_trip_cost,
                liquidity_penalty=penalty,
                gross_research_score=score,
                supporting_observation_id=observation_id,
                canonical_hash=content_digest(result_payload),
                canonical_asset_id=canonical_asset_id,
                alternate_product_route_ids=alternates,
            )
        )
    if len({item.canonical_asset_id for item in results}) != len(results):
        raise CryptoUniverseError("CRYPTO_DISCOVERY_DUPLICATE_CANONICAL_ASSET")
    result_hash = content_digest(tuple(item.canonical_hash for item in results))
    run_payload = (
        _identity(run_id, "CRYPTO_DISCOVERY_RUN_ID_INVALID"),
        snapshot.snapshot_id,
        CRYPTO_DISCOVERY_V1,
        1,
        _time(generated_at, "CRYPTO_DISCOVERY_TIME_INVALID"),
        tuple(results),
        (
            "DISCOVERY_RANKING_IS_NOT_A_VALIDATED_STRATEGY",
            "HISTORICAL_PUBLIC_EVIDENCE_MAY_BE_PROVIDER_REVISED",
            CRYPTO_UNIVERSE_WARNING,
        ),
        result_hash,
        "NONE",
    )
    return CryptoDiscoveryRun(
        run_id=_identity(run_id, "CRYPTO_DISCOVERY_RUN_ID_INVALID"),
        universe_snapshot_id=snapshot.snapshot_id,
        method_id=CRYPTO_DISCOVERY_V1,
        method_version=1,
        generated_at=_time(generated_at, "CRYPTO_DISCOVERY_TIME_INVALID"),
        results=tuple(results),
        warnings=(
            "DISCOVERY_RANKING_IS_NOT_A_VALIDATED_STRATEGY",
            "HISTORICAL_PUBLIC_EVIDENCE_MAY_BE_PROVIDER_REVISED",
            CRYPTO_UNIVERSE_WARNING,
        ),
        result_hash=result_hash,
        canonical_hash=content_digest(run_payload),
    )


def _window_volatility(closes: tuple[Decimal, ...]) -> Decimal:
    if len(closes) < 2:
        raise CryptoUniverseError("CRYPTO_RELATIVE_STRENGTH_VOLATILITY_INSUFFICIENT")
    returns = tuple(
        closes[index] / closes[index - 1] - Decimal(1) for index in range(1, len(closes))
    )
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        mean = sum(returns, Decimal(0)) / Decimal(len(returns))
        variance = sum(((value - mean) ** 2 for value in returns), Decimal(0)) / Decimal(
            len(returns) - 1
        )
        return metric(variance.sqrt() * Decimal(365).sqrt())


def _median(values: tuple[Decimal, ...]) -> Decimal:
    ordered = tuple(sorted(values))
    if not ordered:
        raise CryptoUniverseError("CRYPTO_RELATIVE_STRENGTH_UNIVERSE_EMPTY")
    middle = len(ordered) // 2
    return metric(
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / Decimal(2)
    )


def _relative_score(
    *,
    return_30d: Decimal,
    return_90d: Decimal,
    median_90d: Decimal,
    volatility_30d: Decimal,
    spread_penalty: Decimal,
    depth_penalty: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    relative = metric(return_90d - median_90d)
    reversal = metric(max(Decimal(0), -return_30d) * Decimal("0.50"))
    volatility_penalty = metric(volatility_30d * Decimal("0.15"))
    round_trip_cost = metric(
        Decimal(2) * (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / Decimal(10000)
    )
    score = metric(
        relative
        + Decimal("0.25") * return_30d
        - reversal
        - volatility_penalty
        - spread_penalty
        - depth_penalty
        - round_trip_cost
    )
    return score, reversal, volatility_penalty


def _rank_bucket(rank: int, count: int) -> str:
    top = max(1, count // 3)
    if rank <= top:
        return "TOP_THIRD"
    if rank <= top * 2:
        return "MIDDLE_THIRD"
    return "BOTTOM_THIRD"


def _calibrate_relative_strength(
    *,
    calibration_id: str,
    observations: tuple[CryptoProductObservation, ...],
    formula_hash: str,
) -> CryptoRelativeStrengthCalibration:
    by_product = {item.product_id: item for item in observations}
    if len(by_product) != len(observations) or len(by_product) < 3:
        raise CryptoUniverseError("CRYPTO_RELATIVE_STRENGTH_CALIBRATION_UNIVERSE_INVALID")
    candle_maps = {
        product_id: {item.bucket_start: item.close for item in observation.candles}
        for product_id, observation in by_product.items()
    }
    common_dates = tuple(sorted(set.intersection(*(set(value) for value in candle_maps.values()))))
    if len(common_dates) < 121:
        raise CryptoUniverseError("CRYPTO_RELATIVE_STRENGTH_CALIBRATION_HISTORY_INSUFFICIENT")
    outcomes: dict[str, list[Decimal]] = {
        "TOP_THIRD": [],
        "MIDDLE_THIRD": [],
        "BOTTOM_THIRD": [],
    }
    decisions: list[CryptoCalibrationDecisionOutcome] = []
    product_ids = tuple(sorted(by_product))
    round_trip_cost = metric(
        Decimal(2) * (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / Decimal(10000)
    )
    for index in range(90, len(common_dates) - RELATIVE_STRENGTH_HOLDING_DAYS, 5):
        date = common_dates[index]
        returns_90 = {
            product_id: metric(
                candle_maps[product_id][date] / candle_maps[product_id][common_dates[index - 90]]
                - Decimal(1)
            )
            for product_id in product_ids
        }
        median_90 = _median(tuple(returns_90.values()))
        rows: list[tuple[str, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]] = []
        for product_id in product_ids:
            return_30 = metric(
                candle_maps[product_id][date] / candle_maps[product_id][common_dates[index - 30]]
                - Decimal(1)
            )
            volatility = _window_volatility(
                tuple(
                    candle_maps[product_id][value] for value in common_dates[index - 30 : index + 1]
                )
            )
            score, reversal, volatility_penalty = _relative_score(
                return_30d=return_30,
                return_90d=returns_90[product_id],
                median_90d=median_90,
                volatility_30d=volatility,
                spread_penalty=Decimal(0),
                depth_penalty=Decimal(0),
            )
            rows.append(
                (
                    product_id,
                    score,
                    returns_90[product_id],
                    return_30,
                    median_90,
                    reversal,
                    volatility_penalty,
                )
            )
        rows.sort(key=lambda value: (-value[1], value[0]))
        outcome_date = common_dates[index + RELATIVE_STRENGTH_HOLDING_DAYS]
        for rank, (
            product_id,
            score,
            absolute_return,
            return_30,
            median_90,
            reversal,
            volatility_penalty,
        ) in enumerate(rows, 1):
            gross = metric(
                candle_maps[product_id][outcome_date] / candle_maps[product_id][date] - Decimal(1)
            )
            bucket = _rank_bucket(rank, len(rows))
            included = absolute_return > 0
            exclusion_reasons = () if included else ("ABSOLUTE_90_DAY_RETURN_NOT_POSITIVE",)
            decision_payload = (
                product_id,
                date,
                outcome_date,
                rank,
                bucket,
                return_30,
                absolute_return,
                median_90,
                metric(absolute_return - median_90),
                reversal,
                volatility_penalty,
                score,
                gross,
                included,
                exclusion_reasons,
            )
            decisions.append(
                CryptoCalibrationDecisionOutcome(
                    product_id=product_id,
                    decision_at=date,
                    outcome_at=outcome_date,
                    rank=rank,
                    bucket=bucket,
                    trailing_return_30d=return_30,
                    trailing_return_90d=absolute_return,
                    universe_median_90d=median_90,
                    relative_strength=metric(absolute_return - median_90),
                    reversal_penalty=reversal,
                    volatility_penalty=volatility_penalty,
                    gross_research_score=score,
                    forward_gross_return=gross,
                    included_in_bucket=included,
                    exclusion_reasons=exclusion_reasons,
                    canonical_hash=content_digest(decision_payload),
                )
            )
            if included:
                outcomes[bucket].append(gross)
    buckets: list[CryptoCalibrationBucket] = []
    for bucket in ("TOP_THIRD", "MIDDLE_THIRD", "BOTTOM_THIRD"):
        values = tuple(outcomes[bucket])
        expected: Decimal | None = None
        lower: Decimal | None = None
        upper: Decimal | None = None
        expected_net: Decimal | None = None
        net_lower: Decimal | None = None
        net_upper: Decimal | None = None
        if len(values) >= RELATIVE_STRENGTH_MINIMUM_SAMPLES:
            with localcontext() as context:
                context.prec = 50
                context.rounding = ROUND_HALF_EVEN
                mean = sum(values, Decimal(0)) / Decimal(len(values))
                variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(
                    len(values) - 1
                )
                standard_error = variance.sqrt() / Decimal(len(values)).sqrt()
                width = Decimal("1.96") * standard_error
                expected = metric(mean)
                lower = metric(mean - width)
                upper = metric(mean + width)
                expected_net = metric(expected - round_trip_cost)
                net_lower = metric(lower - round_trip_cost)
                net_upper = metric(upper - round_trip_cost)
        bucket_payload = (
            bucket,
            len(values),
            expected,
            lower,
            upper,
            round_trip_cost,
            expected_net,
            net_lower,
            net_upper,
        )
        buckets.append(
            CryptoCalibrationBucket(
                bucket=bucket,
                sample_count=len(values),
                expected_gross_return=expected,
                uncertainty_lower=lower,
                uncertainty_upper=upper,
                canonical_hash=content_digest(bucket_payload),
                modeled_round_trip_cost=round_trip_cost,
                expected_net_return=expected_net,
                net_uncertainty_lower=net_lower,
                net_uncertainty_upper=net_upper,
            )
        )
    coverage = tuple(
        CryptoCalibrationAssetCoverage(
            canonical_asset_id=(
                by_product[product_id].canonical_asset_id or product_id.rsplit("-", 1)[0]
            ),
            product_id=product_id,
            observation_id=by_product[product_id].observation_id,
            requested_start=by_product[product_id].interval_start,
            requested_end=by_product[product_id].interval_end,
            accepted_buckets=len(by_product[product_id].candles),
            missing_buckets=by_product[product_id].missing_buckets,
            duplicate_records=by_product[product_id].duplicate_records,
            rejected_records=by_product[product_id].rejected_records,
            request_count=by_product[product_id].request_count,
            coverage_status=by_product[product_id].coverage_status,
            limitations=by_product[product_id].coverage_limitations,
            raw_evidence_hashes=by_product[product_id].raw_evidence_hashes,
            canonical_hash=content_digest(
                (
                    product_id,
                    by_product[product_id].observation_id,
                    by_product[product_id].interval_start,
                    by_product[product_id].interval_end,
                    len(by_product[product_id].candles),
                    by_product[product_id].missing_buckets,
                    by_product[product_id].duplicate_records,
                    by_product[product_id].rejected_records,
                    by_product[product_id].request_count,
                    by_product[product_id].coverage_status,
                    by_product[product_id].coverage_limitations,
                    by_product[product_id].raw_evidence_hashes,
                )
            ),
        )
        for product_id in product_ids
    )
    calibration_payload = (
        calibration_id,
        CRYPTO_RELATIVE_STRENGTH_CALIBRATION_V1,
        1,
        common_dates[0],
        common_dates[-1],
        5,
        90,
        RELATIVE_STRENGTH_HOLDING_DAYS,
        tuple(by_product[product_id].observation_id for product_id in product_ids),
        tuple(buckets),
        formula_hash,
        round_trip_cost,
        coverage,
        common_dates,
        tuple(decisions),
        "NONE",
    )
    return CryptoRelativeStrengthCalibration(
        calibration_id=calibration_id,
        method_id=CRYPTO_RELATIVE_STRENGTH_CALIBRATION_V1,
        version=1,
        dataset_start=common_dates[0],
        dataset_end=common_dates[-1],
        decision_spacing_days=5,
        lookback_days=90,
        outcome_days=RELATIVE_STRENGTH_HOLDING_DAYS,
        observation_ids=tuple(by_product[product_id].observation_id for product_id in product_ids),
        buckets=tuple(buckets),
        formula_hash=formula_hash,
        canonical_hash=content_digest(calibration_payload),
        asset_coverage=coverage,
        usable_common_dates=common_dates,
        decision_outcomes=tuple(decisions),
    )


def run_relative_strength(
    *,
    run_id: str,
    source_pulse_id: str,
    snapshot: CryptoUniverseSnapshot,
    observations: tuple[CryptoProductObservation, ...],
    current_market: tuple[CryptoCurrentMarketEvidence, ...],
    generated_at: datetime,
    calibration_observations: tuple[CryptoProductObservation, ...] | None = None,
) -> CryptoDiscoveryRun:
    """Run a fixed current multi-asset relative-strength method and prior-only calibration."""

    observation_by_product = {item.product_id: item for item in observations}
    market_by_product = {item.product_id: item for item in current_market}
    product_ids = tuple(snapshot.eligible_product_ids)
    if (
        len(product_ids) < 3
        or set(observation_by_product) != set(product_ids)
        or set(market_by_product) != set(product_ids)
    ):
        raise CryptoUniverseError("CRYPTO_RELATIVE_STRENGTH_INPUT_LINEAGE_INCOMPLETE")
    formula_hash = content_digest(
        (
            CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1,
            "RETURN_90_MINUS_UNIVERSE_MEDIAN",
            "PLUS_0.25_RETURN_30",
            "MINUS_0.50_NEGATIVE_RETURN_30",
            "MINUS_0.15_VOLATILITY_30",
            "MINUS_CURRENT_SPREAD",
            "MINUS_ROUND_TRIP_FEES_AND_SLIPPAGE",
            "MINUS_DEPTH_SHORTFALL",
            RELATIVE_STRENGTH_MINIMUM_SAMPLES,
        )
    )
    calibration_values = (
        observations if calibration_observations is None else calibration_observations
    )
    if {item.product_id for item in calibration_values} != set(product_ids) or any(
        item.evidence_class is not snapshot.evidence_class
        or item.source_capture_id != snapshot.source_capture_id
        for item in calibration_values
    ):
        raise CryptoUniverseError("CRYPTO_RELATIVE_STRENGTH_CALIBRATION_LINEAGE_INVALID")
    calibration = _calibrate_relative_strength(
        calibration_id=f"{run_id}-calibration",
        observations=calibration_values,
        formula_hash=formula_hash,
    )
    generated = _time(generated_at, "CRYPTO_RELATIVE_STRENGTH_TIME_INVALID")
    if calibration.dataset_end >= generated:
        raise CryptoUniverseError("CRYPTO_RELATIVE_STRENGTH_CALIBRATION_LEAKAGE")
    calibration_by_bucket = {item.bucket: item for item in calibration.buckets}
    raw_rows: list[
        tuple[str, str, tuple[str, ...], Decimal, Decimal, Decimal, Decimal, Decimal, str]
    ] = []
    returns_90: dict[str, Decimal] = {}
    returns_30: dict[str, Decimal] = {}
    volatilities: dict[str, Decimal] = {}
    for product_id in product_ids:
        observation = observation_by_product[product_id]
        candles = observation.candles
        if len(candles) < 91:
            raise CryptoUniverseError("CRYPTO_RELATIVE_STRENGTH_HISTORY_INSUFFICIENT")
        returns_30[product_id] = metric(candles[-1].close / candles[-31].close - Decimal(1))
        returns_90[product_id] = metric(candles[-1].close / candles[-91].close - Decimal(1))
        volatilities[product_id] = _window_volatility(tuple(item.close for item in candles[-31:]))
    median_90 = _median(tuple(returns_90.values()))
    for product_id in product_ids:
        observation = observation_by_product[product_id]
        market = market_by_product[product_id]
        spread_penalty = metric(
            Decimal(0) if market.spread_bps is None else market.spread_bps / Decimal(10000)
        )
        depth_penalty = metric(
            Decimal("0.05")
            if market.visible_ask_depth is None
            else max(
                Decimal(0),
                RELATIVE_STRENGTH_MINIMUM_DEPTH - market.visible_ask_depth,
            )
            / RELATIVE_STRENGTH_MINIMUM_DEPTH
            * Decimal("0.05")
        )
        score, reversal, volatility_penalty = _relative_score(
            return_30d=returns_30[product_id],
            return_90d=returns_90[product_id],
            median_90d=median_90,
            volatility_30d=volatilities[product_id],
            spread_penalty=spread_penalty,
            depth_penalty=depth_penalty,
        )
        raw_rows.append(
            (
                observation.canonical_asset_id or product_id.rsplit("-", 1)[0],
                product_id,
                (),
                score,
                reversal,
                volatility_penalty,
                spread_penalty,
                depth_penalty,
                observation.observation_id,
            )
        )
    raw_rows.sort(key=lambda value: (-value[3], value[0], value[1]))
    results: list[CryptoDiscoveryResult] = []
    round_trip_cost = metric(
        Decimal(2) * (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / Decimal(10000)
    )
    for rank, row in enumerate(raw_rows, 1):
        asset_id, product_id, alternates, score, reversal, vol_penalty, spread, depth, obs_id = row
        market = market_by_product[product_id]
        bucket_name = _rank_bucket(rank, len(raw_rows))
        bucket = calibration_by_bucket[bucket_name]
        gross = bucket.expected_gross_return
        net = None if gross is None else metric(gross - round_trip_cost - spread)
        lower = (
            None
            if bucket.uncertainty_lower is None
            else metric(bucket.uncertainty_lower - round_trip_cost - spread)
        )
        upper = (
            None
            if bucket.uncertainty_upper is None
            else metric(bucket.uncertainty_upper - round_trip_cost - spread)
        )
        reasons: list[str] = []
        if returns_90[product_id] <= 0:
            reasons.append("ABSOLUTE_90_DAY_RETURN_NOT_POSITIVE")
        if returns_90[product_id] <= median_90:
            reasons.append("NOT_ABOVE_ELIGIBLE_UNIVERSE_MEDIAN")
        if bucket_name != "TOP_THIRD":
            reasons.append("OUTSIDE_TOP_RELATIVE_STRENGTH_BUCKET")
        if not market.evidence_complete:
            reasons.append("CURRENT_MARKET_EVIDENCE_INCOMPLETE")
        if market.product_status != "ACTIVE/ONLINE":
            reasons.append("CURRENT_PRODUCT_STATUS_INELIGIBLE")
        if (
            int((generated - market.observed_at).total_seconds())
            > RELATIVE_STRENGTH_FRESHNESS_SECONDS
        ):
            reasons.append("CURRENT_MARKET_EVIDENCE_STALE")
        if (
            market.required_capital is None
            or market.visible_ask_depth is None
            or market.visible_ask_depth < market.required_capital
        ):
            reasons.append("CURRENT_VISIBLE_DEPTH_INSUFFICIENT")
        if bucket.sample_count < RELATIVE_STRENGTH_MINIMUM_SAMPLES or gross is None:
            reasons.append("CALIBRATION_SAMPLE_INSUFFICIENT")
        if net is None or net <= 0:
            reasons.append("EXPECTED_NET_RETURN_NOT_POSITIVE")
        if lower is None or lower <= 0:
            reasons.append("UNCERTAINTY_LOWER_BOUND_NOT_POSITIVE")
        eligible = not reasons
        result_payload = (
            product_id,
            rank,
            returns_30[product_id],
            returns_90[product_id],
            median_90,
            metric(returns_90[product_id] - median_90),
            volatilities[product_id],
            reversal,
            vol_penalty,
            spread,
            depth,
            score,
            bucket_name,
            gross,
            net,
            lower,
            upper,
            bucket.sample_count,
            eligible,
            tuple(reasons),
            market,
            obs_id,
            asset_id,
        )
        results.append(
            CryptoDiscoveryResult(
                product_id=product_id,
                rank=rank,
                trailing_return=returns_90[product_id],
                realized_volatility=volatilities[product_id],
                risk_adjusted_return=metric(
                    (returns_90[product_id] - median_90)
                    / max(volatilities[product_id], METRIC_QUANTUM)
                ),
                mean_daily_quote_volume=observation_by_product[product_id].mean_daily_quote_volume,
                estimated_round_trip_cost=metric(round_trip_cost + spread),
                liquidity_penalty=depth,
                gross_research_score=score,
                supporting_observation_id=obs_id,
                canonical_hash=content_digest(result_payload),
                canonical_asset_id=asset_id,
                alternate_product_route_ids=alternates,
                trailing_return_30d=returns_30[product_id],
                trailing_return_90d=returns_90[product_id],
                universe_median_90d=median_90,
                relative_strength=metric(returns_90[product_id] - median_90),
                reversal_penalty=reversal,
                volatility_penalty=vol_penalty,
                spread_penalty=spread,
                depth_penalty=depth,
                calibration_bucket=bucket_name,
                expected_gross_return=gross,
                expected_net_return=net,
                uncertainty_lower=lower,
                uncertainty_upper=upper,
                calibration_sample_count=bucket.sample_count,
                source_eligible=eligible,
                source_rejection_reasons=tuple(reasons),
                current_spread_bps=market.spread_bps,
                visible_ask_depth=market.visible_ask_depth,
                required_capital=market.required_capital,
                maximum_loss=market.maximum_loss,
                maximum_sensible_allocation=market.maximum_sensible_allocation,
                supporting_checkpoint_id=market.checkpoint_id,
            )
        )
    result_hash = content_digest(tuple(item.canonical_hash for item in results))
    run_payload = (
        _identity(run_id, "CRYPTO_RELATIVE_STRENGTH_RUN_ID_INVALID"),
        snapshot.snapshot_id,
        CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1,
        1,
        generated,
        tuple(results),
        calibration,
        source_pulse_id,
        result_hash,
        "NONE",
    )
    return CryptoDiscoveryRun(
        run_id=run_id,
        universe_snapshot_id=snapshot.snapshot_id,
        method_id=CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1,
        method_version=1,
        generated_at=generated,
        results=tuple(results),
        warnings=(
            "EXPECTED_RETURN_USES_STRICTLY_PRIOR_WALK_FORWARD_BUCKET_OUTCOMES",
            "OVERLAPPING_30_DAY_OUTCOMES_LIMIT_INDEPENDENCE",
            "DISPLAYED_PUBLIC_DEPTH_IS_NOT_FILLABILITY",
            CRYPTO_UNIVERSE_WARNING,
        ),
        result_hash=result_hash,
        canonical_hash=content_digest(run_payload),
        source_pulse_id=source_pulse_id,
        calibration=calibration,
    )


def reversal_regime_score(
    *,
    return_7d: Decimal,
    return_90d: Decimal,
    volatility_30d: Decimal,
    spread_penalty: Decimal,
    depth_penalty: Decimal,
    modeled_round_trip_cost: Decimal,
) -> Decimal:
    """The frozen v1 reversal-within-positive-regime score."""

    return metric(
        -return_7d
        + Decimal("0.50") * return_90d
        - Decimal("0.15") * volatility_30d
        - spread_penalty
        - depth_penalty
        - modeled_round_trip_cost
    )


def _reversal_horizon(
    *, outcome_days: int, values: tuple[Decimal, ...], round_trip_cost: Decimal
) -> CryptoCalibrationHorizon:
    expected_gross: Decimal | None = None
    expected_net: Decimal | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None
    downside_probability: Decimal | None = None
    downside_p05: Decimal | None = None
    maximum_observed_loss: Decimal | None = None
    if len(values) >= RELATIVE_STRENGTH_MINIMUM_SAMPLES:
        with localcontext() as context:
            context.prec = 50
            context.rounding = ROUND_HALF_EVEN
            mean = sum(values, Decimal(0)) / Decimal(len(values))
            variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(
                len(values) - 1
            )
            width = Decimal("1.96") * variance.sqrt() / Decimal(len(values)).sqrt()
            expected_gross = metric(mean)
            expected_net = metric(mean - round_trip_cost)
            lower = metric(mean - width - round_trip_cost)
            upper = metric(mean + width - round_trip_cost)
            downside_probability = metric(
                Decimal(sum(value < 0 for value in values)) / Decimal(len(values))
            )
            ordered = tuple(sorted(values))
            downside_p05 = ordered[((len(ordered) - 1) * 5) // 100]
            maximum_observed_loss = ordered[0]
    payload = (
        outcome_days,
        len(values),
        expected_gross,
        expected_net,
        lower,
        upper,
        downside_probability,
        downside_p05,
        maximum_observed_loss,
    )
    return CryptoCalibrationHorizon(
        outcome_days=outcome_days,
        sample_count=len(values),
        expected_gross_return=expected_gross,
        expected_net_return=expected_net,
        uncertainty_lower=lower,
        uncertainty_upper=upper,
        downside_probability=downside_probability,
        downside_p05=downside_p05,
        maximum_observed_loss=maximum_observed_loss,
        canonical_hash=content_digest(payload),
    )


def _calibrate_reversal_regime(
    *,
    calibration_id: str,
    observations: tuple[CryptoProductObservation, ...],
    formula_hash: str,
) -> CryptoRelativeStrengthCalibration:
    by_product = {item.product_id: item for item in observations}
    if len(by_product) != len(observations) or len(by_product) < 3:
        raise CryptoUniverseError("CRYPTO_REVERSAL_REGIME_CALIBRATION_UNIVERSE_INVALID")
    candle_maps = {
        product_id: {item.bucket_start: item.close for item in observation.candles}
        for product_id, observation in by_product.items()
    }
    common_dates = tuple(sorted(set.intersection(*(set(value) for value in candle_maps.values()))))
    if len(common_dates) < 121:
        raise CryptoUniverseError("CRYPTO_REVERSAL_REGIME_CALIBRATION_HISTORY_INSUFFICIENT")
    product_ids = tuple(sorted(by_product))
    round_trip_cost = metric(
        Decimal(2) * (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / Decimal(10000)
    )
    outcomes: dict[str, dict[int, list[Decimal]]] = {
        bucket: {
            REVERSAL_REGIME_PRIMARY_HOLDING_DAYS: [],
            REVERSAL_REGIME_SECONDARY_HOLDING_DAYS: [],
        }
        for bucket in ("TOP_THIRD", "MIDDLE_THIRD", "BOTTOM_THIRD")
    }
    decisions: list[CryptoCalibrationDecisionOutcome] = []
    for index in range(90, len(common_dates) - REVERSAL_REGIME_SECONDARY_HOLDING_DAYS, 5):
        decision_at = common_dates[index]
        rows: list[tuple[str, Decimal, Decimal, Decimal, Decimal, Decimal]] = []
        for product_id in product_ids:
            prices = candle_maps[product_id]
            return_7 = metric(prices[decision_at] / prices[common_dates[index - 7]] - Decimal(1))
            return_30 = metric(prices[decision_at] / prices[common_dates[index - 30]] - Decimal(1))
            return_90 = metric(prices[decision_at] / prices[common_dates[index - 90]] - Decimal(1))
            volatility = _window_volatility(
                tuple(prices[value] for value in common_dates[index - 30 : index + 1])
            )
            score = reversal_regime_score(
                return_7d=return_7,
                return_90d=return_90,
                volatility_30d=volatility,
                spread_penalty=Decimal(0),
                depth_penalty=Decimal(0),
                modeled_round_trip_cost=round_trip_cost,
            )
            rows.append((product_id, score, return_7, return_30, return_90, volatility))
        qualified = tuple(
            sorted(
                (row for row in rows if row[4] > 0 and row[2] < 0),
                key=lambda row: (-row[1], row[0]),
            )
        )
        qualified_rank = {row[0]: rank for rank, row in enumerate(qualified, 1)}
        all_rank = {
            row[0]: rank
            for rank, row in enumerate(sorted(rows, key=lambda row: (-row[1], row[0])), 1)
        }
        for product_id, score, return_7, return_30, return_90, volatility in rows:
            included = product_id in qualified_rank
            bucket = (
                _rank_bucket(qualified_rank[product_id], len(qualified))
                if included
                else "PRECONDITION_FAILED"
            )
            outcome_14_at = common_dates[index + REVERSAL_REGIME_PRIMARY_HOLDING_DAYS]
            outcome_30_at = common_dates[index + REVERSAL_REGIME_SECONDARY_HOLDING_DAYS]
            forward_14 = metric(
                candle_maps[product_id][outcome_14_at] / candle_maps[product_id][decision_at]
                - Decimal(1)
            )
            forward_30 = metric(
                candle_maps[product_id][outcome_30_at] / candle_maps[product_id][decision_at]
                - Decimal(1)
            )
            reasons = tuple(
                reason
                for condition, reason in (
                    (return_90 <= 0, "ABSOLUTE_90_DAY_RETURN_NOT_POSITIVE"),
                    (return_7 >= 0, "SEVEN_DAY_RETURN_NOT_NEGATIVE"),
                )
                if condition
            )
            payload = (
                product_id,
                decision_at,
                outcome_14_at,
                outcome_30_at,
                all_rank[product_id],
                bucket,
                return_7,
                return_30,
                return_90,
                volatility,
                score,
                forward_14,
                forward_30,
                included,
                reasons,
            )
            decisions.append(
                CryptoCalibrationDecisionOutcome(
                    product_id=product_id,
                    decision_at=decision_at,
                    outcome_at=outcome_14_at,
                    rank=all_rank[product_id],
                    bucket=bucket,
                    trailing_return_30d=return_30,
                    trailing_return_90d=return_90,
                    universe_median_90d=Decimal(0),
                    relative_strength=return_90,
                    reversal_penalty=metric(-return_7),
                    volatility_penalty=metric(volatility * Decimal("0.15")),
                    gross_research_score=score,
                    forward_gross_return=forward_14,
                    included_in_bucket=included,
                    exclusion_reasons=reasons,
                    canonical_hash=content_digest(payload),
                    trailing_return_7d=return_7,
                    forward_gross_return_14d=forward_14,
                    forward_gross_return_30d=forward_30,
                )
            )
            if included:
                outcomes[bucket][REVERSAL_REGIME_PRIMARY_HOLDING_DAYS].append(forward_14)
                outcomes[bucket][REVERSAL_REGIME_SECONDARY_HOLDING_DAYS].append(forward_30)
    buckets: list[CryptoCalibrationBucket] = []
    for bucket in ("TOP_THIRD", "MIDDLE_THIRD", "BOTTOM_THIRD"):
        horizons = tuple(
            _reversal_horizon(
                outcome_days=days,
                values=tuple(outcomes[bucket][days]),
                round_trip_cost=round_trip_cost,
            )
            for days in (
                REVERSAL_REGIME_PRIMARY_HOLDING_DAYS,
                REVERSAL_REGIME_SECONDARY_HOLDING_DAYS,
            )
        )
        primary = horizons[0]
        bucket_payload = (bucket, horizons, round_trip_cost)
        buckets.append(
            CryptoCalibrationBucket(
                bucket=bucket,
                sample_count=primary.sample_count,
                expected_gross_return=primary.expected_gross_return,
                uncertainty_lower=primary.uncertainty_lower,
                uncertainty_upper=primary.uncertainty_upper,
                canonical_hash=content_digest(bucket_payload),
                modeled_round_trip_cost=round_trip_cost,
                expected_net_return=primary.expected_net_return,
                net_uncertainty_lower=primary.uncertainty_lower,
                net_uncertainty_upper=primary.uncertainty_upper,
                horizon_results=horizons,
            )
        )
    coverage = tuple(
        CryptoCalibrationAssetCoverage(
            canonical_asset_id=(
                by_product[product_id].canonical_asset_id or product_id.rsplit("-", 1)[0]
            ),
            product_id=product_id,
            observation_id=by_product[product_id].observation_id,
            requested_start=by_product[product_id].interval_start,
            requested_end=by_product[product_id].interval_end,
            accepted_buckets=len(by_product[product_id].candles),
            missing_buckets=by_product[product_id].missing_buckets,
            duplicate_records=by_product[product_id].duplicate_records,
            rejected_records=by_product[product_id].rejected_records,
            request_count=by_product[product_id].request_count,
            coverage_status=by_product[product_id].coverage_status,
            limitations=by_product[product_id].coverage_limitations,
            raw_evidence_hashes=by_product[product_id].raw_evidence_hashes,
            canonical_hash=content_digest(
                (
                    product_id,
                    by_product[product_id].observation_id,
                    by_product[product_id].canonical_hash,
                )
            ),
        )
        for product_id in product_ids
    )
    calibration_payload = (
        calibration_id,
        CRYPTO_REVERSAL_REGIME_CALIBRATION_V1,
        1,
        common_dates[0],
        common_dates[-1],
        5,
        90,
        (REVERSAL_REGIME_PRIMARY_HOLDING_DAYS, REVERSAL_REGIME_SECONDARY_HOLDING_DAYS),
        tuple(by_product[product_id].observation_id for product_id in product_ids),
        tuple(buckets),
        formula_hash,
        round_trip_cost,
        coverage,
        common_dates,
        tuple(decisions),
        "NONE",
    )
    return CryptoRelativeStrengthCalibration(
        calibration_id=calibration_id,
        method_id=CRYPTO_REVERSAL_REGIME_CALIBRATION_V1,
        version=1,
        dataset_start=common_dates[0],
        dataset_end=common_dates[-1],
        decision_spacing_days=5,
        lookback_days=90,
        outcome_days=REVERSAL_REGIME_PRIMARY_HOLDING_DAYS,
        observation_ids=tuple(by_product[product_id].observation_id for product_id in product_ids),
        buckets=tuple(buckets),
        formula_hash=formula_hash,
        canonical_hash=content_digest(calibration_payload),
        asset_coverage=coverage,
        usable_common_dates=common_dates,
        decision_outcomes=tuple(decisions),
        outcome_horizons=(
            REVERSAL_REGIME_PRIMARY_HOLDING_DAYS,
            REVERSAL_REGIME_SECONDARY_HOLDING_DAYS,
        ),
    )


def run_reversal_regime(
    *,
    run_id: str,
    source_pulse_id: str,
    snapshot: CryptoUniverseSnapshot,
    observations: tuple[CryptoProductObservation, ...],
    current_market: tuple[CryptoCurrentMarketEvidence, ...],
    generated_at: datetime,
    calibration_observations: tuple[CryptoProductObservation, ...],
) -> CryptoDiscoveryRun:
    """Run the fixed multi-asset reversal signal inside a positive 90-day regime."""

    observation_by_product = {item.product_id: item for item in observations}
    market_by_product = {item.product_id: item for item in current_market}
    product_ids = tuple(snapshot.eligible_product_ids)
    if (
        len(product_ids) < 3
        or set(observation_by_product) != set(product_ids)
        or set(market_by_product) != set(product_ids)
        or {item.product_id for item in calibration_observations} != set(product_ids)
    ):
        raise CryptoUniverseError("CRYPTO_REVERSAL_REGIME_INPUT_LINEAGE_INCOMPLETE")
    formula_hash = content_digest(
        (
            CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1,
            "NEGATIVE_RETURN_7D",
            "PLUS_0.50_RETURN_90D",
            "MINUS_0.15_VOLATILITY_30D",
            "MINUS_CURRENT_SPREAD_PENALTY",
            "MINUS_CURRENT_DEPTH_PENALTY",
            "MINUS_MODELED_ROUND_TRIP_COST",
            RELATIVE_STRENGTH_MINIMUM_SAMPLES,
        )
    )
    calibration = _calibrate_reversal_regime(
        calibration_id=f"{run_id}-calibration",
        observations=calibration_observations,
        formula_hash=formula_hash,
    )
    generated = _time(generated_at, "CRYPTO_REVERSAL_REGIME_TIME_INVALID")
    if calibration.dataset_end >= generated:
        raise CryptoUniverseError("CRYPTO_REVERSAL_REGIME_CALIBRATION_LEAKAGE")
    round_trip_cost = metric(
        Decimal(2) * (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / Decimal(10000)
    )
    rows: list[
        tuple[str, str, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, str]
    ] = []
    for product_id in product_ids:
        observation = observation_by_product[product_id]
        if len(observation.candles) < 91:
            raise CryptoUniverseError("CRYPTO_REVERSAL_REGIME_HISTORY_INSUFFICIENT")
        market = market_by_product[product_id]
        return_7 = metric(observation.candles[-1].close / observation.candles[-8].close - 1)
        return_90 = metric(observation.candles[-1].close / observation.candles[-91].close - 1)
        volatility = _window_volatility(tuple(item.close for item in observation.candles[-31:]))
        spread = metric(
            Decimal(0) if market.spread_bps is None else market.spread_bps / Decimal(10000)
        )
        depth = metric(
            Decimal("0.05")
            if market.visible_ask_depth is None
            else max(Decimal(0), RELATIVE_STRENGTH_MINIMUM_DEPTH - market.visible_ask_depth)
            / RELATIVE_STRENGTH_MINIMUM_DEPTH
            * Decimal("0.05")
        )
        score = reversal_regime_score(
            return_7d=return_7,
            return_90d=return_90,
            volatility_30d=volatility,
            spread_penalty=spread,
            depth_penalty=depth,
            modeled_round_trip_cost=round_trip_cost,
        )
        rows.append(
            (
                observation.canonical_asset_id or product_id.rsplit("-", 1)[0],
                product_id,
                score,
                return_7,
                return_90,
                volatility,
                spread,
                depth,
                round_trip_cost,
                observation.observation_id,
            )
        )
    rows.sort(key=lambda row: (-row[2], row[0], row[1]))
    qualified = tuple(row for row in rows if row[4] > 0 and row[3] < 0)
    qualified_rank = {row[1]: rank for rank, row in enumerate(qualified, 1)}
    calibration_by_bucket = {item.bucket: item for item in calibration.buckets}
    results: list[CryptoDiscoveryResult] = []
    for rank, row in enumerate(rows, 1):
        asset, product_id, score, return_7, return_90, volatility, spread, depth, cost, obs_id = row
        market = market_by_product[product_id]
        bucket_name = (
            _rank_bucket(qualified_rank[product_id], len(qualified))
            if product_id in qualified_rank
            else "PRECONDITION_FAILED"
        )
        bucket = calibration_by_bucket.get(bucket_name)
        primary = None if bucket is None else bucket.horizon_results[0]
        gross = None if primary is None else primary.expected_gross_return
        net = None if primary is None else primary.expected_net_return
        lower = None if primary is None else primary.uncertainty_lower
        upper = None if primary is None else primary.uncertainty_upper
        reasons: list[str] = []
        if return_90 <= 0:
            reasons.append("ABSOLUTE_90_DAY_RETURN_NOT_POSITIVE")
        if return_7 >= 0:
            reasons.append("SEVEN_DAY_RETURN_NOT_NEGATIVE")
        if not market.evidence_complete:
            reasons.append("CURRENT_MARKET_EVIDENCE_INCOMPLETE")
        if market.product_status != "ACTIVE/ONLINE":
            reasons.append("CURRENT_PRODUCT_STATUS_INELIGIBLE")
        if (
            int((generated - market.observed_at).total_seconds())
            > RELATIVE_STRENGTH_FRESHNESS_SECONDS
        ):
            reasons.append("CURRENT_MARKET_EVIDENCE_STALE")
        if (
            market.required_capital is None
            or market.visible_ask_depth is None
            or market.visible_ask_depth
            < max(market.required_capital, RELATIVE_STRENGTH_MINIMUM_DEPTH)
        ):
            reasons.append("CURRENT_VISIBLE_DEPTH_INSUFFICIENT")
        if primary is None or primary.sample_count < RELATIVE_STRENGTH_MINIMUM_SAMPLES:
            reasons.append("CALIBRATION_SAMPLE_INSUFFICIENT")
        if net is None or net <= 0:
            reasons.append("EXPECTED_NET_RETURN_NOT_POSITIVE")
        if lower is None or lower <= 0:
            reasons.append("UNCERTAINTY_LOWER_BOUND_NOT_POSITIVE")
        eligible = not reasons
        payload = (
            product_id,
            rank,
            return_7,
            return_90,
            volatility,
            spread,
            depth,
            cost,
            score,
            bucket_name,
            None if bucket is None else bucket.horizon_results,
            eligible,
            tuple(reasons),
            market,
            obs_id,
        )
        results.append(
            CryptoDiscoveryResult(
                product_id=product_id,
                rank=rank,
                trailing_return=return_90,
                realized_volatility=volatility,
                risk_adjusted_return=metric(return_90 / max(volatility, METRIC_QUANTUM)),
                mean_daily_quote_volume=observation_by_product[product_id].mean_daily_quote_volume,
                estimated_round_trip_cost=metric(cost + spread),
                liquidity_penalty=depth,
                gross_research_score=score,
                supporting_observation_id=obs_id,
                canonical_hash=content_digest(payload),
                canonical_asset_id=asset,
                trailing_return_90d=return_90,
                volatility_penalty=metric(volatility * Decimal("0.15")),
                spread_penalty=spread,
                depth_penalty=depth,
                calibration_bucket=bucket_name,
                expected_gross_return=gross,
                expected_net_return=net,
                uncertainty_lower=lower,
                uncertainty_upper=upper,
                calibration_sample_count=None if primary is None else primary.sample_count,
                source_eligible=eligible,
                source_rejection_reasons=tuple(reasons),
                current_spread_bps=market.spread_bps,
                visible_ask_depth=market.visible_ask_depth,
                required_capital=market.required_capital,
                maximum_loss=market.maximum_loss,
                maximum_sensible_allocation=market.maximum_sensible_allocation,
                supporting_checkpoint_id=market.checkpoint_id,
                trailing_return_7d=return_7,
                downside_probability=(None if primary is None else primary.downside_probability),
                downside_p05=None if primary is None else primary.downside_p05,
                maximum_observed_loss=(None if primary is None else primary.maximum_observed_loss),
                calibration_horizons=() if bucket is None else bucket.horizon_results,
            )
        )
    result_hash = content_digest(tuple(item.canonical_hash for item in results))
    run_payload = (
        run_id,
        snapshot.snapshot_id,
        CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1,
        1,
        generated,
        tuple(results),
        calibration,
        source_pulse_id,
        result_hash,
        "NONE",
    )
    return CryptoDiscoveryRun(
        run_id=_identity(run_id, "CRYPTO_REVERSAL_REGIME_RUN_ID_INVALID"),
        universe_snapshot_id=snapshot.snapshot_id,
        method_id=CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1,
        method_version=1,
        generated_at=generated,
        results=tuple(results),
        warnings=(
            "EXPECTED_RETURN_USES_STRICTLY_PRIOR_COMPLETED_WALK_FORWARD_OUTCOMES",
            "HISTORICAL_BOOK_PENALTIES_UNAVAILABLE_AND_RETAINED_AS_LIMITATION",
            "OVERLAPPING_14_AND_30_DAY_OUTCOMES_LIMIT_INDEPENDENCE",
            "DISPLAYED_PUBLIC_DEPTH_IS_NOT_FILLABILITY",
            CRYPTO_UNIVERSE_WARNING,
        ),
        result_hash=result_hash,
        canonical_hash=content_digest(run_payload),
        source_pulse_id=source_pulse_id,
        calibration=calibration,
    )


@dataclass(frozen=True, slots=True)
class CryptoResearchOpportunity:
    opportunity_id: str
    # Despite the name, this is a union identity, not always a
    # ``CryptoDiscoveryRun.run_id``: as of migration 0100 it durably resolves
    # through *either* ``CryptoDiscoveryRun.run_id`` (the three
    # ``build_opportunities``/``build_relative_strength_opportunities``/
    # ``build_reversal_regime_opportunities`` cross-sectional producers) *or*
    # ``domain.crypto_stateful_research.CryptoStatefulCurrentSignal.signal_id``
    # (the stateful pullback-rotation producer) -- enforced at insert time by
    # ``apex_crypto_opportunity_lineage_guard`` against exactly those two
    # tables, not a single-table foreign key. Renaming was deliberately
    # deferred (see migration 0100's docstring); do not reintroduce a
    # single-table assumption when reading or constructing this field.
    discovery_run_id: str
    universe_snapshot_id: str
    product_id: str
    generated_at: datetime
    valid_until: datetime
    rank: int
    supporting_observation_id: str
    gross_research_score: Decimal
    volatility_estimate: Decimal
    fee_bps_per_side: Decimal
    slippage_bps_per_side: Decimal
    liquidity_limitations: tuple[str, ...]
    hypothetical_holding_days: int
    maximum_research_allocation: Decimal
    status: OpportunityStatus
    warnings: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    canonical_hash: str
    canonical_asset_id: str = ""
    alternate_product_route_ids: tuple[str, ...] = ()
    strategy_version: int = 1
    expected_gross_return: Decimal | None = None
    expected_net_return: Decimal | None = None
    uncertainty_lower: Decimal | None = None
    uncertainty_upper: Decimal | None = None
    calibration_sample_count: int | None = None
    required_capital: Decimal | None = None
    maximum_loss: Decimal | None = None
    maximum_sensible_allocation: Decimal | None = None
    current_spread_bps: Decimal | None = None
    visible_ask_depth: Decimal | None = None
    score_components: tuple[tuple[str, Decimal], ...] = ()
    calibration_id: str | None = None
    supporting_checkpoint_id: str | None = None
    research_only: bool = True
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    broker_authority: str = "NONE"
    financial_mutation_authority: str = "NONE"
    method_id: str = CRYPTO_DISCOVERY_V1
    trailing_return_7d: Decimal | None = None
    trailing_return_90d: Decimal | None = None
    realized_volatility_30d: Decimal | None = None
    downside_probability: Decimal | None = None
    downside_p05: Decimal | None = None
    maximum_observed_loss: Decimal | None = None
    calibration_horizons: tuple[CryptoCalibrationHorizon, ...] = ()


def build_relative_strength_opportunities(
    run: CryptoDiscoveryRun,
) -> tuple[CryptoResearchOpportunity, ...]:
    if (
        run.method_id != CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1
        or run.calibration is None
        or run.source_pulse_id is None
    ):
        raise CryptoUniverseError("CRYPTO_RELATIVE_STRENGTH_RUN_INVALID")
    values: list[CryptoResearchOpportunity] = []
    for result in run.results:
        status = (
            OpportunityStatus.RESEARCH_ONLY
            if result.source_eligible
            else OpportunityStatus.REJECTED
        )
        opportunity_id = f"{run.run_id}-{result.canonical_asset_id.lower()}-opportunity"
        valid_until = run.generated_at + timedelta(days=1)
        components = tuple(
            (name, value)
            for name, value in (
                ("RETURN_30D", result.trailing_return_30d),
                ("RETURN_90D", result.trailing_return_90d),
                ("UNIVERSE_MEDIAN_90D", result.universe_median_90d),
                ("RELATIVE_STRENGTH", result.relative_strength),
                ("REVERSAL_PENALTY", result.reversal_penalty),
                ("VOLATILITY_PENALTY", result.volatility_penalty),
                ("SPREAD_PENALTY", result.spread_penalty),
                ("DEPTH_PENALTY", result.depth_penalty),
            )
            if value is not None
        )
        payload = (
            opportunity_id,
            run.run_id,
            run.universe_snapshot_id,
            result.product_id,
            run.generated_at,
            valid_until,
            result.rank,
            result.supporting_observation_id,
            result.gross_research_score,
            result.realized_volatility,
            FEE_BPS_PER_SIDE,
            SLIPPAGE_BPS_PER_SIDE,
            RELATIVE_STRENGTH_HOLDING_DAYS,
            MAX_RESEARCH_ALLOCATION,
            status,
            tuple(result.source_rejection_reasons),
            result.canonical_asset_id,
            result.expected_gross_return,
            result.expected_net_return,
            result.uncertainty_lower,
            result.uncertainty_upper,
            result.calibration_sample_count,
            result.required_capital,
            result.maximum_loss,
            result.maximum_sensible_allocation,
            result.current_spread_bps,
            result.visible_ask_depth,
            components,
            run.calibration.calibration_id,
            result.supporting_checkpoint_id,
        )
        values.append(
            CryptoResearchOpportunity(
                opportunity_id=opportunity_id,
                discovery_run_id=run.run_id,
                universe_snapshot_id=run.universe_snapshot_id,
                product_id=result.product_id,
                generated_at=run.generated_at,
                valid_until=valid_until,
                rank=result.rank,
                supporting_observation_id=result.supporting_observation_id,
                gross_research_score=result.gross_research_score,
                volatility_estimate=result.realized_volatility,
                fee_bps_per_side=FEE_BPS_PER_SIDE,
                slippage_bps_per_side=SLIPPAGE_BPS_PER_SIDE,
                liquidity_limitations=(
                    "DISPLAYED_PUBLIC_DEPTH_IS_NOT_FILLABILITY",
                    "SPOT_DOWNSIDE_ASSUMES_PRICE_CAN_FALL_TO_ZERO",
                ),
                hypothetical_holding_days=RELATIVE_STRENGTH_HOLDING_DAYS,
                maximum_research_allocation=MAX_RESEARCH_ALLOCATION,
                status=status,
                warnings=(
                    CRYPTO_UNIVERSE_WARNING,
                    "EXPECTED_RETURN_IS_CALIBRATED_NOT_CURRENT_SCORE_DERIVED",
                    "NOT AN ORDER",
                    "NOT PAPER-ELIGIBLE",
                    "NOT LIVE-ELIGIBLE",
                    "NO BROKER OR FINANCIAL MUTATION AUTHORITY",
                ),
                rejection_reasons=result.source_rejection_reasons,
                canonical_hash=content_digest(payload),
                canonical_asset_id=result.canonical_asset_id,
                alternate_product_route_ids=result.alternate_product_route_ids,
                strategy_version=1,
                expected_gross_return=result.expected_gross_return,
                expected_net_return=result.expected_net_return,
                uncertainty_lower=result.uncertainty_lower,
                uncertainty_upper=result.uncertainty_upper,
                calibration_sample_count=result.calibration_sample_count,
                required_capital=result.required_capital,
                maximum_loss=result.maximum_loss,
                maximum_sensible_allocation=result.maximum_sensible_allocation,
                current_spread_bps=result.current_spread_bps,
                visible_ask_depth=result.visible_ask_depth,
                score_components=components,
                calibration_id=run.calibration.calibration_id,
                supporting_checkpoint_id=result.supporting_checkpoint_id,
                method_id=CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1,
                trailing_return_90d=result.trailing_return_90d,
                realized_volatility_30d=result.realized_volatility,
            )
        )
    return tuple(values)


def build_reversal_regime_opportunities(
    run: CryptoDiscoveryRun,
) -> tuple[CryptoResearchOpportunity, ...]:
    if (
        run.method_id != CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1
        or run.calibration is None
        or run.source_pulse_id is None
    ):
        raise CryptoUniverseError("CRYPTO_REVERSAL_REGIME_RUN_INVALID")
    values: list[CryptoResearchOpportunity] = []
    for result in run.results:
        status = (
            OpportunityStatus.RESEARCH_ONLY
            if result.source_eligible
            else OpportunityStatus.REJECTED
        )
        opportunity_id = f"{run.run_id}-{result.canonical_asset_id.lower()}-opportunity"
        valid_until = run.generated_at + timedelta(days=1)
        components = tuple(
            (name, value)
            for name, value in (
                ("RETURN_7D", result.trailing_return_7d),
                ("RETURN_90D", result.trailing_return_90d),
                ("VOLATILITY_30D", result.realized_volatility),
                ("VOLATILITY_PENALTY", result.volatility_penalty),
                ("SPREAD_PENALTY", result.spread_penalty),
                ("DEPTH_PENALTY", result.depth_penalty),
                ("MODELED_ROUND_TRIP_COST", result.estimated_round_trip_cost),
            )
            if value is not None
        )
        payload = (
            opportunity_id,
            run.run_id,
            run.universe_snapshot_id,
            result.product_id,
            run.generated_at,
            valid_until,
            result.rank,
            result.supporting_observation_id,
            result.gross_research_score,
            result.realized_volatility,
            result.trailing_return_7d,
            result.trailing_return_90d,
            result.calibration_bucket,
            result.calibration_horizons,
            result.expected_gross_return,
            result.expected_net_return,
            result.uncertainty_lower,
            result.uncertainty_upper,
            result.downside_probability,
            result.downside_p05,
            result.maximum_observed_loss,
            result.source_eligible,
            result.source_rejection_reasons,
            components,
            run.calibration.calibration_id,
            result.supporting_checkpoint_id,
            "NONE",
        )
        values.append(
            CryptoResearchOpportunity(
                opportunity_id=opportunity_id,
                discovery_run_id=run.run_id,
                universe_snapshot_id=run.universe_snapshot_id,
                product_id=result.product_id,
                generated_at=run.generated_at,
                valid_until=valid_until,
                rank=result.rank,
                supporting_observation_id=result.supporting_observation_id,
                gross_research_score=result.gross_research_score,
                volatility_estimate=result.realized_volatility,
                fee_bps_per_side=FEE_BPS_PER_SIDE,
                slippage_bps_per_side=SLIPPAGE_BPS_PER_SIDE,
                liquidity_limitations=(
                    "DISPLAYED_PUBLIC_DEPTH_IS_NOT_FILLABILITY",
                    "HISTORICAL_BOOK_PENALTIES_UNAVAILABLE",
                    "SPOT_DOWNSIDE_ASSUMES_PRICE_CAN_FALL_TO_ZERO",
                ),
                hypothetical_holding_days=REVERSAL_REGIME_PRIMARY_HOLDING_DAYS,
                maximum_research_allocation=MAX_RESEARCH_ALLOCATION,
                status=status,
                warnings=(
                    CRYPTO_UNIVERSE_WARNING,
                    "EXPECTED_RETURN_IS_CALIBRATED_NOT_CURRENT_SCORE_DERIVED",
                    "FOURTEEN_AND_THIRTY_DAY_OUTCOMES_RETAINED_SEPARATELY",
                    "NOT AN ORDER",
                    "NOT PAPER-ELIGIBLE",
                    "NOT LIVE-ELIGIBLE",
                    "NO BROKER OR FINANCIAL MUTATION AUTHORITY",
                ),
                rejection_reasons=result.source_rejection_reasons,
                canonical_hash=content_digest(payload),
                canonical_asset_id=result.canonical_asset_id,
                strategy_version=1,
                expected_gross_return=result.expected_gross_return,
                expected_net_return=result.expected_net_return,
                uncertainty_lower=result.uncertainty_lower,
                uncertainty_upper=result.uncertainty_upper,
                calibration_sample_count=result.calibration_sample_count,
                required_capital=result.required_capital,
                maximum_loss=result.maximum_loss,
                maximum_sensible_allocation=result.maximum_sensible_allocation,
                current_spread_bps=result.current_spread_bps,
                visible_ask_depth=result.visible_ask_depth,
                score_components=components,
                calibration_id=run.calibration.calibration_id,
                supporting_checkpoint_id=result.supporting_checkpoint_id,
                method_id=CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1,
                trailing_return_7d=result.trailing_return_7d,
                trailing_return_90d=result.trailing_return_90d,
                realized_volatility_30d=result.realized_volatility,
                downside_probability=result.downside_probability,
                downside_p05=result.downside_p05,
                maximum_observed_loss=result.maximum_observed_loss,
                calibration_horizons=result.calibration_horizons,
            )
        )
    return tuple(values)


def build_opportunities(
    run: CryptoDiscoveryRun,
) -> tuple[CryptoResearchOpportunity, ...]:
    values: list[CryptoResearchOpportunity] = []
    for result in run.results:
        accepted = result.rank <= MAX_OPPORTUNITIES and result.gross_research_score > 0
        reasons: tuple[str, ...] = ()
        if result.rank > MAX_OPPORTUNITIES:
            reasons += ("OUTSIDE_TOP_RANKED_BOUND",)
        if result.gross_research_score <= 0:
            reasons += ("NON_POSITIVE_NET_RESEARCH_SCORE",)
        status = OpportunityStatus.RESEARCH_ONLY if accepted else OpportunityStatus.REJECTED
        valid_until = run.generated_at + timedelta(days=1)
        opportunity_id = (
            f"{run.run_id}-{(result.canonical_asset_id or result.product_id).lower()}-opportunity"
        )
        payload = (
            opportunity_id,
            run.run_id,
            run.universe_snapshot_id,
            result.product_id,
            run.generated_at,
            valid_until,
            result.rank,
            result.supporting_observation_id,
            result.gross_research_score,
            result.realized_volatility,
            FEE_BPS_PER_SIDE,
            SLIPPAGE_BPS_PER_SIDE,
            ("PUBLIC_VOLUME_IS_NOT_EXECUTABLE_LIQUIDITY",),
            HYPOTHETICAL_HOLDING_DAYS,
            MAX_RESEARCH_ALLOCATION,
            status,
            (
                CRYPTO_UNIVERSE_WARNING,
                "NOT AN ORDER",
                "NOT PAPER-ELIGIBLE",
                "NOT LIVE-ELIGIBLE",
                "NO BROKER OR FINANCIAL MUTATION AUTHORITY",
            ),
            reasons,
            result.canonical_asset_id,
            result.alternate_product_route_ids,
        )
        values.append(
            CryptoResearchOpportunity(
                opportunity_id=opportunity_id,
                discovery_run_id=run.run_id,
                universe_snapshot_id=run.universe_snapshot_id,
                product_id=result.product_id,
                generated_at=run.generated_at,
                valid_until=valid_until,
                rank=result.rank,
                supporting_observation_id=result.supporting_observation_id,
                gross_research_score=result.gross_research_score,
                volatility_estimate=result.realized_volatility,
                fee_bps_per_side=FEE_BPS_PER_SIDE,
                slippage_bps_per_side=SLIPPAGE_BPS_PER_SIDE,
                liquidity_limitations=("PUBLIC_VOLUME_IS_NOT_EXECUTABLE_LIQUIDITY",),
                hypothetical_holding_days=HYPOTHETICAL_HOLDING_DAYS,
                maximum_research_allocation=MAX_RESEARCH_ALLOCATION,
                status=status,
                warnings=(
                    CRYPTO_UNIVERSE_WARNING,
                    "NOT AN ORDER",
                    "NOT PAPER-ELIGIBLE",
                    "NOT LIVE-ELIGIBLE",
                    "NO BROKER OR FINANCIAL MUTATION AUTHORITY",
                ),
                rejection_reasons=reasons,
                canonical_hash=content_digest(payload),
                canonical_asset_id=result.canonical_asset_id,
                alternate_product_route_ids=result.alternate_product_route_ids,
            )
        )
    return tuple(values)
