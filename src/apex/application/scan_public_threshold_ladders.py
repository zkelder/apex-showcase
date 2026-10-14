"""Bounded public threshold-ladder research over exact Kalshi event evidence.

The orchestration is foreground-only.  It keeps network requests and sleeps
outside persistence transactions and has no financial or execution imports.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from apex.application.capture_public_orderbook_session import (
    BoundedOrderbookCaptureRequest,
    CaptureBoundedPublicOrderbookSession,
    CaptureMarketCandidate,
    MarketRefresh,
    PublicOrderbookCaptureError,
    PublicOrderbookCaptureTerminalError,
)
from apex.application.discover_public_events import CapturedPublicEventPage
from apex.application.discover_public_markets import (
    CapturedPublicMarketPage,
    DiscoverPublicPredictionMarkets,
    PublicMarketNormalizer,
    PublicMarketScanRequest,
    PublicMarketScanSummary,
)
from apex.application.scan_public_binary_complements import (
    CapturedKalshiPublicResponse,
    ComplementMarket,
    RawComplementResponse,
    _fee_snapshot,
    _normalize_orderbook,
    _raw_response_id,
)
from apex.domain.public_complement import (
    PublicOrderbookLevel,
    PublicOrderbookSnapshot,
    SeriesFeeSnapshot,
)
from apex.domain.public_event import (
    EventMarketRelationship,
    NormalizedPublicEventSnapshot,
    link_event_markets,
)
from apex.domain.public_market import (
    NormalizedPublicMarketSnapshot,
    PublicMarketDataQualityState,
    ShadowMarketEvaluation,
    evaluate_public_market_snapshot,
)
from apex.domain.public_threshold import (
    ThresholdBundleEvaluation,
    ThresholdEvaluationReasonCode,
    ThresholdEvaluationState,
    ThresholdMarketEvidence,
    ThresholdMarketRelationship,
    ThresholdMatchedSlice,
    ThresholdRelationshipState,
    build_threshold_relationships,
    evaluate_threshold_bundle,
    threshold_market_evidence,
)
from apex.domain.values import ContentHash, content_digest, deterministic_id, parse_decimal

THRESHOLD_SCAN_VERSION = "public-kalshi-threshold-ladder-scan-v1"


class ThresholdLadderScanState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class ThresholdResearchLabel(StrEnum):
    PERSISTING_CANDIDATE = "PERSISTING_CANDIDATE"
    CANDIDATE = "CANDIDATE"
    FEE_ELIMINATED = "FEE_ELIMINATED"
    ROUNDING_ELIMINATED = "ROUNDING_ELIMINATED"
    GROSS_LOWER_BOUND_NOT_POSITIVE = "GROSS_LOWER_BOUND_NOT_POSITIVE"
    INSUFFICIENT_BOOK = "INSUFFICIENT_BOOK"
    SKEW_INELIGIBLE = "SKEW_INELIGIBLE"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"


class PublicThresholdLadderError(RuntimeError):
    """The bounded public threshold scan cannot proceed safely."""


class PublicThresholdLadderConflictError(PublicThresholdLadderError):
    """A deterministic public-evidence identity conflicts with retained content."""


@dataclass(frozen=True, slots=True)
class PublicThresholdLadderScanRequest:
    scan_id: str
    event_count: int = 5
    maximum_pages: int = 3
    market_limit: int = 300
    maximum_enrichments: int = 40
    maximum_markets_per_event: int = 8
    maximum_relationships: int = 100
    rounds: int = 3
    interval_seconds: int = 10
    maximum_depth: int = 5
    maximum_bundle_quantity: str | int | Decimal = "10"
    maximum_observation_skew_seconds: int = 5
    timeout_seconds: int = 10

    def __post_init__(self) -> None:
        if not self.scan_id or len(self.scan_id) > 160:
            raise ValueError("threshold scan identity is required")
        if isinstance(self.event_count, bool) or not 1 <= self.event_count <= 10:
            raise ValueError("threshold event count must be between one and ten")
        if isinstance(self.maximum_pages, bool) or not 1 <= self.maximum_pages <= 3:
            raise ValueError("threshold maximum pages must be between one and three")
        if isinstance(self.market_limit, bool) or not 50 <= self.market_limit <= 300:
            raise ValueError("threshold market limit must be between fifty and three hundred")
        if (
            isinstance(self.maximum_enrichments, bool)
            or not self.event_count <= self.maximum_enrichments <= 40
        ):
            raise ValueError("threshold enrichments must be between selected events and forty")
        if (
            isinstance(self.maximum_markets_per_event, bool)
            or not 2 <= self.maximum_markets_per_event <= 12
        ):
            raise ValueError("threshold markets per event must be between two and twelve")
        if (
            isinstance(self.maximum_relationships, bool)
            or not 1 <= self.maximum_relationships <= 100
        ):
            raise ValueError("threshold relationships must be between one and one hundred")
        if isinstance(self.rounds, bool) or not 2 <= self.rounds <= 5:
            raise ValueError("threshold rounds must be between two and five")
        if isinstance(self.interval_seconds, bool) or not 5 <= self.interval_seconds <= 60:
            raise ValueError("threshold interval must be between five and sixty seconds")
        if isinstance(self.maximum_depth, bool) or not 1 <= self.maximum_depth <= 20:
            raise ValueError("threshold depth must be between one and twenty")
        quantity = parse_decimal(self.maximum_bundle_quantity, allow_zero=False)
        if quantity > 100:
            raise ValueError("threshold bundle quantity must not exceed one hundred")
        object.__setattr__(self, "maximum_bundle_quantity", quantity)
        if (
            isinstance(self.maximum_observation_skew_seconds, bool)
            or not 1 <= self.maximum_observation_skew_seconds <= 15
        ):
            raise ValueError("threshold observation skew must be between one and fifteen seconds")
        if isinstance(self.timeout_seconds, bool) or not 1 <= self.timeout_seconds <= 30:
            raise ValueError("threshold timeout must be between one and thirty seconds")

    @property
    def maximum_observation_skew(self) -> timedelta:
        return timedelta(seconds=self.maximum_observation_skew_seconds)

    @property
    def canonical_configuration_hash(self) -> ContentHash:
        return ContentHash(
            content_digest(
                {
                    "threshold_scan_version": THRESHOLD_SCAN_VERSION,
                    "event_count": self.event_count,
                    "maximum_pages": self.maximum_pages,
                    "market_limit": self.market_limit,
                    "maximum_enrichments": self.maximum_enrichments,
                    "maximum_markets_per_event": self.maximum_markets_per_event,
                    "maximum_relationships": self.maximum_relationships,
                    "rounds": self.rounds,
                    "interval_seconds": self.interval_seconds,
                    "maximum_depth": self.maximum_depth,
                    "maximum_bundle_quantity": self.maximum_bundle_quantity,
                    "maximum_observation_skew_seconds": self.maximum_observation_skew_seconds,
                    "timeout_seconds": self.timeout_seconds,
                }
            )
        )

    @property
    def evidence_ingestion_run_id(self) -> str:
        return deterministic_id(
            "provider_ingestion_run",
            {"threshold_scan_id": self.scan_id, "capability": "PUBLIC_REST_THRESHOLD_LADDER_SCAN"},
        )


@dataclass(frozen=True, slots=True)
class ThresholdLadderScan:
    scan_id: str
    evidence_ingestion_run_id: str
    request: PublicThresholdLadderScanRequest
    started_at: datetime
    completed_at: datetime | None
    state: ThresholdLadderScanState
    selected_event_count: int
    relationship_count: int
    evaluation_count: int
    candidate_count: int
    persisting_candidate_count: int
    canonical_input_hash: ContentHash
    canonical_result_hash: ContentHash | None
    last_error_code: str | None

    def __post_init__(self) -> None:
        if not self.scan_id or not self.evidence_ingestion_run_id:
            raise ValueError("threshold scan identity is required")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("threshold scan start must be timezone-aware")
        object.__setattr__(self, "started_at", self.started_at.astimezone(UTC))
        if self.completed_at is not None:
            if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
                raise ValueError("threshold scan completion must be timezone-aware")
            object.__setattr__(self, "completed_at", self.completed_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class ThresholdEventReport:
    provider_event_ticker: str
    series_ticker: str
    market_tickers: tuple[str, ...]
    verified_relationship_count: int
    open_interest: Decimal | None
    volume_24h: Decimal | None
    liquidity: Decimal | None
    close_time: datetime | None


@dataclass(frozen=True, slots=True)
class ThresholdResearchResult:
    relationship_id: str
    provider_event_ticker: str
    broader_market_ticker: str
    narrower_market_ticker: str
    label: ThresholdResearchLabel
    candidate_round_count: int
    maximum_matched_quantity: Decimal | None
    maximum_conservative_net_lower_bound: Decimal | None
    maximum_conservative_lower_bound_per_contract: Decimal | None
    minimum_observation_skew: timedelta | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublicThresholdLadderScanReport:
    scan: ThresholdLadderScan
    discovery: PublicMarketScanSummary | None
    events: tuple[ThresholdEventReport, ...]
    relationships: tuple[ThresholdMarketRelationship, ...]
    evaluations: tuple[ThresholdBundleEvaluation, ...]
    ranking: tuple[ThresholdResearchResult, ...]
    enrichment_attempt_count: int
    enrichment_rejection_counts: tuple[tuple[str, int], ...]
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class PreparedThresholdMarket:
    evidence: ThresholdMarketEvidence
    market: ComplementMarket
    snapshot: NormalizedPublicMarketSnapshot
    evaluation: ShadowMarketEvaluation
    raw_responses: tuple[RawComplementResponse, ...]
    event_snapshot: NormalizedPublicEventSnapshot
    event_market_relationships: tuple[EventMarketRelationship, ...]
    fee: SeriesFeeSnapshot | None
    open_interest: Decimal | None
    volume_24h: Decimal | None
    liquidity: Decimal | None
    close_time: datetime | None


@dataclass(frozen=True, slots=True)
class ThresholdRoundEvidence:
    raw_responses: tuple[RawComplementResponse, ...]
    orderbooks: tuple[PublicOrderbookSnapshot, ...]
    levels: tuple[PublicOrderbookLevel, ...]
    evaluations: tuple[ThresholdBundleEvaluation, ...]
    slices: tuple[ThresholdMatchedSlice, ...]


class PublicThresholdLadderClient(Protocol):
    def fetch_orderbook(
        self, *, market_ticker: str, maximum_depth: int, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse: ...


class PublicThresholdEventNormalizer(Protocol):
    """Minimal event-normalization seam; no provider client leaks into domain rules."""

    def normalize_events(
        self,
        *,
        raw_response_id: str,
        page: CapturedPublicEventPage,
    ) -> tuple[NormalizedPublicEventSnapshot, ...]: ...


class PublicThresholdLadderStore(Protocol):
    def report(
        self, request: PublicThresholdLadderScanRequest
    ) -> PublicThresholdLadderScanReport | None: ...

    def start(self, scan: ThresholdLadderScan) -> None: ...

    def append_prepared_market(
        self, scan: ThresholdLadderScan, value: PreparedThresholdMarket
    ) -> None: ...

    def append_relationships(
        self, scan: ThresholdLadderScan, values: tuple[ThresholdMarketRelationship, ...]
    ) -> None: ...

    def append_round(self, scan: ThresholdLadderScan, value: ThresholdRoundEvidence) -> None: ...

    def complete(self, scan: ThresholdLadderScan) -> None: ...

    def terminate(self, scan: ThresholdLadderScan) -> None: ...


def _activity_key(value: CaptureMarketCandidate) -> tuple[object, ...]:
    def descending(decimal: Decimal | None) -> tuple[bool, Decimal]:
        return (decimal is None, Decimal(0) if decimal is None else -decimal)

    close_time = (
        value.close_time.astimezone(UTC) if value.close_time else datetime.max.replace(tzinfo=UTC)
    )
    return (
        value.status != "open",
        value.is_provisional,
        descending(value.open_interest),
        descending(value.volume_24h),
        descending(value.liquidity),
        descending(value.volume),
        close_time,
        value.provider_event_ticker,
        value.provider_market_ticker,
    )


def _descending_decimal(value: Decimal | None) -> tuple[bool, Decimal]:
    return (value is None, Decimal(0) if value is None else -value)


def _maximum_decimal(values: Iterable[Decimal | None]) -> Decimal | None:
    present = tuple(value for value in values if value is not None)
    return max(present) if present else None


def _event_rank(
    values: tuple[PreparedThresholdMarket, ...],
    relationships: tuple[ThresholdMarketRelationship, ...],
) -> tuple[object, ...]:
    event = values[0]
    verified = sum(value.state is ThresholdRelationshipState.VERIFIED for value in relationships)
    return (
        -verified,
        _descending_decimal(_maximum_decimal(item.open_interest for item in values)),
        _descending_decimal(_maximum_decimal(item.volume_24h for item in values)),
        _descending_decimal(_maximum_decimal(item.liquidity for item in values)),
        event.close_time is None,
        event.close_time or datetime.max.replace(tzinfo=UTC),
        event.evidence.provider_event_ticker,
    )


def _research_label(
    values: tuple[ThresholdBundleEvaluation, ...], *, persisting: bool
) -> ThresholdResearchLabel:
    reasons = {reason for value in values for reason in value.reason_codes}
    if persisting:
        return ThresholdResearchLabel.PERSISTING_CANDIDATE
    if any(value.state is ThresholdEvaluationState.CANDIDATE for value in values):
        return ThresholdResearchLabel.CANDIDATE
    if ThresholdEvaluationReasonCode.FEES_ELIMINATE_LOWER_BOUND in reasons:
        return ThresholdResearchLabel.FEE_ELIMINATED
    if ThresholdEvaluationReasonCode.ROUNDING_ELIMINATES_LOWER_BOUND in reasons:
        return ThresholdResearchLabel.ROUNDING_ELIMINATED
    if ThresholdEvaluationReasonCode.GROSS_LOWER_BOUND_NOT_POSITIVE in reasons:
        return ThresholdResearchLabel.GROSS_LOWER_BOUND_NOT_POSITIVE
    if ThresholdEvaluationReasonCode.OBSERVATION_SKEW_EXCEEDED in reasons:
        return ThresholdResearchLabel.SKEW_INELIGIBLE
    if ThresholdEvaluationReasonCode.ORDERBOOK_MISSING_SIDE in reasons:
        return ThresholdResearchLabel.INSUFFICIENT_BOOK
    return ThresholdResearchLabel.INVALID_EVIDENCE


def _ranking(
    *,
    relationships: tuple[ThresholdMarketRelationship, ...],
    evaluations: tuple[ThresholdBundleEvaluation, ...],
) -> tuple[ThresholdResearchResult, ...]:
    relationship_by_id = {value.relationship_id: value for value in relationships}
    grouped: dict[str, list[ThresholdBundleEvaluation]] = defaultdict(list)
    for value in evaluations:
        grouped[value.relationship_id].append(value)
    values: list[ThresholdResearchResult] = []
    for relationship_id, evidence in grouped.items():
        relationship = relationship_by_id.get(relationship_id)
        if relationship is None:
            continue
        ordered = tuple(sorted(evidence, key=lambda value: value.round_ordinal))
        candidate_count = sum(
            value.state is ThresholdEvaluationState.CANDIDATE for value in ordered
        )
        persisting = candidate_count >= 2
        values.append(
            ThresholdResearchResult(
                relationship_id=relationship_id,
                provider_event_ticker=relationship.provider_event_ticker,
                broader_market_ticker=relationship.broader_market_ticker,
                narrower_market_ticker=relationship.narrower_market_ticker,
                label=_research_label(ordered, persisting=persisting),
                candidate_round_count=candidate_count,
                maximum_matched_quantity=max(
                    (value.matched_quantity for value in ordered), default=None
                ),
                maximum_conservative_net_lower_bound=max(
                    (value.conservative_net_lower_bound for value in ordered), default=None
                ),
                maximum_conservative_lower_bound_per_contract=max(
                    (
                        value.conservative_lower_bound_per_contract
                        for value in ordered
                        if value.conservative_lower_bound_per_contract is not None
                    ),
                    default=None,
                ),
                minimum_observation_skew=min(
                    (
                        value.observation_skew
                        for value in ordered
                        if value.observation_skew is not None
                    ),
                    default=None,
                ),
                reason_codes=tuple(
                    sorted({reason.value for value in ordered for reason in value.reason_codes})
                ),
            )
        )
    priority = {
        ThresholdResearchLabel.PERSISTING_CANDIDATE: 0,
        ThresholdResearchLabel.CANDIDATE: 1,
        ThresholdResearchLabel.FEE_ELIMINATED: 2,
        ThresholdResearchLabel.ROUNDING_ELIMINATED: 3,
        ThresholdResearchLabel.GROSS_LOWER_BOUND_NOT_POSITIVE: 4,
        ThresholdResearchLabel.INSUFFICIENT_BOOK: 5,
        ThresholdResearchLabel.SKEW_INELIGIBLE: 6,
        ThresholdResearchLabel.INVALID_EVIDENCE: 7,
    }
    return tuple(
        sorted(
            values,
            key=lambda value: (
                priority[value.label],
                _descending_decimal(value.maximum_conservative_net_lower_bound),
                _descending_decimal(value.maximum_conservative_lower_bound_per_contract),
                _descending_decimal(value.maximum_matched_quantity),
                value.minimum_observation_skew or timedelta.max,
                value.provider_event_ticker,
                value.broader_market_ticker,
                value.narrower_market_ticker,
            ),
        )
    )


class ScanPublicThresholdLadders:
    """One bounded foreground scan of exact structured threshold relationships."""

    def __init__(
        self,
        *,
        discovery: DiscoverPublicPredictionMarkets,
        capture: CaptureBoundedPublicOrderbookSession,
        client: PublicThresholdLadderClient,
        candidate_selector: Callable[[str, int], tuple[CaptureMarketCandidate, ...]],
        store: PublicThresholdLadderStore,
        normalizer: PublicMarketNormalizer,
        event_normalizer: PublicThresholdEventNormalizer,
        wall_clock: Callable[[], datetime],
        monotonic_clock: Callable[[], float],
        sleeper: Callable[[float], None],
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self._discovery = discovery
        self._capture = capture
        self._client = client
        self._candidate_selector = candidate_selector
        self._store = store
        self._normalizer = normalizer
        self._event_normalizer = event_normalizer
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self._progress = progress or (lambda value: None)

    @staticmethod
    def _scan(
        request: PublicThresholdLadderScanRequest, *, started_at: datetime
    ) -> ThresholdLadderScan:
        return ThresholdLadderScan(
            scan_id=request.scan_id,
            evidence_ingestion_run_id=request.evidence_ingestion_run_id,
            request=request,
            started_at=started_at,
            completed_at=None,
            state=ThresholdLadderScanState.RUNNING,
            selected_event_count=0,
            relationship_count=0,
            evaluation_count=0,
            candidate_count=0,
            persisting_candidate_count=0,
            canonical_input_hash=ContentHash(
                content_digest(
                    {
                        "scan_id": request.scan_id,
                        "configuration_hash": request.canonical_configuration_hash,
                        "evidence_ingestion_run_id": request.evidence_ingestion_run_id,
                    }
                )
            ),
            canonical_result_hash=None,
            last_error_code=None,
        )

    @staticmethod
    def _capture_request(
        request: PublicThresholdLadderScanRequest, candidate: CaptureMarketCandidate
    ) -> BoundedOrderbookCaptureRequest:
        return BoundedOrderbookCaptureRequest(
            session_id=deterministic_id(
                "threshold_ladder_enrichment",
                {"scan_id": request.scan_id, "market_ticker": candidate.provider_market_ticker},
            ),
            market_ticker=candidate.provider_market_ticker,
            observations=3,
            interval_seconds=5,
            maximum_depth=request.maximum_depth,
            maximum_bundle_quantity=request.maximum_bundle_quantity,
            timeout_seconds=request.timeout_seconds,
        )

    @staticmethod
    def _raws(
        *,
        scan: ThresholdLadderScan,
        values: tuple[RawComplementResponse, ...],
        offset: int,
    ) -> tuple[RawComplementResponse, ...]:
        return tuple(
            RawComplementResponse(
                raw_response_id=_raw_response_id(
                    value.response,
                    run_id=scan.evidence_ingestion_run_id,
                    ordinal=offset + ordinal,
                ),
                ingestion_run_id=scan.evidence_ingestion_run_id,
                request_ordinal=offset + ordinal,
                response=value.response,
            )
            for ordinal, value in enumerate(values)
        )

    def _prepared(
        self,
        *,
        scan: ThresholdLadderScan,
        refresh: MarketRefresh,
        offset: int,
        fee_by_series: dict[str, SeriesFeeSnapshot],
    ) -> PreparedThresholdMarket:
        raws = self._raws(scan=scan, values=refresh.raw_responses, offset=offset)
        by_endpoint = {value.response.endpoint_identity: value for value in raws}
        market_raw = by_endpoint.get("KALSHI_GET_MARKET")
        event_raw = by_endpoint.get("KALSHI_GET_EVENTS")
        if market_raw is None or event_raw is None:
            raise PublicThresholdLadderError(
                "enrichment evidence lacks exact market or event raw lineage"
            )
        market_document = market_raw.response.document.get("market")
        event_document = event_raw.response.document.get("event")
        if not isinstance(market_document, Mapping) or not isinstance(event_document, Mapping):
            raise PublicThresholdLadderError("enrichment structured response is incompatible")
        page = CapturedPublicMarketPage(
            endpoint_identity="KALSHI_GET_MARKETS",
            request_cursor=None,
            received_at=market_raw.response.received_at,
            http_status=market_raw.response.http_status,
            response_content_type=market_raw.response.response_content_type,
            raw_payload=market_raw.response.raw_payload,
            response_cursor=None,
            document={"markets": [market_document]},
        )
        normalizer = self._normalizer
        snapshots = normalizer.normalize(raw_response_id=market_raw.raw_response_id, page=page)
        if len(snapshots) != 1:
            raise PublicThresholdLadderError("exact market refresh did not normalize one market")
        snapshot = snapshots[0]
        evaluation = evaluate_public_market_snapshot(
            snapshot,
            evaluated_at=market_raw.response.received_at,
            evaluator_version="public-market-structure-v1",
        )
        if (
            snapshot.provider_market_ticker != refresh.market.provider_market_ticker
            or snapshot.event_ticker != refresh.market.provider_event_ticker
            or snapshot.status != "open"
            or snapshot.rule_content_hash is None
            or evaluation.state is not PublicMarketDataQualityState.ELIGIBLE
        ):
            raise PublicThresholdLadderError("exact market refresh is no longer capture eligible")
        event_page = CapturedPublicEventPage(
            endpoint_identity=event_raw.response.endpoint_identity,
            request_cursor=None,
            received_at=event_raw.response.received_at,
            http_status=event_raw.response.http_status,
            response_content_type=event_raw.response.response_content_type,
            raw_payload=event_raw.response.raw_payload,
            response_cursor=None,
            document={"events": [event_document]},
        )
        event_snapshots = self._event_normalizer.normalize_events(
            raw_response_id=event_raw.raw_response_id, page=event_page
        )
        if len(event_snapshots) != 1:
            raise PublicThresholdLadderError("exact event refresh did not normalize one event")
        event_snapshot = event_snapshots[0]
        event_market_relationships = link_event_markets(event_snapshot, (snapshot,))
        series_ticker = refresh.market.series_ticker
        if series_ticker is None:
            raise PublicThresholdLadderError("exact market refresh has no series identity")
        fee = fee_by_series.get(series_ticker)
        if fee is None:
            fee_raw = next(
                (
                    value
                    for value in raws
                    if value.response.endpoint_identity
                    in {"KALSHI_GET_SERIES", "KALSHI_GET_SERIES_FEE_CHANGES"}
                ),
                None,
            )
            if fee_raw is not None:
                fee = _fee_snapshot(
                    response=fee_raw.response,
                    raw_response_id=fee_raw.raw_response_id,
                    run_id=scan.evidence_ingestion_run_id,
                    series_ticker=series_ticker,
                )
                fee_by_series[series_ticker] = fee
        market = ComplementMarket(
            market_snapshot_id=snapshot.snapshot_id,
            ingestion_run_id=scan.evidence_ingestion_run_id,
            provider_market_ticker=refresh.market.provider_market_ticker,
            provider_event_ticker=refresh.market.provider_event_ticker,
            series_ticker=series_ticker,
            status=snapshot.status,
            observed_at=market_raw.response.received_at,
            rule_content_hash=snapshot.rule_content_hash,
            data_quality_state=evaluation.state.value,
        )
        evidence = threshold_market_evidence(
            market_snapshot_id=snapshot.snapshot_id,
            provider_market_ticker=market.provider_market_ticker,
            market_is_active=True,
            rule_content_hash=snapshot.rule_content_hash,
            market_document=market_document,
            event_document=event_document,
        )
        return PreparedThresholdMarket(
            evidence=evidence,
            market=market,
            snapshot=snapshot,
            evaluation=evaluation,
            raw_responses=raws,
            event_snapshot=event_snapshot,
            event_market_relationships=event_market_relationships,
            fee=fee,
            open_interest=snapshot.open_interest,
            volume_24h=snapshot.volume_24h,
            liquidity=snapshot.liquidity,
            close_time=snapshot.close_time,
        )

    @staticmethod
    def _events(
        *,
        prepared: tuple[PreparedThresholdMarket, ...],
        relationships: tuple[ThresholdMarketRelationship, ...],
        event_count: int,
    ) -> tuple[tuple[ThresholdEventReport, tuple[PreparedThresholdMarket, ...]], ...]:
        grouped: dict[str, list[PreparedThresholdMarket]] = defaultdict(list)
        for value in prepared:
            grouped[value.evidence.provider_event_ticker].append(value)
        values: list[tuple[ThresholdEventReport, tuple[PreparedThresholdMarket, ...]]] = []
        for event_ticker, members in grouped.items():
            member_values = tuple(
                sorted(members, key=lambda value: value.market.provider_market_ticker)
            )
            if len(member_values) < 2:
                continue
            event_relationships = tuple(
                value for value in relationships if value.provider_event_ticker == event_ticker
            )
            verified = sum(
                value.state is ThresholdRelationshipState.VERIFIED for value in event_relationships
            )
            if not verified:
                continue
            values.append(
                (
                    ThresholdEventReport(
                        provider_event_ticker=event_ticker,
                        series_ticker=member_values[0].evidence.series_ticker,
                        market_tickers=tuple(
                            value.market.provider_market_ticker for value in member_values
                        ),
                        verified_relationship_count=verified,
                        open_interest=_maximum_decimal(
                            value.open_interest for value in member_values
                        ),
                        volume_24h=_maximum_decimal(value.volume_24h for value in member_values),
                        liquidity=_maximum_decimal(value.liquidity for value in member_values),
                        close_time=min(
                            (
                                value.close_time
                                for value in member_values
                                if value.close_time is not None
                            ),
                            default=None,
                        ),
                    ),
                    member_values,
                )
            )
        return tuple(
            sorted(
                values,
                key=lambda value: _event_rank(
                    value[1],
                    tuple(
                        item
                        for item in relationships
                        if item.provider_event_ticker == value[0].provider_event_ticker
                    ),
                ),
            )[:event_count]
        )

    def scan(self, request: PublicThresholdLadderScanRequest) -> PublicThresholdLadderScanReport:
        existing = self._store.report(request)
        if existing is not None:
            return replace(existing, idempotent_replay=True)
        started_at = self._wall_clock().astimezone(UTC)
        scan = self._scan(request, started_at=started_at)
        self._store.start(scan)
        discovery: PublicMarketScanSummary | None = None
        rejections: Counter[str] = Counter()
        prepared: list[PreparedThresholdMarket] = []
        enrichment_attempt_count = 0
        fee_by_series: dict[str, SeriesFeeSnapshot] = {}
        try:
            self._progress("discovering bounded public Kalshi market evidence")
            discovery = self._discovery.discover(
                PublicMarketScanRequest(
                    run_key=deterministic_id(
                        "threshold_ladder_discovery",
                        {
                            "scan_id": request.scan_id,
                            "configuration": request.canonical_configuration_hash,
                        },
                    ),
                    page_size=max(1, request.market_limit // request.maximum_pages),
                    maximum_pages=request.maximum_pages,
                    timeout_seconds=request.timeout_seconds,
                )
            )
            candidates = tuple(
                sorted(
                    self._candidate_selector(discovery.ingestion_run_id, request.market_limit),
                    key=_activity_key,
                )
            )
            for candidate in candidates:
                if enrichment_attempt_count >= request.maximum_enrichments:
                    break
                enrichment_attempt_count += 1
                self._progress(
                    f"enrichment {enrichment_attempt_count}/{request.maximum_enrichments}: "
                    f"{candidate.provider_market_ticker}"
                )
                try:
                    enrichment = self._capture.enrich_candidates(
                        request=self._capture_request(request, candidate), candidates=(candidate,)
                    )
                except PublicOrderbookCaptureTerminalError as error:
                    raise PublicThresholdLadderError(
                        "terminal public enrichment boundary"
                    ) from error
                except PublicOrderbookCaptureError as error:
                    rejections[type(error).__name__.upper()] += 1
                    continue
                rejections.update(dict(enrichment.rejection_counts))
                if enrichment.selected is None:
                    continue
                value = self._prepared(
                    scan=scan,
                    refresh=enrichment.selected,
                    offset=1_000 + enrichment_attempt_count * 10,
                    fee_by_series=fee_by_series,
                )
                self._store.append_prepared_market(scan, value)
                prepared.append(value)

            bounded_prepared = tuple(
                value
                for event_ticker in sorted(
                    {value.evidence.provider_event_ticker for value in prepared}
                )
                for value in sorted(
                    (
                        item
                        for item in prepared
                        if item.evidence.provider_event_ticker == event_ticker
                    ),
                    key=lambda item: (
                        _descending_decimal(item.open_interest),
                        _descending_decimal(item.volume_24h),
                        _descending_decimal(item.liquidity),
                        item.market.provider_market_ticker,
                    ),
                )[: request.maximum_markets_per_event]
            )
            relationships = tuple(
                relationship
                for event_ticker in sorted(
                    {value.evidence.provider_event_ticker for value in bounded_prepared}
                )
                for relationship in build_threshold_relationships(
                    scan_id=scan.scan_id,
                    markets=tuple(
                        value.evidence
                        for value in bounded_prepared
                        if value.evidence.provider_event_ticker == event_ticker
                    ),
                )
            )
            self._store.append_relationships(scan, relationships)
            selected = self._events(
                prepared=bounded_prepared,
                relationships=relationships,
                event_count=request.event_count,
            )
            selected_events = tuple(value[0] for value in selected)
            selected_markets = tuple(value for _, members in selected for value in members)
            selected_tickers = {value.market.provider_market_ticker for value in selected_markets}
            selected_relationships = tuple(
                sorted(
                    (
                        value
                        for value in relationships
                        if value.state is ThresholdRelationshipState.VERIFIED
                        and value.broader_market_ticker in selected_tickers
                        and value.narrower_market_ticker in selected_tickers
                    ),
                    key=lambda value: (
                        value.provider_event_ticker,
                        value.broader_strike or Decimal(0),
                        value.narrower_strike or Decimal(0),
                        value.broader_market_ticker,
                        value.narrower_market_ticker,
                    ),
                )[: request.maximum_relationships]
            )
            self._progress(
                "selected events="
                + (",".join(value.provider_event_ticker for value in selected_events) or "none")
            )
            self._progress(
                "verified relationships="
                + (
                    ",".join(
                        f"{value.broader_market_ticker}>{value.narrower_market_ticker}"
                        for value in selected_relationships
                    )
                    or "none"
                )
            )
            all_evaluations: list[ThresholdBundleEvaluation] = []
            market_by_ticker = {
                value.market.provider_market_ticker: value for value in selected_markets
            }
            for round_ordinal in range(request.rounds):
                round_started = self._monotonic_clock()
                self._progress(f"round {round_ordinal + 1}/{request.rounds}")
                raw_responses: list[RawComplementResponse] = []
                orderbooks: dict[str, PublicOrderbookSnapshot] = {}
                levels: dict[str, tuple[PublicOrderbookLevel, ...]] = {}
                for market_ordinal, prepared_market in enumerate(selected_markets):
                    ticker = prepared_market.market.provider_market_ticker
                    try:
                        response = self._client.fetch_orderbook(
                            market_ticker=ticker,
                            maximum_depth=request.maximum_depth,
                            timeout_seconds=request.timeout_seconds,
                        )
                        if response.endpoint_identity != "KALSHI_GET_MARKET_ORDERBOOK":
                            raise PublicThresholdLadderError(
                                "public orderbook endpoint identity is invalid"
                            )
                    except Exception as error:
                        if CaptureBoundedPublicOrderbookSession._is_terminal_provider_boundary(
                            error
                        ):
                            raise PublicThresholdLadderError(
                                "terminal public orderbook boundary"
                            ) from error
                        rejections[f"ORDERBOOK_{type(error).__name__.upper()}"] += 1
                        continue
                    raw = RawComplementResponse(
                        raw_response_id=_raw_response_id(
                            response,
                            run_id=scan.evidence_ingestion_run_id,
                            ordinal=100_000 + round_ordinal * 100 + market_ordinal,
                        ),
                        ingestion_run_id=scan.evidence_ingestion_run_id,
                        request_ordinal=100_000 + round_ordinal * 100 + market_ordinal,
                        response=response,
                    )
                    raw_responses.append(raw)
                    orderbook, orderbook_levels = _normalize_orderbook(
                        response=response,
                        raw_response_id=raw.raw_response_id,
                        market=prepared_market.market,
                        maximum_depth=request.maximum_depth,
                    )
                    orderbooks[ticker] = orderbook
                    levels[ticker] = orderbook_levels
                round_evaluations: list[ThresholdBundleEvaluation] = []
                round_slices: list[ThresholdMatchedSlice] = []
                for relationship in selected_relationships:
                    broader = market_by_ticker[relationship.broader_market_ticker]
                    narrower = market_by_ticker[relationship.narrower_market_ticker]
                    evaluation, slices = evaluate_threshold_bundle(
                        scan_id=scan.scan_id,
                        round_ordinal=round_ordinal,
                        relationship=relationship,
                        broader_orderbook=orderbooks.get(relationship.broader_market_ticker),
                        narrower_orderbook=orderbooks.get(relationship.narrower_market_ticker),
                        broader_levels=levels.get(relationship.broader_market_ticker, ()),
                        narrower_levels=levels.get(relationship.narrower_market_ticker, ()),
                        broader_fee=broader.fee,
                        narrower_fee=narrower.fee,
                        maximum_bundle_quantity=parse_decimal(
                            request.maximum_bundle_quantity, allow_zero=False
                        ),
                        maximum_observation_skew=request.maximum_observation_skew,
                        canonical_configuration_hash=request.canonical_configuration_hash,
                        evaluated_at=self._wall_clock().astimezone(UTC),
                    )
                    round_evaluations.append(evaluation)
                    round_slices.extend(slices)
                evidence = ThresholdRoundEvidence(
                    raw_responses=tuple(raw_responses),
                    orderbooks=tuple(orderbooks.values()),
                    levels=tuple(level for values in levels.values() for level in values),
                    evaluations=tuple(round_evaluations),
                    slices=tuple(round_slices),
                )
                self._store.append_round(scan, evidence)
                all_evaluations.extend(round_evaluations)
                if round_ordinal + 1 < request.rounds:
                    remaining = request.interval_seconds - (self._monotonic_clock() - round_started)
                    if remaining > 0:
                        self._sleeper(remaining)
            ranking = _ranking(
                relationships=selected_relationships, evaluations=tuple(all_evaluations)
            )
            candidate_count = sum(
                value.state is ThresholdEvaluationState.CANDIDATE for value in all_evaluations
            )
            persisting_count = sum(
                value.label is ThresholdResearchLabel.PERSISTING_CANDIDATE for value in ranking
            )
            completed_at = self._wall_clock().astimezone(UTC)
            scan = replace(
                scan,
                completed_at=completed_at,
                state=ThresholdLadderScanState.COMPLETED,
                selected_event_count=len(selected_events),
                relationship_count=len(relationships),
                evaluation_count=len(all_evaluations),
                candidate_count=candidate_count,
                persisting_candidate_count=persisting_count,
                canonical_result_hash=ContentHash(
                    content_digest(
                        {
                            "scan_input_hash": scan.canonical_input_hash,
                            "selected_events": selected_events,
                            "relationship_hashes": tuple(
                                value.canonical_hash for value in relationships
                            ),
                            "evaluation_hashes": tuple(
                                value.canonical_hash for value in all_evaluations
                            ),
                        }
                    )
                ),
            )
            self._store.complete(scan)
            completed = self._store.report(request)
            if completed is None:
                raise PublicThresholdLadderError("completed threshold scan is not recoverable")
            return replace(completed, idempotent_replay=False)
        except Exception as error:
            terminated = replace(
                scan,
                completed_at=self._wall_clock().astimezone(UTC),
                state=ThresholdLadderScanState.FAILED,
                last_error_code=type(error).__name__.upper(),
            )
            self._store.terminate(terminated)
            raise


class InMemoryPublicThresholdLadderStore:
    """Deterministic test store containing public evidence only."""

    def __init__(self) -> None:
        self._reports: dict[str, PublicThresholdLadderScanReport] = {}
        self._scans: dict[str, ThresholdLadderScan] = {}
        self._prepared: dict[str, list[PreparedThresholdMarket]] = defaultdict(list)
        self._relationships: dict[str, list[ThresholdMarketRelationship]] = defaultdict(list)
        self._evaluations: dict[str, list[ThresholdBundleEvaluation]] = defaultdict(list)

    def report(
        self, request: PublicThresholdLadderScanRequest
    ) -> PublicThresholdLadderScanReport | None:
        return self._reports.get(request.scan_id)

    def start(self, scan: ThresholdLadderScan) -> None:
        existing = self._scans.get(scan.scan_id)
        if existing is not None and existing.canonical_input_hash != scan.canonical_input_hash:
            raise PublicThresholdLadderConflictError("threshold scan identity conflicts")
        self._scans.setdefault(scan.scan_id, scan)

    def append_prepared_market(
        self, scan: ThresholdLadderScan, value: PreparedThresholdMarket
    ) -> None:
        if any(
            item.evidence.market_snapshot_id == value.evidence.market_snapshot_id
            and item.evidence.canonical_hash != value.evidence.canonical_hash
            for item in self._prepared[scan.scan_id]
        ):
            raise PublicThresholdLadderConflictError("threshold market evidence conflicts")
        self._prepared[scan.scan_id].append(value)

    def append_relationships(
        self, scan: ThresholdLadderScan, values: tuple[ThresholdMarketRelationship, ...]
    ) -> None:
        known = {value.relationship_id: value for value in self._relationships[scan.scan_id]}
        for value in values:
            existing = known.get(value.relationship_id)
            if existing is not None and existing.canonical_hash != value.canonical_hash:
                raise PublicThresholdLadderConflictError(
                    "threshold relationship identity conflicts"
                )
            known[value.relationship_id] = value
        self._relationships[scan.scan_id] = list(known.values())

    def append_round(self, scan: ThresholdLadderScan, value: ThresholdRoundEvidence) -> None:
        known = {item.evaluation_id: item for item in self._evaluations[scan.scan_id]}
        for item in value.evaluations:
            existing = known.get(item.evaluation_id)
            if existing is not None and existing.canonical_hash != item.canonical_hash:
                raise PublicThresholdLadderConflictError("threshold evaluation identity conflicts")
            known[item.evaluation_id] = item
        self._evaluations[scan.scan_id] = list(known.values())

    def complete(self, scan: ThresholdLadderScan) -> None:
        self._scans[scan.scan_id] = scan
        prepared = tuple(self._prepared[scan.scan_id])
        relationships = tuple(self._relationships[scan.scan_id])
        grouped: dict[str, list[PreparedThresholdMarket]] = defaultdict(list)
        for value in prepared:
            grouped[value.evidence.provider_event_ticker].append(value)
        event_reports = tuple(
            ThresholdEventReport(
                provider_event_ticker=event_ticker,
                series_ticker=values[0].evidence.series_ticker,
                market_tickers=tuple(
                    sorted(value.market.provider_market_ticker for value in values)
                ),
                verified_relationship_count=sum(
                    relationship.provider_event_ticker == event_ticker
                    and relationship.state is ThresholdRelationshipState.VERIFIED
                    for relationship in relationships
                ),
                open_interest=_maximum_decimal(value.open_interest for value in values),
                volume_24h=_maximum_decimal(value.volume_24h for value in values),
                liquidity=_maximum_decimal(value.liquidity for value in values),
                close_time=min(
                    (value.close_time for value in values if value.close_time is not None),
                    default=None,
                ),
            )
            for event_ticker, values in sorted(grouped.items())
            if any(
                relationship.provider_event_ticker == event_ticker
                and relationship.state is ThresholdRelationshipState.VERIFIED
                for relationship in relationships
            )
        )
        self._reports[scan.scan_id] = PublicThresholdLadderScanReport(
            scan=scan,
            discovery=None,
            events=event_reports,
            relationships=relationships,
            evaluations=tuple(self._evaluations[scan.scan_id]),
            ranking=_ranking(
                relationships=relationships, evaluations=tuple(self._evaluations[scan.scan_id])
            ),
            enrichment_attempt_count=0,
            enrichment_rejection_counts=(),
            idempotent_replay=False,
        )

    def terminate(self, scan: ThresholdLadderScan) -> None:
        self._scans[scan.scan_id] = scan
