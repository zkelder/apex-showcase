"""Bounded, no-network history analysis of retained same-market orderbooks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Protocol

from apex.domain.public_complement import BinaryComplementEvaluation, ComplementState
from apex.domain.public_market import PublicMarketProvider
from apex.domain.public_orderbook_history import (
    OrderbookHistoryAnalysis,
    OrderbookHistoryAnalysisState,
    OrderbookHistoryContinuityState,
    OrderbookHistoryEvidence,
    OrderbookHistoryObservation,
    OrderbookHistoryObservationState,
)
from apex.domain.values import ContentHash, content_digest, deterministic_id

HISTORY_EVALUATOR_VERSION = "public-orderbook-observation-history-v1"


class OrderbookObservationHistoryError(RuntimeError):
    """Retained orderbook history cannot be analyzed safely."""


class OrderbookObservationHistoryConflictError(OrderbookObservationHistoryError):
    """Immutable orderbook history identity has incompatible content."""


@dataclass(frozen=True, slots=True)
class OrderbookHistoryRequest:
    provider_market_ticker: str
    ingestion_run_id: str | None = None
    first_orderbook_snapshot_id: str | None = None
    last_orderbook_snapshot_id: str | None = None
    maximum_observations: int = 20
    continuity_gap: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if not self.provider_market_ticker or len(self.provider_market_ticker) > 300:
            raise ValueError("exact market ticker is required")
        if not 3 <= self.maximum_observations <= 50:
            raise ValueError("maximum observations must be between three and fifty")
        if self.ingestion_run_id is not None and not self.ingestion_run_id:
            raise ValueError("orderbook history ingestion run identity must be non-empty when set")
        if not timedelta(seconds=1) <= self.continuity_gap <= timedelta(minutes=30):
            raise ValueError("continuity gap must be between one second and thirty minutes")


@dataclass(frozen=True, slots=True)
class OrderbookHistoryReport:
    analysis: OrderbookHistoryAnalysis
    observations: tuple[OrderbookHistoryObservation, ...]
    idempotent_replay: bool


class OrderbookObservationHistoryStore(Protocol):
    def select_observations(
        self,
        *,
        provider: PublicMarketProvider,
        provider_market_ticker: str,
        ingestion_run_id: str | None,
        first_orderbook_snapshot_id: str | None,
        last_orderbook_snapshot_id: str | None,
        maximum_observations: int,
    ) -> tuple[OrderbookHistoryEvidence, ...]: ...

    def get_history(self, analysis_id: str) -> OrderbookHistoryReport | None: ...

    def append_history(
        self,
        analysis: OrderbookHistoryAnalysis,
        observations: tuple[OrderbookHistoryObservation, ...],
    ) -> None: ...


def _state(value: ComplementState) -> OrderbookHistoryObservationState:
    return {
        ComplementState.CANDIDATE: OrderbookHistoryObservationState.PRESENT_CANDIDATE,
        ComplementState.NO_CANDIDATE: OrderbookHistoryObservationState.PRESENT_NO_CANDIDATE,
        ComplementState.INELIGIBLE: OrderbookHistoryObservationState.PRESENT_INELIGIBLE,
        ComplementState.INVALID: OrderbookHistoryObservationState.PRESENT_INVALID,
    }[value]


def _candidate(value: OrderbookHistoryEvidence) -> bool:
    evaluation = value.evaluation
    return (
        evaluation.state is ComplementState.CANDIDATE
        and evaluation.matched_quantity > 0
        and evaluation.conservative_net_lower_bound > 0
    )


def _compatibility_reasons(
    previous: OrderbookHistoryEvidence, current: OrderbookHistoryEvidence
) -> tuple[str, ...]:
    values: list[str] = []
    if previous.rule_evidence_hash != current.rule_evidence_hash:
        values.append("RULE_EVIDENCE_CHANGED")
    if previous.orderbook.series_ticker != current.orderbook.series_ticker:
        values.append("SERIES_IDENTITY_CHANGED")
    if previous.market_lineage_hash != current.market_lineage_hash:
        values.append("MARKET_LINEAGE_CHANGED")
    previous_fee, current_fee = previous.fee, current.fee
    if (previous_fee is None) != (current_fee is None):
        values.append("FEE_EVIDENCE_CHANGED")
    elif previous_fee is not None and current_fee is not None:
        if previous_fee.schedule_identity != current_fee.schedule_identity:
            values.append("FEE_SCHEDULE_CHANGED")
        if previous_fee.fee_multiplier != current_fee.fee_multiplier:
            values.append("FEE_MULTIPLIER_CHANGED")
        if previous_fee.fee_model_identity != current_fee.fee_model_identity:
            values.append("FEE_MODEL_CHANGED")
        if previous_fee.rounding_identity != current_fee.rounding_identity:
            values.append("ROUNDING_MODEL_CHANGED")
    if previous.evaluation.evaluator_version != current.evaluation.evaluator_version:
        values.append("EVALUATOR_VERSION_CHANGED")
    if current.observed_at <= previous.observed_at:
        values.append("OBSERVATION_ORDER_INVALID")
    return tuple(values)


def _delta(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    return None if current is None or previous is None else current - previous


def _metric(evaluation: BinaryComplementEvaluation, field: str) -> Decimal | None:
    """Keep unreliable evaluation values absent in the history projection."""

    if evaluation.state in {ComplementState.INELIGIBLE, ComplementState.INVALID}:
        return None
    values: dict[str, Decimal | None] = {
        "matched_quantity": evaluation.matched_quantity,
        "weighted_yes_ask": evaluation.weighted_yes_ask,
        "weighted_no_ask": evaluation.weighted_no_ask,
        "gross_acquisition_cost": evaluation.gross_acquisition_cost,
        "gross_settlement_value": evaluation.gross_settlement_value,
        "gross_complement_gap": evaluation.gross_complement_gap,
        "yes_modeled_taker_fee": evaluation.yes_modeled_taker_fee,
        "no_modeled_taker_fee": evaluation.no_modeled_taker_fee,
        "modeled_taker_fees": evaluation.modeled_taker_fees,
        "conservative_rounding_reserve": evaluation.conservative_rounding_reserve,
        "total_modeled_acquisition_cost": evaluation.total_modeled_acquisition_cost,
        "conservative_net_lower_bound": evaluation.conservative_net_lower_bound,
        "net_lower_bound_per_contract": evaluation.net_lower_bound_per_contract,
    }
    return values[field]


class BuildOrderbookObservationHistory:
    """Build one immutable exact-market timeline from existing public evidence only."""

    def __init__(self, *, store: OrderbookObservationHistoryStore) -> None:
        self._store = store

    @staticmethod
    def _validate(
        values: tuple[OrderbookHistoryEvidence, ...], request: OrderbookHistoryRequest
    ) -> None:
        if len(values) < 3:
            raise OrderbookObservationHistoryError(
                "at least three retained orderbooks are required"
            )
        if len(values) > request.maximum_observations:
            raise OrderbookObservationHistoryError("orderbook history selection exceeds its bound")
        previous: OrderbookHistoryEvidence | None = None
        identities: set[str] = set()
        for value in values:
            if value.orderbook.provider is not PublicMarketProvider.KALSHI:
                raise OrderbookObservationHistoryError("orderbook history requires Kalshi evidence")
            if value.orderbook.provider_market_ticker != request.provider_market_ticker:
                raise OrderbookObservationHistoryError(
                    "orderbook history exact market ticker conflicts"
                )
            if value.orderbook.orderbook_snapshot_id in identities:
                raise OrderbookObservationHistoryError(
                    "orderbook history snapshot identity is duplicated"
                )
            identities.add(value.orderbook.orderbook_snapshot_id)
            if previous is not None and value.observed_at <= previous.observed_at:
                raise OrderbookObservationHistoryError(
                    "orderbook history observation ordering is invalid"
                )
            previous = value
        if (
            request.first_orderbook_snapshot_id
            and values[0].orderbook.orderbook_snapshot_id != request.first_orderbook_snapshot_id
        ):
            raise OrderbookObservationHistoryError(
                "orderbook history first snapshot bound is invalid"
            )
        if (
            request.last_orderbook_snapshot_id
            and values[-1].orderbook.orderbook_snapshot_id != request.last_orderbook_snapshot_id
        ):
            raise OrderbookObservationHistoryError(
                "orderbook history last snapshot bound is invalid"
            )

    @staticmethod
    def _configuration(request: OrderbookHistoryRequest) -> dict[str, object]:
        return {
            "provider": PublicMarketProvider.KALSHI,
            "provider_market_ticker": request.provider_market_ticker,
            "ingestion_run_id": request.ingestion_run_id,
            "history_evaluator_version": HISTORY_EVALUATOR_VERSION,
            "first_orderbook_snapshot_id": request.first_orderbook_snapshot_id,
            "last_orderbook_snapshot_id": request.last_orderbook_snapshot_id,
            "maximum_observations": request.maximum_observations,
            "continuity_gap_seconds": int(request.continuity_gap.total_seconds()),
        }

    @staticmethod
    def _input_payload(
        configuration: dict[str, object], values: tuple[OrderbookHistoryEvidence, ...]
    ) -> dict[str, object]:
        return {
            "configuration": configuration,
            "observations": tuple(
                {
                    "orderbook_snapshot_id": value.orderbook.orderbook_snapshot_id,
                    "evaluation_hash": value.evaluation.canonical_hash,
                    "fee_hash": value.fee_evidence_hash,
                    "rule_evidence_hash": value.rule_evidence_hash,
                    "market_lineage_hash": value.market_lineage_hash,
                }
                for value in values
            ),
        }

    @staticmethod
    def _build(
        *,
        analysis_id: str,
        request: OrderbookHistoryRequest,
        configuration_hash: ContentHash,
        input_hash: ContentHash,
        values: tuple[OrderbookHistoryEvidence, ...],
    ) -> OrderbookHistoryReport:
        observations: list[OrderbookHistoryObservation] = []
        previous: OrderbookHistoryEvidence | None = None
        current_streak: list[OrderbookHistoryObservation] = []
        longest_streak: list[OrderbookHistoryObservation] = []
        maximum_gap = timedelta(0)
        evidence_breaks = observation_gaps = continuity_segments = 0

        for ordinal, value in enumerate(values):
            evaluation = value.evaluation
            reasons: list[str] = []
            reasons.extend(f"EVALUATION_{reason.value}" for reason in evaluation.reason_codes)
            if previous is None:
                continuity = OrderbookHistoryContinuityState.FIRST_OBSERVATION
                reasons.append("FIRST_RETAINED_ORDERBOOK_OBSERVATION")
                continuity_segments = 1
            else:
                gap = value.observed_at - previous.observed_at
                maximum_gap = max(maximum_gap, gap)
                incompatibilities = _compatibility_reasons(previous, value)
                if gap > request.continuity_gap:
                    continuity = OrderbookHistoryContinuityState.OBSERVATION_GAP
                    reasons.extend(("OBSERVATION_GAP_EXCEEDS_CONFIGURATION", *incompatibilities))
                    observation_gaps += 1
                    continuity_segments += 1
                elif incompatibilities:
                    continuity = OrderbookHistoryContinuityState.EVIDENCE_BREAK
                    reasons.extend(incompatibilities)
                    evidence_breaks += 1
                    continuity_segments += 1
                elif (
                    previous.evaluation.state is not ComplementState.CANDIDATE
                    or evaluation.state is not ComplementState.CANDIDATE
                ):
                    continuity = OrderbookHistoryContinuityState.NOT_APPLICABLE
                    if previous.evaluation.state is not ComplementState.CANDIDATE:
                        reasons.append("PRIOR_EVALUATION_NOT_CANDIDATE")
                    if evaluation.state is not ComplementState.CANDIDATE:
                        reasons.append(f"EVALUATION_{evaluation.state}_BREAKS_STREAK")
                else:
                    continuity = OrderbookHistoryContinuityState.CONTINUOUS

            prior_evaluation = previous.evaluation if previous else None
            comparable = continuity in {
                OrderbookHistoryContinuityState.CONTINUOUS,
                OrderbookHistoryContinuityState.NOT_APPLICABLE,
            }
            if comparable and prior_evaluation is not None:
                for label, current, prior in (
                    (
                        "MATCHED_QUANTITY",
                        _metric(evaluation, "matched_quantity"),
                        _metric(prior_evaluation, "matched_quantity"),
                    ),
                    (
                        "GROSS_COMPLEMENT_GAP",
                        _metric(evaluation, "gross_complement_gap"),
                        _metric(prior_evaluation, "gross_complement_gap"),
                    ),
                    (
                        "MODELED_TAKER_FEES",
                        _metric(evaluation, "modeled_taker_fees"),
                        _metric(prior_evaluation, "modeled_taker_fees"),
                    ),
                    (
                        "CONSERVATIVE_NET_LOWER_BOUND",
                        _metric(evaluation, "conservative_net_lower_bound"),
                        _metric(prior_evaluation, "conservative_net_lower_bound"),
                    ),
                    (
                        "YES_WEIGHTED_ASK",
                        _metric(evaluation, "weighted_yes_ask"),
                        _metric(prior_evaluation, "weighted_yes_ask"),
                    ),
                    (
                        "NO_WEIGHTED_ASK",
                        _metric(evaluation, "weighted_no_ask"),
                        _metric(prior_evaluation, "weighted_no_ask"),
                    ),
                ):
                    delta = _delta(current, prior)
                    if delta is not None and delta:
                        reasons.append(f"{label}_{'GREW' if delta > 0 else 'SHRANK'}")

            input_payload = {
                "analysis_id": analysis_id,
                "ordinal": ordinal,
                "evidence": value,
                "continuity": continuity,
            }
            fee = value.fee
            observation = OrderbookHistoryObservation(
                observation_id=deterministic_id("orderbook_history_observation", input_payload),
                analysis_id=analysis_id,
                ordinal=ordinal,
                orderbook_snapshot_id=value.orderbook.orderbook_snapshot_id,
                evaluation_id=evaluation.evaluation_id,
                market_snapshot_id=value.orderbook.market_snapshot_id,
                fee_snapshot_id=evaluation.fee_snapshot_id,
                observed_at=value.observed_at,
                state=_state(evaluation.state),
                matched_quantity=_metric(evaluation, "matched_quantity"),
                weighted_yes_ask=_metric(evaluation, "weighted_yes_ask"),
                weighted_no_ask=_metric(evaluation, "weighted_no_ask"),
                gross_acquisition_cost=_metric(evaluation, "gross_acquisition_cost"),
                gross_settlement_value=_metric(evaluation, "gross_settlement_value"),
                gross_complement_gap=_metric(evaluation, "gross_complement_gap"),
                yes_modeled_taker_fee=_metric(evaluation, "yes_modeled_taker_fee"),
                no_modeled_taker_fee=_metric(evaluation, "no_modeled_taker_fee"),
                modeled_taker_fees=_metric(evaluation, "modeled_taker_fees"),
                conservative_rounding_reserve=_metric(evaluation, "conservative_rounding_reserve"),
                total_modeled_acquisition_cost=_metric(
                    evaluation, "total_modeled_acquisition_cost"
                ),
                conservative_net_lower_bound=_metric(evaluation, "conservative_net_lower_bound"),
                net_lower_bound_per_contract=_metric(evaluation, "net_lower_bound_per_contract"),
                minimum_visible_matched_size=_metric(evaluation, "matched_quantity"),
                rule_evidence_hash=value.rule_evidence_hash,
                series_ticker=value.orderbook.series_ticker,
                fee_schedule_identity=fee.schedule_identity if fee else None,
                fee_multiplier=fee.fee_multiplier if fee else None,
                fee_evidence_hash=value.fee_evidence_hash,
                fee_model_identity=fee.fee_model_identity if fee else None,
                rounding_identity=fee.rounding_identity if fee else None,
                market_lineage_hash=value.market_lineage_hash,
                continuity_state=continuity,
                matched_quantity_delta=(
                    _delta(
                        _metric(evaluation, "matched_quantity"),
                        _metric(prior_evaluation, "matched_quantity"),
                    )
                    if comparable and prior_evaluation
                    else None
                ),
                gross_complement_gap_delta=(
                    _delta(
                        _metric(evaluation, "gross_complement_gap"),
                        _metric(prior_evaluation, "gross_complement_gap"),
                    )
                    if comparable and prior_evaluation
                    else None
                ),
                modeled_taker_fees_delta=(
                    _delta(
                        _metric(evaluation, "modeled_taker_fees"),
                        _metric(prior_evaluation, "modeled_taker_fees"),
                    )
                    if comparable and prior_evaluation
                    else None
                ),
                conservative_net_lower_bound_delta=(
                    _delta(
                        _metric(evaluation, "conservative_net_lower_bound"),
                        _metric(prior_evaluation, "conservative_net_lower_bound"),
                    )
                    if comparable and prior_evaluation
                    else None
                ),
                net_lower_bound_per_contract_delta=(
                    _delta(
                        _metric(evaluation, "net_lower_bound_per_contract"),
                        _metric(prior_evaluation, "net_lower_bound_per_contract"),
                    )
                    if comparable and prior_evaluation
                    else None
                ),
                weighted_yes_ask_delta=(
                    _delta(
                        _metric(evaluation, "weighted_yes_ask"),
                        _metric(prior_evaluation, "weighted_yes_ask"),
                    )
                    if comparable and prior_evaluation
                    else None
                ),
                weighted_no_ask_delta=(
                    _delta(
                        _metric(evaluation, "weighted_no_ask"),
                        _metric(prior_evaluation, "weighted_no_ask"),
                    )
                    if comparable and prior_evaluation
                    else None
                ),
                reason_codes=tuple(reasons),
                canonical_input_hash=ContentHash(content_digest(input_payload)),
            )
            observations.append(observation)
            if _candidate(value):
                if current_streak and continuity is OrderbookHistoryContinuityState.CONTINUOUS:
                    current_streak.append(observation)
                else:
                    current_streak = [observation]
                if len(current_streak) > len(longest_streak):
                    longest_streak = list(current_streak)
            else:
                current_streak = []
            previous = value

        def duration(streak: list[OrderbookHistoryObservation]) -> timedelta | None:
            return None if not streak else streak[-1].observed_at - streak[0].observed_at

        candidates = tuple(
            value
            for value in observations
            if value.state is OrderbookHistoryObservationState.PRESENT_CANDIDATE
        )
        compatible_candidates = tuple(
            value
            for value in observations
            if value.state is OrderbookHistoryObservationState.PRESENT_CANDIDATE
        )
        latest = observations[-1]
        analysis = OrderbookHistoryAnalysis(
            analysis_id=analysis_id,
            provider=PublicMarketProvider.KALSHI,
            provider_market_ticker=request.provider_market_ticker,
            history_evaluator_version=HISTORY_EVALUATOR_VERSION,
            first_orderbook_snapshot_id=values[0].orderbook.orderbook_snapshot_id,
            last_orderbook_snapshot_id=values[-1].orderbook.orderbook_snapshot_id,
            requested_maximum_observation_count=request.maximum_observations,
            included_observation_count=len(observations),
            candidate_observation_count=len(candidates),
            no_candidate_observation_count=sum(
                value.state is OrderbookHistoryObservationState.PRESENT_NO_CANDIDATE
                for value in observations
            ),
            ineligible_observation_count=sum(
                value.state is OrderbookHistoryObservationState.PRESENT_INELIGIBLE
                for value in observations
            ),
            invalid_observation_count=sum(
                value.state is OrderbookHistoryObservationState.PRESENT_INVALID
                for value in observations
            ),
            continuity_segment_count=continuity_segments,
            evidence_break_count=evidence_breaks,
            observation_gap_count=observation_gaps,
            current_candidate_streak_observation_count=len(current_streak),
            longest_candidate_streak_observation_count=len(longest_streak),
            current_candidate_streak_duration=duration(current_streak),
            longest_candidate_streak_duration=duration(longest_streak),
            first_candidate_observed_at=(candidates[0].observed_at if candidates else None),
            latest_candidate_observed_at=(candidates[-1].observed_at if candidates else None),
            current_matched_quantity=latest.matched_quantity,
            maximum_compatible_matched_quantity=max(
                (
                    value.matched_quantity
                    for value in compatible_candidates
                    if value.matched_quantity is not None
                ),
                default=None,
            ),
            current_conservative_net_lower_bound=latest.conservative_net_lower_bound,
            maximum_compatible_conservative_net_lower_bound=max(
                (
                    value.conservative_net_lower_bound
                    for value in compatible_candidates
                    if value.conservative_net_lower_bound is not None
                ),
                default=None,
            ),
            minimum_lower_bound_current_streak=min(
                (
                    value.conservative_net_lower_bound
                    for value in current_streak
                    if value.conservative_net_lower_bound is not None
                ),
                default=None,
            ),
            current_net_lower_bound_per_contract=latest.net_lower_bound_per_contract,
            maximum_observation_gap=maximum_gap,
            continuity_gap_threshold=request.continuity_gap,
            state=(
                OrderbookHistoryAnalysisState.INVALID
                if any(
                    value.state is OrderbookHistoryObservationState.PRESENT_INVALID
                    for value in observations
                )
                else OrderbookHistoryAnalysisState.PARTIAL
                if any(
                    value.state is OrderbookHistoryObservationState.PRESENT_INELIGIBLE
                    for value in observations
                )
                else OrderbookHistoryAnalysisState.COMPLETE
            ),
            canonical_configuration_hash=configuration_hash,
            canonical_input_hash=input_hash,
            created_at=latest.observed_at,
        )
        return OrderbookHistoryReport(
            analysis=analysis, observations=tuple(observations), idempotent_replay=False
        )

    def prepare(
        self,
        request: OrderbookHistoryRequest,
        values: tuple[OrderbookHistoryEvidence, ...],
    ) -> OrderbookHistoryReport:
        self._validate(values, request)
        configuration = self._configuration(request)
        configuration_hash = ContentHash(content_digest(configuration))
        input_hash = ContentHash(content_digest(self._input_payload(configuration, values)))
        analysis_id = deterministic_id(
            "orderbook_history_analysis",
            {"configuration_hash": configuration_hash, "input_hash": input_hash},
        )
        return self._build(
            analysis_id=analysis_id,
            request=request,
            configuration_hash=configuration_hash,
            input_hash=input_hash,
            values=values,
        )

    def build(self, request: OrderbookHistoryRequest) -> OrderbookHistoryReport:
        values = self._store.select_observations(
            provider=PublicMarketProvider.KALSHI,
            provider_market_ticker=request.provider_market_ticker,
            ingestion_run_id=request.ingestion_run_id,
            first_orderbook_snapshot_id=request.first_orderbook_snapshot_id,
            last_orderbook_snapshot_id=request.last_orderbook_snapshot_id,
            maximum_observations=request.maximum_observations,
        )
        report = self.prepare(request, values)
        existing = self._store.get_history(report.analysis.analysis_id)
        if existing is not None:
            if existing.analysis.canonical_input_hash != report.analysis.canonical_input_hash:
                raise OrderbookObservationHistoryConflictError(
                    "orderbook history analysis identity conflicts"
                )
            return OrderbookHistoryReport(existing.analysis, existing.observations, True)
        self._store.append_history(report.analysis, report.observations)
        persisted = self._store.get_history(report.analysis.analysis_id)
        if persisted is None:
            raise OrderbookObservationHistoryError("orderbook history was not retained atomically")
        return persisted


class InMemoryOrderbookObservationHistoryStore:
    """Fixture-only store with no provider, financial, or execution capability."""

    def __init__(self, values: tuple[OrderbookHistoryEvidence, ...]) -> None:
        self._values = values
        self._reports: dict[str, OrderbookHistoryReport] = {}

    def select_observations(
        self,
        *,
        provider: PublicMarketProvider,
        provider_market_ticker: str,
        ingestion_run_id: str | None,
        first_orderbook_snapshot_id: str | None,
        last_orderbook_snapshot_id: str | None,
        maximum_observations: int,
    ) -> tuple[OrderbookHistoryEvidence, ...]:
        if provider is not PublicMarketProvider.KALSHI:
            return ()
        ticker = provider_market_ticker
        maximum = maximum_observations
        first = first_orderbook_snapshot_id
        last = last_orderbook_snapshot_id
        values = tuple(
            value
            for value in self._values
            if value.orderbook.provider_market_ticker == ticker
            and (ingestion_run_id is None or value.orderbook.ingestion_run_id == ingestion_run_id)
        )
        values = tuple(sorted(values, key=lambda value: value.observed_at))
        if first is not None:
            start = next(
                (
                    index
                    for index, value in enumerate(values)
                    if value.orderbook.orderbook_snapshot_id == first
                ),
                None,
            )
            values = values[start:] if start is not None else ()
        if last is not None:
            end = next(
                (
                    index
                    for index, value in enumerate(values)
                    if value.orderbook.orderbook_snapshot_id == last
                ),
                None,
            )
            values = values[: end + 1] if end is not None else ()
        return values[-int(maximum) :]

    def get_history(self, analysis_id: str) -> OrderbookHistoryReport | None:
        return self._reports.get(analysis_id)

    def append_history(
        self,
        analysis: OrderbookHistoryAnalysis,
        observations: tuple[OrderbookHistoryObservation, ...],
    ) -> None:
        existing = self._reports.get(analysis.analysis_id)
        if existing is not None:
            if existing.analysis.canonical_result_hash != analysis.canonical_result_hash:
                raise OrderbookObservationHistoryConflictError(
                    "orderbook history immutable identity conflicts"
                )
            return
        self._reports[analysis.analysis_id] = OrderbookHistoryReport(analysis, observations, False)
