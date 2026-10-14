"""Bounded, foreground aggregation of existing public Kalshi evidence flows.

The sweep chooses a small exact-market cohort, then delegates every network
observation and persistence operation to the established discovery and capture
services.  It has no financial, authorization, or execution authority.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from apex.application.capture_public_orderbook_session import (
    BoundedOrderbookCaptureRequest,
    CaptureBoundedPublicOrderbookSession,
    CaptureMarketCandidate,
    MarketRefresh,
    OrderbookCaptureReport,
    OrderbookCaptureSessionState,
    PublicOrderbookCaptureConflictError,
    PublicOrderbookCaptureError,
    PublicOrderbookCaptureTerminalError,
)
from apex.application.discover_public_markets import (
    DiscoverPublicPredictionMarkets,
    PublicMarketScanRequest,
    PublicMarketScanSummary,
)
from apex.domain.public_complement import (
    ComplementReasonCode,
    ComplementState,
)
from apex.domain.public_orderbook_history import OrderbookHistoryEvidence
from apex.domain.values import content_digest, deterministic_id, parse_decimal

SWEEP_VERSION = "public-kalshi-orderbook-sweep-v1"


class PublicKalshiOrderbookSweepState(StrEnum):
    COMPLETED = "COMPLETED"
    COMPLETED_PARTIALLY = "COMPLETED_PARTIALLY"
    FAILED_BEFORE_ANY_MARKET = "FAILED_BEFORE_ANY_MARKET"


class PublicOrderbookShape(StrEnum):
    EMPTY = "EMPTY"
    YES_ONLY = "YES_ONLY"
    NO_ONLY = "NO_ONLY"
    TWO_SIDED = "TWO_SIDED"
    MIXED_ACROSS_OBSERVATIONS = "MIXED_ACROSS_OBSERVATIONS"


class ShadowComplementResearchLabel(StrEnum):
    CANDIDATE = "CANDIDATE"
    FEE_ELIMINATED = "FEE_ELIMINATED"
    ROUNDING_ELIMINATED = "ROUNDING_ELIMINATED"
    GROSS_GAP_NOT_POSITIVE = "GROSS_GAP_NOT_POSITIVE"
    INSUFFICIENT_BOOK = "INSUFFICIENT_BOOK"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"


@dataclass(frozen=True, slots=True)
class PublicKalshiOrderbookSweepRequest:
    """All user-configurable foreground limits, deliberately bounded before I/O."""

    sweep_id: str
    market_count: int = 12
    maximum_pages: int = 3
    market_limit: int = 300
    maximum_enrichments: int = 40
    maximum_markets_per_event: int = 2
    maximum_markets_per_series: int = 3
    observations: int = 3
    interval_seconds: int = 5
    maximum_depth: int = 5
    maximum_bundle_quantity: str | int | Decimal = "10"
    timeout_seconds: int = 10

    def __post_init__(self) -> None:
        if not self.sweep_id or len(self.sweep_id) > 160:
            raise ValueError("public orderbook sweep identity is required")
        if isinstance(self.market_count, bool) or not 3 <= self.market_count <= 20:
            raise ValueError("sweep market count must be between three and twenty")
        if isinstance(self.maximum_pages, bool) or not 1 <= self.maximum_pages <= 5:
            raise ValueError("sweep pages must be between one and five")
        if isinstance(self.market_limit, bool) or not 50 <= self.market_limit <= 500:
            raise ValueError("sweep market limit must be between fifty and five hundred")
        if (
            isinstance(self.maximum_enrichments, bool)
            or not self.market_count <= self.maximum_enrichments <= 60
        ):
            raise ValueError("sweep enrichments must be between selected count and sixty")
        if self.maximum_markets_per_event != 2 or self.maximum_markets_per_series != 3:
            raise ValueError("sweep diversity caps are fixed at two per event and three per series")
        if isinstance(self.observations, bool) or not 3 <= self.observations <= 5:
            raise ValueError("sweep observations must be between three and five")
        if isinstance(self.interval_seconds, bool) or not 5 <= self.interval_seconds <= 60:
            raise ValueError("sweep interval must be between five and sixty seconds")
        if isinstance(self.maximum_depth, bool) or not 1 <= self.maximum_depth <= 20:
            raise ValueError("sweep depth must be between one and twenty")
        if isinstance(self.timeout_seconds, bool) or not 1 <= self.timeout_seconds <= 30:
            raise ValueError("sweep timeout must be between one and thirty seconds")
        quantity = parse_decimal(self.maximum_bundle_quantity, allow_zero=False)
        if quantity > 100:
            raise ValueError("sweep maximum bundle quantity must not exceed one hundred")
        object.__setattr__(self, "maximum_bundle_quantity", quantity)

    @property
    def configuration_hash(self) -> str:
        return content_digest(
            {
                "sweep_version": SWEEP_VERSION,
                "market_count": self.market_count,
                "maximum_pages": self.maximum_pages,
                "market_limit": self.market_limit,
                "maximum_enrichments": self.maximum_enrichments,
                "maximum_markets_per_event": self.maximum_markets_per_event,
                "maximum_markets_per_series": self.maximum_markets_per_series,
                "observations": self.observations,
                "interval_seconds": self.interval_seconds,
                "maximum_depth": self.maximum_depth,
                "maximum_bundle_quantity": self.maximum_bundle_quantity,
                "timeout_seconds": self.timeout_seconds,
            }
        )


@dataclass(frozen=True, slots=True)
class PublicKalshiSweepMarketReport:
    provider_market_ticker: str
    provider_event_ticker: str
    series_ticker: str
    is_provisional: bool
    close_time: datetime | None
    open_interest: Decimal | None
    volume_24h: Decimal | None
    liquidity: Decimal | None
    volume: Decimal | None
    session_state: OrderbookCaptureSessionState
    attempted_observation_count: int
    successful_observation_count: int
    failed_observation_count: int
    orderbook_shape: PublicOrderbookShape | None
    evaluation_state_counts: tuple[tuple[str, int], ...]
    gross_positive_gap_observation_count: int
    fees_eliminated_gap_count: int
    rounding_eliminated_gap_count: int
    candidate_observation_count: int
    longest_compatible_candidate_streak: int | None
    maximum_matched_quantity: Decimal | None
    maximum_gross_complement_gap: Decimal | None
    maximum_conservative_net_lower_bound: Decimal | None
    maximum_conservative_net_lower_bound_per_contract: Decimal | None
    reason_codes: tuple[str, ...]

    @property
    def has_persisting_candidate(self) -> bool:
        return self.longest_compatible_candidate_streak is not None and (
            self.longest_compatible_candidate_streak >= 2
        )

    @property
    def has_candidate(self) -> bool:
        return self.candidate_observation_count > 0


@dataclass(frozen=True, slots=True)
class ShadowComplementResearchResult:
    provider_market_ticker: str
    label: ShadowComplementResearchLabel
    has_persisting_candidate: bool
    has_candidate: bool
    conservative_net_lower_bound: Decimal | None
    gross_complement_gap: Decimal | None
    matched_quantity: Decimal | None
    open_interest: Decimal | None
    volume_24h: Decimal | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublicKalshiOrderbookSweepReport:
    sweep_id: str
    state: PublicKalshiOrderbookSweepState
    discovery: PublicMarketScanSummary | None
    requested_market_count: int
    selected_market_count: int
    enrichment_attempt_count: int
    enrichment_rejection_counts: tuple[tuple[str, int], ...]
    markets: tuple[PublicKalshiSweepMarketReport, ...]
    research_ranking: tuple[ShadowComplementResearchResult, ...]
    fatal_error_code: str | None

    @property
    def selected_cohort_smaller_than_requested(self) -> bool:
        return self.selected_market_count < self.requested_market_count

    @property
    def failed_session_count(self) -> int:
        return sum(
            market.session_state is not OrderbookCaptureSessionState.COMPLETED
            for market in self.markets
        )


@dataclass(frozen=True, slots=True)
class _SelectedMarket:
    candidate: CaptureMarketCandidate
    refresh: MarketRefresh


def _descending_decimal(value: Decimal | None) -> tuple[bool, Decimal]:
    """Sort present Decimal values before missing values without making absence zero."""

    return (value is None, Decimal(0) if value is None else -value)


def _candidate_key(value: CaptureMarketCandidate) -> tuple[object, ...]:
    close_time = value.close_time.astimezone(UTC) if value.close_time is not None else None
    return (
        value.status != "open",
        value.is_provisional,
        _descending_decimal(value.open_interest),
        _descending_decimal(value.volume_24h),
        _descending_decimal(value.liquidity),
        _descending_decimal(value.volume),
        close_time is None,
        close_time or datetime.max.replace(tzinfo=UTC),
        value.provider_market_ticker,
        value.market_snapshot_id,
    )


def _shape(evidence: tuple[OrderbookHistoryEvidence, ...]) -> PublicOrderbookShape | None:
    if not evidence:
        return None
    shapes: set[PublicOrderbookShape] = set()
    for item in evidence:
        yes = item.orderbook.yes_bid_level_count > 0
        no = item.orderbook.no_bid_level_count > 0
        shapes.add(
            PublicOrderbookShape.TWO_SIDED
            if yes and no
            else PublicOrderbookShape.YES_ONLY
            if yes
            else PublicOrderbookShape.NO_ONLY
            if no
            else PublicOrderbookShape.EMPTY
        )
    if len(shapes) == 1:
        return next(iter(shapes))
    return PublicOrderbookShape.MIXED_ACROSS_OBSERVATIONS


def _maximum(values: tuple[Decimal | None, ...]) -> Decimal | None:
    return max((value for value in values if value is not None), default=None)


def _market_report(
    selected: _SelectedMarket,
    report: OrderbookCaptureReport,
    evidence: tuple[OrderbookHistoryEvidence, ...],
) -> PublicKalshiSweepMarketReport:
    evaluations = tuple(item.evaluation for item in evidence)
    state_counts = Counter(value.state.value for value in evaluations)
    reasons = {reason.value for evaluation in evaluations for reason in evaluation.reason_codes}
    if report.session.last_error_code is not None:
        reasons.add(f"SESSION_{report.session.last_error_code}")
    candidate = selected.candidate
    event_ticker = selected.refresh.market.provider_event_ticker
    series_ticker = selected.refresh.market.series_ticker
    if event_ticker is None or series_ticker is None:
        raise PublicOrderbookCaptureTerminalError("retained capture is missing exact lineage")
    analysis = report.history.analysis if report.history is not None else None
    return PublicKalshiSweepMarketReport(
        provider_market_ticker=selected.refresh.market.provider_market_ticker,
        provider_event_ticker=event_ticker,
        series_ticker=series_ticker,
        is_provisional=candidate.is_provisional,
        close_time=candidate.close_time,
        open_interest=candidate.open_interest,
        volume_24h=candidate.volume_24h,
        liquidity=candidate.liquidity,
        volume=candidate.volume,
        session_state=report.session.state,
        attempted_observation_count=report.session.attempted_observation_count,
        successful_observation_count=report.session.successful_observation_count,
        failed_observation_count=report.session.failed_observation_count,
        orderbook_shape=_shape(evidence),
        evaluation_state_counts=tuple(sorted(state_counts.items())),
        gross_positive_gap_observation_count=sum(
            evaluation.gross_complement_gap > 0 for evaluation in evaluations
        ),
        fees_eliminated_gap_count=sum(
            ComplementReasonCode.FEES_ELIMINATE_GROSS_GAP in evaluation.reason_codes
            for evaluation in evaluations
        ),
        rounding_eliminated_gap_count=sum(
            ComplementReasonCode.ROUNDING_RESERVE_ELIMINATES_GAP in evaluation.reason_codes
            for evaluation in evaluations
        ),
        candidate_observation_count=sum(
            evaluation.state is ComplementState.CANDIDATE for evaluation in evaluations
        ),
        longest_compatible_candidate_streak=(
            analysis.longest_candidate_streak_observation_count if analysis is not None else None
        ),
        maximum_matched_quantity=_maximum(
            tuple(evaluation.matched_quantity for evaluation in evaluations)
        ),
        maximum_gross_complement_gap=_maximum(
            tuple(evaluation.gross_complement_gap for evaluation in evaluations)
        ),
        maximum_conservative_net_lower_bound=_maximum(
            tuple(evaluation.conservative_net_lower_bound for evaluation in evaluations)
        ),
        maximum_conservative_net_lower_bound_per_contract=_maximum(
            tuple(evaluation.net_lower_bound_per_contract for evaluation in evaluations)
        ),
        reason_codes=tuple(sorted(reasons)),
    )


def _label(value: PublicKalshiSweepMarketReport) -> ShadowComplementResearchLabel:
    reasons = set(value.reason_codes)
    if value.has_candidate:
        return ShadowComplementResearchLabel.CANDIDATE
    if ComplementReasonCode.FEES_ELIMINATE_GROSS_GAP.value in reasons:
        return ShadowComplementResearchLabel.FEE_ELIMINATED
    if ComplementReasonCode.ROUNDING_RESERVE_ELIMINATES_GAP.value in reasons:
        return ShadowComplementResearchLabel.ROUNDING_ELIMINATED
    if ComplementReasonCode.GROSS_GAP_NOT_POSITIVE.value in reasons:
        return ShadowComplementResearchLabel.GROSS_GAP_NOT_POSITIVE
    if ComplementReasonCode.ORDERBOOK_MISSING_SIDE.value in reasons:
        return ShadowComplementResearchLabel.INSUFFICIENT_BOOK
    return ShadowComplementResearchLabel.INVALID_EVIDENCE


def _ranking(
    markets: tuple[PublicKalshiSweepMarketReport, ...],
) -> tuple[ShadowComplementResearchResult, ...]:
    values = tuple(
        ShadowComplementResearchResult(
            provider_market_ticker=market.provider_market_ticker,
            label=_label(market),
            has_persisting_candidate=market.has_persisting_candidate,
            has_candidate=market.has_candidate,
            conservative_net_lower_bound=market.maximum_conservative_net_lower_bound,
            gross_complement_gap=market.maximum_gross_complement_gap,
            matched_quantity=market.maximum_matched_quantity,
            open_interest=market.open_interest,
            volume_24h=market.volume_24h,
            reason_codes=market.reason_codes,
        )
        for market in markets
    )
    return tuple(
        sorted(
            values,
            key=lambda value: (
                not value.has_persisting_candidate,
                not value.has_candidate,
                _descending_decimal(value.conservative_net_lower_bound),
                _descending_decimal(value.gross_complement_gap),
                _descending_decimal(value.matched_quantity),
                _descending_decimal(value.open_interest),
                _descending_decimal(value.volume_24h),
                value.provider_market_ticker,
            ),
        )
    )


class SweepPublicKalshiOrderbooks:
    """One sequential cohort sweep layered only over current public evidence services."""

    def __init__(
        self,
        *,
        discovery: DiscoverPublicPredictionMarkets,
        capture: CaptureBoundedPublicOrderbookSession,
        candidate_selector: Callable[[str, int], tuple[CaptureMarketCandidate, ...]],
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self._discovery = discovery
        self._capture = capture
        self._candidate_selector = candidate_selector
        self._progress = progress or (lambda value: None)

    @staticmethod
    def _capture_request(
        request: PublicKalshiOrderbookSweepRequest,
        candidate: CaptureMarketCandidate,
    ) -> BoundedOrderbookCaptureRequest:
        return BoundedOrderbookCaptureRequest(
            session_id=deterministic_id(
                "public_orderbook_sweep_session",
                {
                    "sweep_id": request.sweep_id,
                    "configuration_hash": request.configuration_hash,
                    "market_ticker": candidate.provider_market_ticker,
                },
            ),
            market_ticker=candidate.provider_market_ticker,
            observations=request.observations,
            interval_seconds=request.interval_seconds,
            maximum_depth=request.maximum_depth,
            maximum_bundle_quantity=request.maximum_bundle_quantity,
            timeout_seconds=request.timeout_seconds,
        )

    @staticmethod
    def _state(
        *,
        selected: tuple[_SelectedMarket, ...],
        reports: tuple[PublicKalshiSweepMarketReport, ...],
        request: PublicKalshiOrderbookSweepRequest,
        fatal_error_code: str | None,
    ) -> PublicKalshiOrderbookSweepState:
        if fatal_error_code is not None and not any(
            report.session_state is OrderbookCaptureSessionState.COMPLETED for report in reports
        ):
            return PublicKalshiOrderbookSweepState.FAILED_BEFORE_ANY_MARKET
        if (
            fatal_error_code is not None
            or len(selected) < request.market_count
            or any(
                report.session_state is not OrderbookCaptureSessionState.COMPLETED
                for report in reports
            )
        ):
            return PublicKalshiOrderbookSweepState.COMPLETED_PARTIALLY
        return PublicKalshiOrderbookSweepState.COMPLETED

    def _report(
        self,
        *,
        request: PublicKalshiOrderbookSweepRequest,
        discovery: PublicMarketScanSummary | None,
        selected: tuple[_SelectedMarket, ...],
        reports: tuple[PublicKalshiSweepMarketReport, ...],
        enrichment_attempt_count: int,
        rejections: Counter[str],
        fatal_error_code: str | None,
    ) -> PublicKalshiOrderbookSweepReport:
        return PublicKalshiOrderbookSweepReport(
            sweep_id=request.sweep_id,
            state=self._state(
                selected=selected,
                reports=reports,
                request=request,
                fatal_error_code=fatal_error_code,
            ),
            discovery=discovery,
            requested_market_count=request.market_count,
            selected_market_count=len(selected),
            enrichment_attempt_count=enrichment_attempt_count,
            enrichment_rejection_counts=tuple(sorted(rejections.items())),
            markets=reports,
            research_ranking=_ranking(reports),
            fatal_error_code=fatal_error_code,
        )

    def sweep(self, request: PublicKalshiOrderbookSweepRequest) -> PublicKalshiOrderbookSweepReport:
        self._progress("discovering bounded public Kalshi market evidence")
        page_size = max(1, request.market_limit // request.maximum_pages)
        discovery_request = PublicMarketScanRequest(
            run_key=deterministic_id(
                "public_orderbook_sweep_discovery",
                {"sweep_id": request.sweep_id, "configuration_hash": request.configuration_hash},
            ),
            page_size=page_size,
            maximum_pages=request.maximum_pages,
            timeout_seconds=request.timeout_seconds,
        )
        try:
            discovery = self._discovery.discover(discovery_request)
            candidates = self._candidate_selector(discovery.ingestion_run_id, request.market_limit)
        except Exception as error:
            return self._report(
                request=request,
                discovery=None,
                selected=(),
                reports=(),
                enrichment_attempt_count=0,
                rejections=Counter(),
                fatal_error_code=f"DISCOVERY_{type(error).__name__.upper()}",
            )

        ranked = tuple(sorted(candidates, key=_candidate_key))
        self._progress(f"ranked candidates={len(ranked)}")
        selected_values: list[_SelectedMarket] = []
        rejections: Counter[str] = Counter()
        enriched_tickers: set[str] = set()
        event_counts: Counter[str] = Counter()
        series_counts: Counter[str] = Counter()
        enrichment_attempt_count = 0
        fatal_error_code: str | None = None

        for candidate in ranked:
            if enrichment_attempt_count >= request.maximum_enrichments:
                break
            if len(selected_values) >= request.market_count:
                break
            if candidate.provider_market_ticker in enriched_tickers:
                rejections["DUPLICATE_MARKET_TICKER"] += 1
                continue
            enriched_tickers.add(candidate.provider_market_ticker)
            enrichment_attempt_count += 1
            self._progress(
                f"enrichment {enrichment_attempt_count}/{request.maximum_enrichments}: "
                f"{candidate.provider_market_ticker}"
            )
            capture_request = self._capture_request(request, candidate)
            try:
                enrichment = self._capture.enrich_candidates(
                    request=capture_request, candidates=(candidate,)
                )
            except PublicOrderbookCaptureTerminalError:
                fatal_error_code = "TERMINAL_ENRICHMENT_BOUNDARY"
                break
            except PublicOrderbookCaptureError:
                fatal_error_code = "ENRICHMENT_SECURITY_OR_INTEGRITY_FAILURE"
                break
            rejections.update(dict(enrichment.rejection_counts))
            refresh = enrichment.selected
            if refresh is None:
                continue
            event_ticker = refresh.market.provider_event_ticker
            series_ticker = refresh.market.series_ticker
            if event_ticker is None or series_ticker is None:
                fatal_error_code = "ENRICHMENT_LINEAGE_MISSING"
                break
            if event_counts[event_ticker] >= request.maximum_markets_per_event:
                rejections["EVENT_DIVERSITY_CAP"] += 1
                continue
            if series_counts[series_ticker] >= request.maximum_markets_per_series:
                rejections["SERIES_DIVERSITY_CAP"] += 1
                continue
            event_counts[event_ticker] += 1
            series_counts[series_ticker] += 1
            selected_values.append(_SelectedMarket(candidate=candidate, refresh=refresh))

        selected = tuple(selected_values)
        cohort = ",".join(value.refresh.market.provider_market_ticker for value in selected) or "-"
        self._progress(f"selected cohort={cohort}")
        market_reports: list[PublicKalshiSweepMarketReport] = []
        if fatal_error_code is None:
            for ordinal, value in enumerate(selected, start=1):
                ticker = value.refresh.market.provider_market_ticker
                self._progress(f"market {ordinal}/{len(selected)}: {ticker}")
                capture_request = self._capture_request(request, value.candidate)
                try:
                    capture_report = self._capture.capture_prepared(capture_request, value.refresh)
                except PublicOrderbookCaptureTerminalError:
                    retained = self._capture.capture_report(capture_request.session_id)
                    if retained is not None:
                        evidence = self._capture.session_evidence(retained.session.session_id)
                        market_reports.append(_market_report(value, retained, evidence))
                    fatal_error_code = "TERMINAL_CAPTURE_BOUNDARY"
                    break
                except PublicOrderbookCaptureConflictError:
                    fatal_error_code = "CAPTURE_INTEGRITY_FAILURE"
                    break
                except PublicOrderbookCaptureError:
                    fatal_error_code = "CAPTURE_SECURITY_FAILURE"
                    break
                except Exception as error:
                    fatal_error_code = f"CAPTURE_{type(error).__name__.upper()}"
                    break
                try:
                    evidence = self._capture.session_evidence(capture_report.session.session_id)
                except Exception as error:
                    fatal_error_code = f"CAPTURE_EVIDENCE_{type(error).__name__.upper()}"
                    break
                market_reports.append(_market_report(value, capture_report, evidence))
                self._progress(
                    f"session {ticker}: {capture_report.session.state.value} "
                    f"{capture_report.session.successful_observation_count}/"
                    f"{capture_report.session.requested_observation_count}"
                )

        return self._report(
            request=request,
            discovery=discovery,
            selected=selected,
            reports=tuple(market_reports),
            enrichment_attempt_count=enrichment_attempt_count,
            rejections=rejections,
            fatal_error_code=fatal_error_code,
        )
