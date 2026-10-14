"""Pure rules for bounded, authority-free crypto market-pulse refreshes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from apex.domain.crypto_universe import (
    CRYPTO_DISCOVERY_V1,
    CRYPTO_UNIVERSE_POLICY_V2,
    CryptoProductObservation,
    CryptoResearchOpportunity,
    CryptoUniverseSnapshot,
    OpportunityStatus,
)
from apex.domain.values import content_digest

CRYPTO_MARKET_PULSE_PLAN_V1 = "CRYPTO_MARKET_PULSE_DAILY_V1"
CRYPTO_MARKET_PULSE_PLAN_V2 = "CRYPTO_MARKET_PULSE_DAILY_WITH_PUBLIC_BOOK_V2"
CRYPTO_OPPORTUNITY_POLICY_V1 = "CRYPTO_RESEARCH_PRIORITY_PERSISTENCE_V1"
CRYPTO_MARKET_PULSE_WARNING = "CRYPTO MARKET PULSE — RESEARCH ONLY — AUTHORITY NONE"
MAX_PULSE_PRODUCTS = 10
PULSE_HISTORY_DAYS = 180
PULSE_FRESHNESS_HOURS = 36
PULSE_MAX_REQUESTS = 10
PULSE_MAX_REQUESTS_WITH_BOOK = 20
PULSE_MAX_BOOK_DEPTH = 20
PULSE_MAX_BYTES = 20_000_000
PULSE_REQUEST_TIMEOUT_SECONDS = 15
PULSE_MAX_RUNTIME_MILLISECONDS = 150_000


class CryptoMarketPulseError(ValueError):
    pass


class RefreshState(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED_NOT_DUE = "SKIPPED_NOT_DUE"


class PulseQuality(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class OpportunityTransition(StrEnum):
    NEWLY_POSITIVE = "NEWLY_POSITIVE"
    PERSISTENT_POSITIVE = "PERSISTENT_POSITIVE"
    IMPROVING = "IMPROVING"
    WEAKENING = "WEAKENING"
    REVERSED_TO_NON_POSITIVE = "REVERSED_TO_NON_POSITIVE"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"
    DATA_QUALITY_UNAVAILABLE = "DATA_QUALITY_UNAVAILABLE"


class PersistenceClassification(StrEnum):
    CURRENT_POSITIVE = "CURRENT_POSITIVE"
    PERSISTENT_POSITIVE = "PERSISTENT_POSITIVE"
    REVERSED = "REVERSED"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    DATA_QUALITY_BLOCKED = "DATA_QUALITY_BLOCKED"


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None:
        raise CryptoMarketPulseError(reason)
    return value.astimezone(UTC)


def _identity(value: str, reason: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 160:
        raise CryptoMarketPulseError(reason)
    return normalized


def completed_utc_day(as_of: datetime) -> datetime:
    value = _time(as_of, "CRYPTO_PULSE_AS_OF_INVALID")
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def daily_refresh_due(
    *,
    as_of: datetime,
    completed_as_of_times: tuple[datetime, ...],
) -> bool:
    target = completed_utc_day(as_of)
    return not any(completed_utc_day(item) >= target for item in completed_as_of_times)


def universe_refresh_due(*, universe_created_at: datetime, as_of: datetime) -> bool:
    return completed_utc_day(as_of) - _time(
        universe_created_at, "CRYPTO_PULSE_UNIVERSE_TIME_INVALID"
    ) >= timedelta(days=7)


@dataclass(frozen=True, slots=True)
class CryptoRefreshPlan:
    plan_id: str
    version: int
    provider: str
    universe_policy_id: str
    universe_snapshot_id: str
    maximum_product_count: int
    observation_cadence_hours: int
    universe_refresh_cadence_days: int
    required_observation_types: tuple[str, ...]
    freshness_limit_hours: int
    maximum_requests: int
    maximum_bytes: int
    request_timeout_seconds: int
    maximum_runtime_milliseconds: int
    discovery_method_id: str
    opportunity_policy_id: str
    actor: str
    enabled: bool
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        universe_snapshot_id: str,
        actor: str,
        created_at: datetime,
        enabled: bool = True,
    ) -> CryptoRefreshPlan:
        timestamp = _time(created_at, "CRYPTO_PULSE_PLAN_TIME_INVALID")
        payload = (
            _identity(plan_id, "CRYPTO_PULSE_PLAN_ID_INVALID"),
            1,
            "COINBASE",
            CRYPTO_UNIVERSE_POLICY_V2,
            _identity(universe_snapshot_id, "CRYPTO_PULSE_UNIVERSE_ID_INVALID"),
            MAX_PULSE_PRODUCTS,
            24,
            7,
            ("COMPLETED_DAILY_CANDLES", "CURRENT_RETAINED_PRODUCT_STATUS"),
            PULSE_FRESHNESS_HOURS,
            PULSE_MAX_REQUESTS,
            PULSE_MAX_BYTES,
            PULSE_REQUEST_TIMEOUT_SECONDS,
            PULSE_MAX_RUNTIME_MILLISECONDS,
            CRYPTO_DISCOVERY_V1,
            CRYPTO_OPPORTUNITY_POLICY_V1,
            _identity(actor, "CRYPTO_PULSE_ACTOR_INVALID"),
            enabled,
            timestamp,
            "NONE",
            False,
            False,
            "NONE",
        )
        return cls(
            *payload[:-4],
            canonical_hash=content_digest(payload),
            authority="NONE",
            paper_eligible=False,
            live_eligible=False,
            capital_authority="NONE",
        )

    @classmethod
    def create_with_current_market_evidence(
        cls,
        *,
        plan_id: str,
        universe_snapshot_id: str,
        actor: str,
        created_at: datetime,
        enabled: bool = True,
    ) -> CryptoRefreshPlan:
        """Create the additive v2 plan while preserving all v1 identities."""

        timestamp = _time(created_at, "CRYPTO_PULSE_PLAN_TIME_INVALID")
        payload = (
            _identity(plan_id, "CRYPTO_PULSE_PLAN_ID_INVALID"),
            2,
            "COINBASE",
            CRYPTO_UNIVERSE_POLICY_V2,
            _identity(universe_snapshot_id, "CRYPTO_PULSE_UNIVERSE_ID_INVALID"),
            MAX_PULSE_PRODUCTS,
            24,
            7,
            (
                "COMPLETED_DAILY_CANDLES",
                "CURRENT_RETAINED_PRODUCT_STATUS",
                "CURRENT_PUBLIC_ORDER_BOOK",
            ),
            PULSE_FRESHNESS_HOURS,
            PULSE_MAX_REQUESTS_WITH_BOOK,
            PULSE_MAX_BYTES,
            PULSE_REQUEST_TIMEOUT_SECONDS,
            PULSE_MAX_RUNTIME_MILLISECONDS,
            CRYPTO_DISCOVERY_V1,
            CRYPTO_OPPORTUNITY_POLICY_V1,
            _identity(actor, "CRYPTO_PULSE_ACTOR_INVALID"),
            enabled,
            timestamp,
            "NONE",
            False,
            False,
            "NONE",
        )
        return cls(
            *payload[:-4],
            canonical_hash=content_digest(payload),
            authority="NONE",
            paper_eligible=False,
            live_eligible=False,
            capital_authority="NONE",
        )


@dataclass(frozen=True, slots=True)
class CryptoRefreshRun:
    refresh_id: str
    plan_id: str
    as_of: datetime
    starting_universe_snapshot_id: str
    request_product_ids: tuple[str, ...]
    universe_refresh_due: bool
    interval_start: datetime
    interval_end: datetime
    maximum_requests: int
    maximum_bytes: int
    maximum_runtime_milliseconds: int
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        refresh_id: str,
        plan: CryptoRefreshPlan,
        universe: CryptoUniverseSnapshot,
        as_of: datetime,
        created_at: datetime,
    ) -> CryptoRefreshRun:
        target = completed_utc_day(as_of)
        routes = tuple(universe.eligible_product_ids[: plan.maximum_product_count])
        if not routes or len(routes) > MAX_PULSE_PRODUCTS:
            raise CryptoMarketPulseError("CRYPTO_PULSE_PRODUCT_BOUND_INVALID")
        payload = (
            _identity(refresh_id, "CRYPTO_PULSE_REFRESH_ID_INVALID"),
            plan.plan_id,
            target,
            universe.snapshot_id,
            routes,
            universe_refresh_due(universe_created_at=universe.created_at, as_of=target),
            target - timedelta(days=PULSE_HISTORY_DAYS),
            target,
            plan.maximum_requests,
            plan.maximum_bytes,
            plan.maximum_runtime_milliseconds,
            _time(created_at, "CRYPTO_PULSE_REFRESH_TIME_INVALID"),
            "NONE",
        )
        return cls(*payload[:-1], canonical_hash=content_digest(payload), authority="NONE")


@dataclass(frozen=True, slots=True)
class CryptoRefreshEvent:
    event_id: str
    refresh_id: str
    ordinal: int
    state: RefreshState
    occurred_at: datetime
    warnings: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        refresh_id: str,
        ordinal: int,
        state: RefreshState,
        occurred_at: datetime,
        warnings: tuple[str, ...] = (),
    ) -> CryptoRefreshEvent:
        if not 1 <= ordinal <= 4:
            raise CryptoMarketPulseError("CRYPTO_PULSE_EVENT_ORDINAL_INVALID")
        event_id = f"{refresh_id}-event-{ordinal}-{state.value.lower()}"
        payload = (
            event_id,
            refresh_id,
            ordinal,
            state,
            _time(occurred_at, "CRYPTO_PULSE_EVENT_TIME_INVALID"),
            tuple(sorted(set(warnings))),
            "NONE",
        )
        return cls(*payload[:-1], canonical_hash=content_digest(payload), authority="NONE")


@dataclass(frozen=True, slots=True)
class CryptoPulseCheckpoint:
    checkpoint_id: str
    refresh_id: str
    ordinal: int
    canonical_asset_id: str
    product_id: str
    product_status: str
    status_observed_at: datetime
    observation: CryptoProductObservation | None
    latest_completed_close: Decimal | None
    freshness_seconds: int | None
    quality: PulseQuality
    quality_reasons: tuple[str, ...]
    raw_bytes: int
    runtime_milliseconds: int
    source_locator: str | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    midpoint: Decimal | None
    spread_bps: Decimal | None
    visible_depth: Decimal | None
    quote_limitation: str
    canonical_hash: str
    authority: str = "NONE"
    quote_captured_at: datetime | None = None
    quote_raw_evidence_hash: str | None = None
    visible_bid_depth: Decimal | None = None
    visible_ask_depth: Decimal | None = None

    @classmethod
    def create(
        cls,
        *,
        refresh_id: str,
        ordinal: int,
        canonical_asset_id: str,
        product_id: str,
        product_status: str,
        status_observed_at: datetime,
        observation: CryptoProductObservation | None,
        as_of: datetime,
        raw_bytes: int,
        runtime_milliseconds: int,
        source_locator: str | None,
        failure_reason: str | None = None,
        best_bid: Decimal | None = None,
        best_ask: Decimal | None = None,
        midpoint: Decimal | None = None,
        spread_bps: Decimal | None = None,
        visible_bid_depth: Decimal | None = None,
        visible_ask_depth: Decimal | None = None,
        quote_captured_at: datetime | None = None,
        quote_raw_evidence_hash: str | None = None,
        quote_failure_reason: str | None = None,
    ) -> CryptoPulseCheckpoint:
        if not 1 <= ordinal <= MAX_PULSE_PRODUCTS:
            raise CryptoMarketPulseError("CRYPTO_PULSE_CHECKPOINT_ORDINAL_INVALID")
        target = completed_utc_day(as_of)
        reasons: list[str] = []
        freshness: int | None = None
        latest_close: Decimal | None = None
        if failure_reason:
            reasons.append(failure_reason)
        quote_values = (
            best_bid,
            best_ask,
            midpoint,
            spread_bps,
            visible_bid_depth,
            visible_ask_depth,
        )
        has_quote = all(value is not None for value in quote_values)
        if any(value is not None for value in quote_values) and not has_quote:
            reasons.append("PUBLIC_ORDER_BOOK_INCOMPLETE")
        if quote_failure_reason:
            reasons.append(quote_failure_reason)
        if has_quote:
            assert best_bid is not None
            assert best_ask is not None
            assert midpoint is not None
            assert spread_bps is not None
            assert visible_ask_depth is not None
            if best_bid <= 0 or best_ask <= best_bid or midpoint <= 0 or spread_bps < 0:
                reasons.append("PUBLIC_ORDER_BOOK_INVALID")
            if visible_ask_depth <= 0 or (visible_bid_depth or Decimal(0)) <= 0:
                reasons.append("PUBLIC_ORDER_BOOK_DEPTH_INSUFFICIENT")
            if quote_captured_at is None or not quote_raw_evidence_hash:
                reasons.append("PUBLIC_ORDER_BOOK_LINEAGE_MISSING")
        if observation is None:
            reasons.append("REQUIRED_DAILY_OBSERVATION_UNAVAILABLE")
        else:
            if any(item.bucket_start >= target for item in observation.candles):
                reasons.append("INCOMPLETE_CURRENT_UTC_DAY_PRESENT")
            if len(observation.candles) < 91:
                reasons.append("DISCOVERY_HISTORY_INSUFFICIENT")
            if observation.missing_buckets:
                reasons.append("OBSERVATION_MISSING_BUCKETS")
            if observation.duplicate_records or observation.rejected_records:
                reasons.append("OBSERVATION_DATA_QUALITY_FAILED")
            if observation.candles:
                latest = observation.candles[-1]
                latest_close = latest.close
                freshness = max(
                    0, int((target - (latest.bucket_start + timedelta(days=1))).total_seconds())
                )
                if freshness > PULSE_FRESHNESS_HOURS * 3600:
                    reasons.append("OBSERVATION_STALE")
        quality = PulseQuality.PASSED if not reasons else PulseQuality.FAILED
        checkpoint_id = f"{refresh_id}-{canonical_asset_id.lower()}-checkpoint"
        legacy_payload = (
            checkpoint_id,
            refresh_id,
            ordinal,
            canonical_asset_id,
            product_id,
            product_status,
            _time(status_observed_at, "CRYPTO_PULSE_STATUS_TIME_INVALID"),
            observation,
            latest_close,
            freshness,
            quality,
            tuple(sorted(set(reasons))),
            raw_bytes,
            runtime_milliseconds,
            source_locator,
            best_bid,
            best_ask,
            midpoint,
            spread_bps,
            visible_ask_depth,
            (
                "BOUNDED_PUBLIC_ORDER_BOOK_RETAINED"
                if has_quote
                else "PUBLIC_TOP_OF_BOOK_NOT_CAPTURED_BY_SUPPORTED_BOUNDARY"
            ),
            "NONE",
        )
        if not has_quote:
            return cls(
                *legacy_payload[:-1],
                canonical_hash=content_digest(legacy_payload),
                authority="NONE",
            )
        quote_time = (
            None
            if quote_captured_at is None
            else _time(quote_captured_at, "CRYPTO_PULSE_QUOTE_TIME_INVALID")
        )
        enriched_payload = (
            *legacy_payload,
            quote_time,
            quote_raw_evidence_hash,
            visible_bid_depth,
            visible_ask_depth,
        )
        return cls(
            *legacy_payload[:-1],
            canonical_hash=content_digest(enriched_payload),
            authority="NONE",
            quote_captured_at=quote_time,
            quote_raw_evidence_hash=quote_raw_evidence_hash,
            visible_bid_depth=visible_bid_depth,
            visible_ask_depth=visible_ask_depth,
        )


@dataclass(frozen=True, slots=True)
class CryptoPulseSnapshot:
    pulse_id: str
    refresh_id: str
    universe_snapshot_id: str
    as_of: datetime
    eligible_assets: tuple[str, ...]
    selected_routes: tuple[tuple[str, str], ...]
    checkpoint_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    event_time: datetime
    ingested_at: datetime
    maximum_freshness_seconds: int
    missing_observation_count: int
    current_statuses: tuple[tuple[str, str], ...]
    manifest_hash: str
    limitations: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"


def build_pulse_snapshot(
    *,
    pulse_id: str,
    refresh: CryptoRefreshRun,
    checkpoints: tuple[CryptoPulseCheckpoint, ...],
    ingested_at: datetime,
) -> CryptoPulseSnapshot:
    ordered = tuple(sorted(checkpoints, key=lambda item: item.ordinal))
    if (
        len(ordered) != len(refresh.request_product_ids)
        or any(item.refresh_id != refresh.refresh_id for item in ordered)
        or any(item.quality is not PulseQuality.PASSED for item in ordered)
    ):
        raise CryptoMarketPulseError("CRYPTO_PULSE_SNAPSHOT_CHECKPOINTS_INCOMPLETE")
    assets = tuple(item.canonical_asset_id for item in ordered)
    if len(set(assets)) != len(assets):
        raise CryptoMarketPulseError("CRYPTO_PULSE_DUPLICATE_CANONICAL_ASSET")
    routes = tuple((item.canonical_asset_id, item.product_id) for item in ordered)
    observations = tuple(
        item.observation.observation_id for item in ordered if item.observation is not None
    )
    manifest = content_digest(tuple(item.canonical_hash for item in ordered))
    payload = (
        _identity(pulse_id, "CRYPTO_PULSE_SNAPSHOT_ID_INVALID"),
        refresh.refresh_id,
        refresh.starting_universe_snapshot_id,
        refresh.as_of,
        assets,
        routes,
        tuple(item.checkpoint_id for item in ordered),
        observations,
        refresh.as_of,
        _time(ingested_at, "CRYPTO_PULSE_INGESTION_TIME_INVALID"),
        max((item.freshness_seconds or 0 for item in ordered), default=0),
        0,
        tuple((item.product_id, item.product_status) for item in ordered),
        manifest,
        (
            *(
                ("BOUNDED_PUBLIC_ORDER_BOOK_IS_VISIBLE_EVIDENCE_NOT_FILLABILITY",)
                if all(item.best_bid is not None for item in ordered)
                else ("PUBLIC_TOP_OF_BOOK_NOT_CAPTURED_BY_SUPPORTED_BOUNDARY",)
            ),
            "PUBLIC_CANDLES_AND_RETAINED_PRODUCT_STATUS_ARE_OBSERVATION_EVIDENCE_ONLY",
        ),
        "NONE",
    )
    return CryptoPulseSnapshot(
        *payload[:-1], canonical_hash=content_digest(payload), authority="NONE"
    )


@dataclass(frozen=True, slots=True)
class CryptoOpportunityLineage:
    lineage_id: str
    refresh_id: str
    pulse_id: str
    as_of: datetime
    canonical_asset_id: str
    product_id: str
    current_opportunity_id: str | None
    prior_opportunity_id: str | None
    transition: OpportunityTransition
    persistence: PersistenceClassification
    first_positive_at: datetime | None
    consecutive_positive_refreshes: int
    comparable_refresh_count: int
    current_score: Decimal | None
    prior_score: Decimal | None
    score_delta: Decimal | None
    current_rank: int | None
    prior_rank: int | None
    rank_delta: int | None
    expiration: datetime | None
    superseding_opportunity_id: str | None
    transition_reasons: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"


def build_opportunity_lineage(
    *,
    refresh_id: str,
    pulse_id: str,
    as_of: datetime,
    asset_id: str,
    product_id: str,
    current: CryptoResearchOpportunity | None,
    prior: CryptoResearchOpportunity | None,
    prior_lineage: CryptoOpportunityLineage | None,
    quality_blocked: bool,
) -> CryptoOpportunityLineage:
    target = completed_utc_day(as_of)
    current_positive = current is not None and current.status is OpportunityStatus.RESEARCH_ONLY
    prior_positive = prior is not None and prior.status is OpportunityStatus.RESEARCH_ONLY
    comparable = (prior_lineage.comparable_refresh_count + 1) if prior_lineage else 1
    reasons: list[str] = []
    if quality_blocked:
        transition = OpportunityTransition.DATA_QUALITY_UNAVAILABLE
        persistence = PersistenceClassification.DATA_QUALITY_BLOCKED
        consecutive = 0
        first_positive = prior_lineage.first_positive_at if prior_lineage else None
    elif prior_positive and not current_positive:
        assert prior is not None
        transition = OpportunityTransition.REVERSED_TO_NON_POSITIVE
        persistence = PersistenceClassification.REVERSED
        consecutive = 0
        first_positive = prior_lineage.first_positive_at if prior_lineage else prior.generated_at
    elif current_positive:
        assert current is not None
        consecutive = (
            prior_lineage.consecutive_positive_refreshes + 1
            if prior_lineage
            and prior_positive
            and target - prior_lineage.as_of == timedelta(days=1)
            else 1
        )
        first_positive = (
            prior_lineage.first_positive_at
            if prior_lineage and prior_lineage.first_positive_at
            else current.generated_at
        )
        persistence = (
            PersistenceClassification.PERSISTENT_POSITIVE
            if consecutive >= 3
            else PersistenceClassification.CURRENT_POSITIVE
        )
        if not prior_positive:
            transition = OpportunityTransition.NEWLY_POSITIVE
        elif prior is not None and current.gross_research_score > prior.gross_research_score:
            transition = OpportunityTransition.IMPROVING
        elif prior is not None and current.gross_research_score < prior.gross_research_score:
            transition = OpportunityTransition.WEAKENING
        else:
            transition = OpportunityTransition.PERSISTENT_POSITIVE
    else:
        transition = OpportunityTransition.REJECTED
        persistence = PersistenceClassification.INSUFFICIENT_HISTORY
        consecutive = 0
        first_positive = prior_lineage.first_positive_at if prior_lineage else None
    if prior is not None:
        reasons.append("PRIOR_OPPORTUNITY_SUPERSEDED")
        if prior.valid_until <= target:
            reasons.append("PRIOR_OPPORTUNITY_EXPIRED")
    if current is not None and current.status is OpportunityStatus.REJECTED:
        reasons.extend(current.rejection_reasons)
    current_score = current.gross_research_score if current else None
    prior_score = prior.gross_research_score if prior else None
    current_rank = current.rank if current else None
    prior_rank = prior.rank if prior else None
    lineage_id = f"{refresh_id}-{asset_id.lower()}-lineage"
    payload = (
        lineage_id,
        refresh_id,
        pulse_id,
        target,
        asset_id,
        product_id,
        current.opportunity_id if current else None,
        prior.opportunity_id if prior else None,
        transition,
        persistence,
        first_positive,
        consecutive,
        comparable,
        current_score,
        prior_score,
        current_score - prior_score
        if current_score is not None and prior_score is not None
        else None,
        current_rank,
        prior_rank,
        current_rank - prior_rank if current_rank is not None and prior_rank is not None else None,
        current.valid_until if current else None,
        current.opportunity_id if current and prior else None,
        tuple(sorted(set(reasons))),
        "NONE",
    )
    return CryptoOpportunityLineage(
        *payload[:-1], canonical_hash=content_digest(payload), authority="NONE"
    )


@dataclass(frozen=True, slots=True)
class CryptoMarketChangeSummary:
    summary_id: str
    refresh_id: str
    pulse_id: str
    prior_pulse_id: str | None
    assets_added: tuple[str, ...]
    assets_removed: tuple[str, ...]
    route_changes: tuple[str, ...]
    newly_positive: tuple[str, ...]
    persistent_positive: tuple[str, ...]
    reversals: tuple[str, ...]
    expired_opportunities: tuple[str, ...]
    largest_score_increases: tuple[tuple[str, Decimal], ...]
    largest_score_decreases: tuple[tuple[str, Decimal], ...]
    rank_changes: tuple[tuple[str, int], ...]
    market_breadth: Decimal
    positive_score_count: int
    rejected_count: int
    data_quality_failures: tuple[str, ...]
    warnings: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"


def build_change_summary(
    *,
    refresh_id: str,
    pulse: CryptoPulseSnapshot,
    prior_pulse: CryptoPulseSnapshot | None,
    lineages: tuple[CryptoOpportunityLineage, ...],
) -> CryptoMarketChangeSummary:
    current_assets = set(pulse.eligible_assets)
    prior_assets = set(prior_pulse.eligible_assets) if prior_pulse else set()
    prior_routes = dict(prior_pulse.selected_routes) if prior_pulse else {}
    route_changes = tuple(
        sorted(
            f"{asset}:{prior_routes[asset]}->{route}"
            for asset, route in pulse.selected_routes
            if asset in prior_routes and prior_routes[asset] != route
        )
    )
    positive = tuple(item for item in lineages if (item.current_score or Decimal(0)) > 0)
    score_deltas = tuple(
        (item.canonical_asset_id, item.score_delta)
        for item in lineages
        if item.score_delta is not None
    )
    rank_deltas = tuple(
        (item.canonical_asset_id, item.rank_delta)
        for item in lineages
        if item.rank_delta is not None
    )
    summary_id = f"{refresh_id}-change-summary"
    values = (
        summary_id,
        refresh_id,
        pulse.pulse_id,
        prior_pulse.pulse_id if prior_pulse else None,
        tuple(sorted(current_assets - prior_assets)),
        tuple(sorted(prior_assets - current_assets)),
        route_changes,
        tuple(
            sorted(
                item.canonical_asset_id
                for item in lineages
                if item.transition is OpportunityTransition.NEWLY_POSITIVE
            )
        ),
        tuple(
            sorted(
                item.canonical_asset_id
                for item in lineages
                if item.persistence is PersistenceClassification.PERSISTENT_POSITIVE
            )
        ),
        tuple(
            sorted(
                item.canonical_asset_id
                for item in lineages
                if item.persistence is PersistenceClassification.REVERSED
            )
        ),
        tuple(
            sorted(
                item.prior_opportunity_id or ""
                for item in lineages
                if "PRIOR_OPPORTUNITY_EXPIRED" in item.transition_reasons
            )
        ),
        tuple(sorted(score_deltas, key=lambda item: (-item[1], item[0]))[:5]),
        tuple(sorted(score_deltas, key=lambda item: (item[1], item[0]))[:5]),
        tuple(sorted(rank_deltas, key=lambda item: (-abs(item[1]), item[0]))[:10]),
        (Decimal(len(positive)) / Decimal(len(lineages)) if lineages else Decimal(0)),
        len(positive),
        sum(item.current_score is not None and item.current_score <= 0 for item in lineages),
        tuple(
            sorted(
                item.canonical_asset_id
                for item in lineages
                if item.persistence is PersistenceClassification.DATA_QUALITY_BLOCKED
            )
        ),
        (CRYPTO_MARKET_PULSE_WARNING,),
        "NONE",
    )
    return CryptoMarketChangeSummary(
        *values[:-1],
        canonical_hash=content_digest(values),
        authority="NONE",
    )
