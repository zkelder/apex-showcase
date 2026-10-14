"""Application services for bounded public multi-asset crypto discovery."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from apex.domain.crypto_universe import (
    COINBASE_PUBLIC_PRODUCT_PARSER_V1,
    COINBASE_PUBLIC_PRODUCTS_V1,
    CRYPTO_CALIBRATION_HISTORY_MAXIMUM_DAYS,
    CRYPTO_CALIBRATION_HISTORY_MINIMUM_DAYS,
    CRYPTO_CALIBRATION_MAXIMUM_REQUESTS_PER_ASSET,
    CRYPTO_UNIVERSE_POLICY_V2,
    MAX_CATALOG_BYTES,
    MAX_CATALOG_PAGES,
    MAX_CATALOG_RUNTIME_MILLISECONDS,
    MAX_CONSIDERED_PRODUCTS,
    MAX_OBSERVED_PRODUCTS,
    MAX_PRODUCTS_PER_PAGE,
    CatalogPaginationStatus,
    CryptoDailyObservation,
    CryptoDiscoveryRun,
    CryptoHistoryPartition,
    CryptoProductCapture,
    CryptoProductObservation,
    CryptoProductPageEvidence,
    CryptoResearchOpportunity,
    CryptoUniverseSnapshot,
    PublicCryptoProduct,
    build_asset_groups,
    build_opportunities,
    materialize_universe,
    run_discovery,
)
from apex.domain.research_evidence import EvidenceClass


class CryptoUniverseConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CapturedProductInput:
    product_id: str
    base_asset: str
    quote_asset: str
    market_type: str
    trading_status: str
    product_status: str
    price_increment: object
    quantity_increment: object
    minimum_quantity: object | None
    minimum_notional: object | None
    public_volume_24h: object | None
    provider_alias: str | None = None
    provider_alias_to: tuple[str, ...] = ()
    restriction_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CapturedProductPage:
    captured_at: datetime
    raw_payload: bytes
    products: tuple[CapturedProductInput, ...]
    source_locator: str
    runtime_milliseconds: int
    request_cursor: str | None
    next_cursor: str | None
    has_next: bool


@dataclass(frozen=True, slots=True)
class CapturedObservationPage:
    captured_at: datetime
    raw_payload: bytes
    candles: tuple[CryptoDailyObservation, ...]
    missing_buckets: int
    duplicate_records: int
    rejected_records: int
    source_locator: str
    runtime_milliseconds: int


@dataclass(frozen=True, slots=True)
class CapturedProductBook:
    """One bounded public book normalized without account or order access."""

    captured_at: datetime
    raw_payload: bytes
    product_id: str
    best_bid: Decimal
    best_ask: Decimal
    midpoint: Decimal
    spread_bps: Decimal
    visible_bid_depth: Decimal
    visible_ask_depth: Decimal
    source_locator: str
    runtime_milliseconds: int


class PublicCryptoUniverseSource(Protocol):
    evidence_class: EvidenceClass
    network_access: bool

    def capture_products(
        self,
        *,
        requested_at: datetime,
        maximum_products: int,
        cursor: str | None,
    ) -> CapturedProductPage: ...

    def capture_daily_observations(
        self,
        *,
        product_id: str,
        interval_start: datetime,
        interval_end: datetime,
        requested_at: datetime,
    ) -> CapturedObservationPage: ...

    def capture_product_book(
        self,
        *,
        product_id: str,
        maximum_depth: int,
        requested_at: datetime,
    ) -> CapturedProductBook: ...


class CryptoUniverseRepository(Protocol):
    def product_capture(self, capture_id: str) -> CryptoProductCapture | None: ...
    def save_product_capture(
        self, capture: CryptoProductCapture, products: tuple[PublicCryptoProduct, ...]
    ) -> CryptoProductCapture: ...
    def product_captures(self) -> tuple[CryptoProductCapture, ...]: ...
    def products(self, capture_id: str) -> tuple[PublicCryptoProduct, ...]: ...
    def observation(self, observation_id: str) -> CryptoProductObservation | None: ...
    def save_observation(
        self, observation: CryptoProductObservation
    ) -> CryptoProductObservation: ...
    def observations(self) -> tuple[CryptoProductObservation, ...]: ...
    def universe_snapshot(self, snapshot_id: str) -> CryptoUniverseSnapshot | None: ...
    def save_universe_snapshot(
        self, snapshot: CryptoUniverseSnapshot
    ) -> CryptoUniverseSnapshot: ...
    def universe_snapshots(self) -> tuple[CryptoUniverseSnapshot, ...]: ...
    def discovery_run(self, run_id: str) -> CryptoDiscoveryRun | None: ...
    def save_discovery_run(self, run: CryptoDiscoveryRun) -> CryptoDiscoveryRun: ...
    def discovery_runs(self) -> tuple[CryptoDiscoveryRun, ...]: ...
    def opportunity(self, opportunity_id: str) -> CryptoResearchOpportunity | None: ...
    def save_opportunities(
        self, opportunities: tuple[CryptoResearchOpportunity, ...]
    ) -> tuple[CryptoResearchOpportunity, ...]: ...
    def opportunities(self) -> tuple[CryptoResearchOpportunity, ...]: ...


class InMemoryCryptoUniverseRepository:
    def __init__(self) -> None:
        self._captures: dict[str, CryptoProductCapture] = {}
        self._products: dict[str, tuple[PublicCryptoProduct, ...]] = {}
        self._observations: dict[str, CryptoProductObservation] = {}
        self._snapshots: dict[str, CryptoUniverseSnapshot] = {}
        self._runs: dict[str, CryptoDiscoveryRun] = {}
        self._opportunities: dict[str, CryptoResearchOpportunity] = {}

    def product_capture(self, capture_id: str) -> CryptoProductCapture | None:
        return self._captures.get(capture_id)

    def save_product_capture(
        self, capture: CryptoProductCapture, products: tuple[PublicCryptoProduct, ...]
    ) -> CryptoProductCapture:
        prior = self._captures.get(capture.capture_id)
        if prior and prior.canonical_hash != capture.canonical_hash:
            raise CryptoUniverseConflict("CRYPTO_PRODUCT_CAPTURE_IDENTITY_CONFLICT")
        self._captures[capture.capture_id] = prior or capture
        self._products[capture.capture_id] = self._products.get(capture.capture_id, products)
        return prior or capture

    def product_captures(self) -> tuple[CryptoProductCapture, ...]:
        return tuple(sorted(self._captures.values(), key=lambda item: item.capture_id))

    def products(self, capture_id: str) -> tuple[PublicCryptoProduct, ...]:
        return self._products.get(capture_id, ())

    def observation(self, observation_id: str) -> CryptoProductObservation | None:
        return self._observations.get(observation_id)

    def save_observation(self, observation: CryptoProductObservation) -> CryptoProductObservation:
        prior = self._observations.get(observation.observation_id)
        if prior and prior.canonical_hash != observation.canonical_hash:
            raise CryptoUniverseConflict("CRYPTO_OBSERVATION_IDENTITY_CONFLICT")
        self._observations[observation.observation_id] = prior or observation
        return prior or observation

    def observations(self) -> tuple[CryptoProductObservation, ...]:
        return tuple(
            sorted(
                self._observations.values(),
                key=lambda item: (item.checkpoint_ordinal, item.observation_id),
            )
        )

    def universe_snapshot(self, snapshot_id: str) -> CryptoUniverseSnapshot | None:
        return self._snapshots.get(snapshot_id)

    def save_universe_snapshot(self, snapshot: CryptoUniverseSnapshot) -> CryptoUniverseSnapshot:
        prior = self._snapshots.get(snapshot.snapshot_id)
        if prior and prior.canonical_hash != snapshot.canonical_hash:
            raise CryptoUniverseConflict("CRYPTO_UNIVERSE_SNAPSHOT_IDENTITY_CONFLICT")
        self._snapshots[snapshot.snapshot_id] = prior or snapshot
        return prior or snapshot

    def universe_snapshots(self) -> tuple[CryptoUniverseSnapshot, ...]:
        return tuple(sorted(self._snapshots.values(), key=lambda item: item.snapshot_id))

    def discovery_run(self, run_id: str) -> CryptoDiscoveryRun | None:
        return self._runs.get(run_id)

    def save_discovery_run(self, run: CryptoDiscoveryRun) -> CryptoDiscoveryRun:
        prior = self._runs.get(run.run_id)
        if prior and prior.canonical_hash != run.canonical_hash:
            raise CryptoUniverseConflict("CRYPTO_DISCOVERY_RUN_IDENTITY_CONFLICT")
        self._runs[run.run_id] = prior or run
        return prior or run

    def discovery_runs(self) -> tuple[CryptoDiscoveryRun, ...]:
        return tuple(sorted(self._runs.values(), key=lambda item: item.run_id))

    def opportunity(self, opportunity_id: str) -> CryptoResearchOpportunity | None:
        return self._opportunities.get(opportunity_id)

    def save_opportunities(
        self, opportunities: tuple[CryptoResearchOpportunity, ...]
    ) -> tuple[CryptoResearchOpportunity, ...]:
        result = []
        for value in opportunities:
            prior = self._opportunities.get(value.opportunity_id)
            if prior and prior.canonical_hash != value.canonical_hash:
                raise CryptoUniverseConflict("CRYPTO_OPPORTUNITY_IDENTITY_CONFLICT")
            self._opportunities[value.opportunity_id] = prior or value
            result.append(prior or value)
        return tuple(result)

    def opportunities(self) -> tuple[CryptoResearchOpportunity, ...]:
        return tuple(sorted(self._opportunities.values(), key=lambda item: item.opportunity_id))


class CapturePublicCryptoProducts:
    def __init__(
        self, repository: CryptoUniverseRepository, source: PublicCryptoUniverseSource
    ) -> None:
        self._repository = repository
        self._source = source

    def execute(self, *, capture_id: str, requested_at: datetime) -> CryptoProductCapture:
        prior = self._repository.product_capture(capture_id)
        if prior is not None:
            return prior
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_products: set[str] = set()
        page_evidence: list[CryptoProductPageEvidence] = []
        product_values: list[PublicCryptoProduct] = []
        raw_hashes: list[str] = []
        total_raw_bytes = 0
        total_runtime = 0
        status = CatalogPaginationStatus.TRUNCATED
        continuation_cursor: str | None = None
        captured_at = requested_at
        for page_ordinal in range(1, MAX_CATALOG_PAGES + 1):
            if cursor is not None:
                if cursor in seen_cursors:
                    raise CryptoUniverseConflict("CRYPTO_PRODUCT_CURSOR_REPEATED")
                seen_cursors.add(cursor)
            page = self._source.capture_products(
                requested_at=requested_at,
                maximum_products=MAX_PRODUCTS_PER_PAGE,
                cursor=cursor,
            )
            if page.request_cursor != cursor:
                raise CryptoUniverseConflict("CRYPTO_PRODUCT_CURSOR_RESPONSE_MISMATCH")
            raw_hash = hashlib.sha256(page.raw_payload).hexdigest()
            page_product_ids = tuple(item.product_id for item in page.products)
            if len(set(page_product_ids)) != len(page_product_ids):
                raise CryptoUniverseConflict("CRYPTO_PRODUCT_PAGE_DUPLICATE")
            if seen_products.intersection(page_product_ids):
                raise CryptoUniverseConflict("CRYPTO_PRODUCT_PAGINATION_DUPLICATE")
            seen_products.update(page_product_ids)
            raw_hashes.append(raw_hash)
            total_raw_bytes += len(page.raw_payload)
            total_runtime += page.runtime_milliseconds
            captured_at = max(captured_at, page.captured_at)
            page_evidence.append(
                CryptoProductPageEvidence.create(
                    page_ordinal=page_ordinal,
                    request_cursor=cursor,
                    next_cursor=page.next_cursor,
                    has_next=page.has_next,
                    product_ids=page_product_ids,
                    raw_evidence_hash=raw_hash,
                    raw_bytes=len(page.raw_payload),
                    completed_at=page.captured_at,
                    source_locator=page.source_locator,
                    runtime_milliseconds=page.runtime_milliseconds,
                )
            )
            product_values.extend(
                PublicCryptoProduct.create(
                    product_id=item.product_id,
                    provider="COINBASE",
                    base_asset=item.base_asset,
                    quote_asset=item.quote_asset,
                    market_type=item.market_type,
                    trading_status=item.trading_status,
                    product_status=item.product_status,
                    price_increment=item.price_increment,
                    quantity_increment=item.quantity_increment,
                    minimum_quantity=item.minimum_quantity,
                    minimum_notional=item.minimum_notional,
                    public_volume_24h=item.public_volume_24h,
                    captured_at=page.captured_at,
                    source_contract_version=COINBASE_PUBLIC_PRODUCTS_V1,
                    raw_evidence_hash=raw_hash,
                    parser_version=COINBASE_PUBLIC_PRODUCT_PARSER_V1,
                    evidence_class=self._source.evidence_class,
                    provider_alias=item.provider_alias,
                    provider_alias_to=item.provider_alias_to,
                    restriction_reasons=item.restriction_reasons,
                )
                for item in page.products
            )
            if total_raw_bytes > MAX_CATALOG_BYTES:
                raise CryptoUniverseConflict("CRYPTO_PRODUCT_CATALOG_BYTES_EXCEEDED")
            if total_runtime > MAX_CATALOG_RUNTIME_MILLISECONDS:
                raise CryptoUniverseConflict("CRYPTO_PRODUCT_CATALOG_RUNTIME_EXCEEDED")
            if not page.has_next:
                status = CatalogPaginationStatus.COMPLETE
                continuation_cursor = None
                break
            if not page.next_cursor or page.next_cursor == cursor:
                raise CryptoUniverseConflict("CRYPTO_PRODUCT_CURSOR_INCONSISTENT")
            if page.next_cursor in seen_cursors:
                raise CryptoUniverseConflict("CRYPTO_PRODUCT_CURSOR_REPEATED")
            continuation_cursor = page.next_cursor
            if page_ordinal == MAX_CATALOG_PAGES or len(product_values) >= MAX_CONSIDERED_PRODUCTS:
                break
            cursor = page.next_cursor
        products = tuple(product_values)
        raw_hash = hashlib.sha256("".join(raw_hashes).encode("ascii")).hexdigest()
        capture = CryptoProductCapture.create(
            capture_id=capture_id,
            provider="COINBASE",
            captured_at=captured_at,
            source_contract_version=COINBASE_PUBLIC_PRODUCTS_V1,
            parser_version=COINBASE_PUBLIC_PRODUCT_PARSER_V1,
            evidence_class=self._source.evidence_class,
            raw_evidence_hash=raw_hash,
            products=products,
            request_count=len(page_evidence),
            limitations=(
                "PUBLIC_PRODUCT_METADATA_IS_CURRENT_REFERENCE_ONLY",
                "PUBLIC_VOLUME_IS_NOT_EXECUTABLE_LIQUIDITY",
                "NO AUTHENTICATED ACCOUNT, FEE, OR ORDER ACCESS",
                "CATALOG_BOUND_10_PAGES_1000_PRODUCTS_20MB_150_SECONDS",
            ),
            pages=tuple(page_evidence),
            pagination_status=status,
            continuation_cursor=continuation_cursor,
            total_raw_bytes=total_raw_bytes,
            runtime_milliseconds=total_runtime,
        )
        return self._repository.save_product_capture(capture, products)


class CaptureCryptoUniverseObservations:
    def __init__(
        self, repository: CryptoUniverseRepository, source: PublicCryptoUniverseSource
    ) -> None:
        self._repository = repository
        self._source = source

    def execute(
        self,
        *,
        capture_id: str,
        interval_start: datetime,
        interval_end: datetime,
        requested_at: datetime,
    ) -> tuple[CryptoProductObservation, ...]:
        capture = self._repository.product_capture(capture_id)
        if capture is None:
            raise CryptoUniverseConflict("CRYPTO_PRODUCT_CAPTURE_NOT_FOUND")
        if capture.evidence_class is not self._source.evidence_class:
            raise CryptoUniverseConflict("CRYPTO_OBSERVATION_EVIDENCE_CLASS_CONFLICT")
        products = self._repository.products(capture_id)
        groups = build_asset_groups(
            products=products,
            observations=(),
            evidence_class=capture.evidence_class,
        )
        by_product = {item.product_id: item for item in products}
        selected = tuple(
            by_product[group.selected_product_id or ""]
            for group in sorted(
                (
                    item
                    for item in groups
                    if item.history_shortlist_rank is not None
                    and item.selected_product_id is not None
                ),
                key=lambda item: item.history_shortlist_rank or 0,
            )[:MAX_OBSERVED_PRODUCTS]
        )
        retained: list[CryptoProductObservation] = []
        for ordinal, product in enumerate(selected, 1):
            observation_id = f"{capture_id}-{product.product_id.lower()}-daily-observation-v1"
            prior = self._repository.observation(observation_id)
            if prior is not None:
                if (
                    prior.source_capture_id != capture_id
                    or prior.interval_start != interval_start
                    or prior.interval_end != interval_end
                    or prior.evidence_class is not self._source.evidence_class
                ):
                    raise CryptoUniverseConflict("CRYPTO_OBSERVATION_IDENTITY_CONFLICT")
                retained.append(prior)
                continue
            page = self._source.capture_daily_observations(
                product_id=product.product_id,
                interval_start=interval_start,
                interval_end=interval_end,
                requested_at=requested_at,
            )
            raw_hash = hashlib.sha256(page.raw_payload).hexdigest()
            value = CryptoProductObservation.create(
                observation_id=observation_id,
                source_capture_id=capture_id,
                product_id=product.product_id,
                interval_start=interval_start,
                interval_end=interval_end,
                evidence_class=self._source.evidence_class,
                candles=page.candles,
                missing_buckets=page.missing_buckets,
                duplicate_records=page.duplicate_records,
                rejected_records=page.rejected_records,
                request_count=1,
                raw_evidence_hashes=(raw_hash,),
                source_contract_version="COINBASE_ADVANCED_TRADE_PUBLIC_CANDLES_V1",
                parser_version="COINBASE_PUBLIC_DAILY_CANDLE_PARSER_V1",
                checkpoint_ordinal=ordinal,
                completed_at=page.captured_at,
                canonical_asset_id=product.canonical_asset_id or product.base_asset,
            )
            retained.append(self._repository.save_observation(value))
        return tuple(retained)


@dataclass(frozen=True, slots=True)
class CryptoCalibrationHistoryCapture:
    history_id: str
    universe_snapshot_id: str
    interval_start: datetime
    interval_end: datetime
    observations: tuple[CryptoProductObservation, ...]
    request_count: int
    raw_bytes: int
    runtime_milliseconds: int
    idempotent_replay: bool
    authority: str = "NONE"


def _history_partitions(
    interval_start: datetime, interval_end: datetime
) -> tuple[tuple[datetime, datetime], ...]:
    days = (interval_end - interval_start).days
    request_count = (days + 299) // 300
    if not 1 <= request_count <= CRYPTO_CALIBRATION_MAXIMUM_REQUESTS_PER_ASSET:
        raise CryptoUniverseConflict("CRYPTO_CALIBRATION_HISTORY_REQUEST_BOUND_INVALID")
    base, remainder = divmod(days, request_count)
    values: list[tuple[datetime, datetime]] = []
    cursor = interval_start
    for ordinal in range(request_count):
        width = base + (1 if ordinal < remainder else 0)
        next_cursor = cursor + timedelta(days=width)
        values.append((cursor, next_cursor))
        cursor = next_cursor
    if cursor != interval_end or any((end - start).days < 91 for start, end in values):
        raise CryptoUniverseConflict("CRYPTO_CALIBRATION_HISTORY_PARTITION_BOUND_INVALID")
    return tuple(values)


class CaptureCryptoCalibrationHistory:
    """Retain bounded multi-request history for the exact current eligible universe."""

    MAXIMUM_TOTAL_BYTES = 80_000_000
    MAXIMUM_TOTAL_RUNTIME_MILLISECONDS = 300_000

    def __init__(
        self, repository: CryptoUniverseRepository, source: PublicCryptoUniverseSource
    ) -> None:
        self._repository = repository
        self._source = source

    def execute(
        self,
        *,
        history_id: str,
        universe_snapshot_id: str,
        interval_start: datetime,
        interval_end: datetime,
        requested_at: datetime,
    ) -> CryptoCalibrationHistoryCapture:
        start = interval_start.astimezone(UTC)
        end = interval_end.astimezone(UTC)
        requested = requested_at.astimezone(UTC)
        days = (end - start).days
        if (
            any(
                value.hour or value.minute or value.second or value.microsecond
                for value in (start, end)
            )
            or not CRYPTO_CALIBRATION_HISTORY_MINIMUM_DAYS
            <= days
            <= CRYPTO_CALIBRATION_HISTORY_MAXIMUM_DAYS
            or end > requested
        ):
            raise CryptoUniverseConflict("CRYPTO_CALIBRATION_HISTORY_INTERVAL_INVALID")
        snapshot = self._repository.universe_snapshot(universe_snapshot_id)
        if snapshot is None:
            raise CryptoUniverseConflict("CRYPTO_CALIBRATION_HISTORY_UNIVERSE_NOT_FOUND")
        capture = self._repository.product_capture(snapshot.source_capture_id)
        if capture is None:
            raise CryptoUniverseConflict("CRYPTO_CALIBRATION_HISTORY_CAPTURE_NOT_FOUND")
        if capture.evidence_class is not self._source.evidence_class:
            raise CryptoUniverseConflict("CRYPTO_CALIBRATION_HISTORY_EVIDENCE_CLASS_CONFLICT")
        product_by_id = {
            item.product_id: item for item in self._repository.products(snapshot.source_capture_id)
        }
        if not 1 <= len(snapshot.eligible_product_ids) <= 10 or any(
            product_id not in product_by_id for product_id in snapshot.eligible_product_ids
        ):
            raise CryptoUniverseConflict("CRYPTO_CALIBRATION_HISTORY_PRODUCT_SCOPE_INVALID")
        partitions = _history_partitions(start, end)
        observations: list[CryptoProductObservation] = []
        total_requests = 0
        total_bytes = 0
        total_runtime = 0
        replay = True
        for checkpoint_ordinal, product_id in enumerate(snapshot.eligible_product_ids, 1):
            product = product_by_id[product_id]
            observation_id = f"{history_id}-{product.canonical_asset_id.lower()}-history-v1"
            prior = self._repository.observation(observation_id)
            if prior is not None:
                if (
                    prior.source_capture_id != snapshot.source_capture_id
                    or prior.product_id != product_id
                    or prior.interval_start != start
                    or prior.interval_end != end
                    or prior.evidence_class is not self._source.evidence_class
                    or not prior.coverage_partitions
                ):
                    raise CryptoUniverseConflict("CRYPTO_CALIBRATION_HISTORY_IDENTITY_CONFLICT")
                observations.append(prior)
                continue
            replay = False
            candles: dict[datetime, CryptoDailyObservation] = {}
            partition_evidence: list[CryptoHistoryPartition] = []
            missing = 0
            duplicates = 0
            rejected = 0
            completed_at = requested
            for ordinal, (partition_start, partition_end) in enumerate(partitions, 1):
                page = self._source.capture_daily_observations(
                    product_id=product_id,
                    interval_start=partition_start,
                    interval_end=partition_end,
                    requested_at=requested,
                )
                raw_hash = hashlib.sha256(page.raw_payload).hexdigest()
                for candle in page.candles:
                    retained = candles.get(candle.bucket_start)
                    if retained is not None and retained != candle:
                        raise CryptoUniverseConflict(
                            "CRYPTO_CALIBRATION_HISTORY_DUPLICATE_CONFLICT"
                        )
                    candles[candle.bucket_start] = candle
                missing += page.missing_buckets
                duplicates += page.duplicate_records
                rejected += page.rejected_records
                total_requests += 1
                total_bytes += len(page.raw_payload)
                total_runtime += page.runtime_milliseconds
                completed_at = max(completed_at, page.captured_at)
                partition_evidence.append(
                    CryptoHistoryPartition.create(
                        ordinal=ordinal,
                        interval_start=partition_start,
                        interval_end=partition_end,
                        accepted_buckets=len(page.candles),
                        missing_buckets=page.missing_buckets,
                        duplicate_records=page.duplicate_records,
                        rejected_records=page.rejected_records,
                        raw_evidence_hash=raw_hash,
                        source_locator=page.source_locator,
                        runtime_milliseconds=page.runtime_milliseconds,
                    )
                )
                if (
                    total_bytes > self.MAXIMUM_TOTAL_BYTES
                    or total_runtime > self.MAXIMUM_TOTAL_RUNTIME_MILLISECONDS
                ):
                    raise CryptoUniverseConflict("CRYPTO_CALIBRATION_HISTORY_TOTAL_BOUND_EXCEEDED")
            accepted = len(candles)
            coverage_status = (
                "COMPLETE"
                if missing == 0
                else "PARTIAL_LISTING_HISTORY"
                if accepted >= 121
                else "INSUFFICIENT_HISTORY"
            )
            limitations = tuple(
                value
                for value, present in (
                    ("LISTING_HISTORY_DOES_NOT_COVER_REQUESTED_INTERVAL", missing > 0),
                    ("REJECTED_PROVIDER_BUCKETS_RETAINED", rejected > 0),
                    ("DUPLICATE_PROVIDER_BUCKETS_RETAINED", duplicates > 0),
                )
                if present
            )
            observations.append(
                self._repository.save_observation(
                    CryptoProductObservation.create(
                        observation_id=observation_id,
                        source_capture_id=snapshot.source_capture_id,
                        product_id=product_id,
                        interval_start=start,
                        interval_end=end,
                        evidence_class=self._source.evidence_class,
                        candles=tuple(candles.values()),
                        missing_buckets=missing,
                        duplicate_records=duplicates,
                        rejected_records=rejected,
                        request_count=len(partitions),
                        raw_evidence_hashes=tuple(
                            item.raw_evidence_hash for item in partition_evidence
                        ),
                        source_contract_version="COINBASE_ADVANCED_TRADE_PUBLIC_CANDLES_V1",
                        parser_version="COINBASE_PUBLIC_DAILY_CANDLE_PARSER_V1",
                        checkpoint_ordinal=checkpoint_ordinal,
                        completed_at=completed_at,
                        canonical_asset_id=(product.canonical_asset_id or product.base_asset),
                        coverage_partitions=tuple(partition_evidence),
                        coverage_status=coverage_status,
                        coverage_limitations=limitations,
                    )
                )
            )
        return CryptoCalibrationHistoryCapture(
            history_id=history_id,
            universe_snapshot_id=universe_snapshot_id,
            interval_start=start,
            interval_end=end,
            observations=tuple(observations),
            request_count=total_requests,
            raw_bytes=total_bytes,
            runtime_milliseconds=total_runtime,
            idempotent_replay=replay,
        )


class MaterializeCryptoUniverse:
    def __init__(self, repository: CryptoUniverseRepository) -> None:
        self._repository = repository

    def execute(
        self, *, snapshot_id: str, capture_id: str, created_at: datetime
    ) -> CryptoUniverseSnapshot:
        capture = self._repository.product_capture(capture_id)
        if capture is None:
            raise CryptoUniverseConflict("CRYPTO_PRODUCT_CAPTURE_NOT_FOUND")
        prior = self._repository.universe_snapshot(snapshot_id)
        if prior is not None:
            if prior.source_capture_id != capture_id:
                raise CryptoUniverseConflict("CRYPTO_UNIVERSE_SNAPSHOT_IDENTITY_CONFLICT")
            return prior
        value = materialize_universe(
            snapshot_id=snapshot_id,
            capture=capture,
            products=self._repository.products(capture_id),
            observations=tuple(
                item
                for item in self._repository.observations()
                if item.source_capture_id == capture_id
            ),
            created_at=created_at,
        )
        return self._repository.save_universe_snapshot(value)


class RunCryptoDiscovery:
    def __init__(self, repository: CryptoUniverseRepository) -> None:
        self._repository = repository

    def execute(
        self, *, run_id: str, snapshot_id: str, generated_at: datetime
    ) -> tuple[CryptoDiscoveryRun, tuple[CryptoResearchOpportunity, ...]]:
        snapshot = self._repository.universe_snapshot(snapshot_id)
        if snapshot is None:
            raise CryptoUniverseConflict("CRYPTO_UNIVERSE_SNAPSHOT_NOT_FOUND")
        prior = self._repository.discovery_run(run_id)
        if prior is not None:
            if prior.universe_snapshot_id != snapshot_id:
                raise CryptoUniverseConflict("CRYPTO_DISCOVERY_RUN_IDENTITY_CONFLICT")
            return (
                prior,
                tuple(
                    item
                    for item in self._repository.opportunities()
                    if item.discovery_run_id == run_id
                ),
            )
        run = self._repository.save_discovery_run(
            run_discovery(
                run_id=run_id,
                snapshot=snapshot,
                observations=tuple(
                    item
                    for item in self._repository.observations()
                    if item.source_capture_id == snapshot.source_capture_id
                ),
                generated_at=generated_at,
            )
        )
        return run, self._repository.save_opportunities(build_opportunities(run))


def crypto_universe_workspace(repository: CryptoUniverseRepository) -> dict[str, object]:
    captures = repository.product_captures()
    snapshots = repository.universe_snapshots()
    runs = repository.discovery_runs()
    opportunities = repository.opportunities()
    current_capture = captures[-1] if captures else None
    current_snapshot = snapshots[-1] if snapshots else None
    current_run = runs[-1] if runs else None
    return {
        "policy": {
            "policy_id": CRYPTO_UNIVERSE_POLICY_V2,
            "version": 1,
            "maximum_eligible_products": 10,
            "maximum_history_assets": MAX_OBSERVED_PRODUCTS,
            "supported_quotes": ("USD", "USDC"),
            "fail_closed": True,
        },
        "product_capture": current_capture,
        "products": (repository.products(current_capture.capture_id) if current_capture else ()),
        "observations": repository.observations(),
        "universe_snapshot": current_snapshot,
        "catalog_pagination_status": (
            current_capture.pagination_status if current_capture else None
        ),
        "catalog_complete": current_capture.catalog_complete if current_capture else False,
        "product_route_count": len(repository.products(current_capture.capture_id))
        if current_capture
        else 0,
        "canonical_base_asset_count": (
            len(
                {
                    item.canonical_asset_id or item.base_asset
                    for item in repository.products(current_capture.capture_id)
                }
            )
            if current_capture
            else 0
        ),
        "route_groups": current_snapshot.asset_groups if current_snapshot else (),
        "history_shortlist": (
            current_snapshot.history_shortlist_asset_ids if current_snapshot else ()
        ),
        "eligible_assets": current_snapshot.eligible_asset_ids if current_snapshot else (),
        "discovery_run": current_run,
        "rankings": current_run.results if current_run else (),
        "opportunities": opportunities,
        "empty_state": (
            None if current_capture else "NO MULTI-ASSET CRYPTO UNIVERSE EVIDENCE RETAINED"
        ),
        "network_behavior": "EXPLICIT BOUNDED PUBLIC GET ONLY",
        "research_only": True,
        "paper_eligible": False,
        "live_eligible": False,
        "capital_authority": "NONE",
        "broker_authority": "NONE",
        "financial_mutation_authority": "NONE",
        "orders": 0,
        "positions": 0,
        "reservations": 0,
        "fills": 0,
        "journals": 0,
    }
