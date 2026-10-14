"""Immutable evidence reviews and deliberately bounded PAPER trial authority.

This module is intentionally an application boundary rather than a trading
strategy.  It turns already-retained, forward-only SHADOW evidence into a
transparent review packet and, only after an explicit operator decision,
permits a campaign-specific PAPER trial.  A trial is an additional constraint;
it never widens campaign, portfolio, allocator, risk-kernel, or execution
policy authority.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Protocol

from apex.application.autopilot import (
    StrategyAuthorityState,
    StrategyDescriptor,
    StrategyFamily,
    StrategyImplementationState,
)
from apex.application.strategy_shadow import ShadowOutcomeEvaluation, ShadowPrediction
from apex.domain.values import (
    ContentHash,
    Money,
    RiskFraction,
    ValueValidationError,
    content_digest,
    deterministic_id,
)

EVIDENCE_REVIEW_POLICY_VERSION = "apex-evidence-review-policy-v1"
EVIDENCE_SNAPSHOT_MATH_VERSION = "apex-evidence-snapshot-math-v1"
CRITERION_VERSION = "apex-evidence-criterion-v1"
PROBABILITY_RANGE_COVERAGE_VERSION = "fixed-five-bucket-v1"
CALIBRATION_COVERAGE_VERSION = "resolved-outcome-bucket-v1"
RECENT_DEGRADATION_VERSION = "recent-minus-full-conservative-v1"
NO_WORSE_THAN_CONSENSUS_VERSION = "difference-at-most-zero-v1"
SCORE_QUANTUM = Decimal("0.00000001")
LOG_LOSS_FLOOR = Decimal("0.00000001")
PROBABILITY_BUCKET_COUNT = 5


class EvidenceCriterionState(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"


class StrategyEvidenceReviewState(StrEnum):
    REQUESTED = "REQUESTED"
    IN_REVIEW = "IN_REVIEW"
    BLOCKED = "BLOCKED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"


class PaperTrialAuthorizationState(StrEnum):
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    SUSPENDED_LIMIT = "SUSPENDED_LIMIT"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class StrategyConfigurationLineageState(StrEnum):
    """Read-only classification of retained prediction configuration lineage."""

    PLAN_CONFIGURATION_MATCH = "PLAN_CONFIGURATION_MATCH"
    PLAN_CONFIGURATION_MISMATCH = "PLAN_CONFIGURATION_MISMATCH"
    UNPLANNED_CONFIGURATION_COMPLETE = "UNPLANNED_CONFIGURATION_COMPLETE"
    CONFIGURATION_LINEAGE_MISSING = "CONFIGURATION_LINEAGE_MISSING"
    CONFIGURATION_VERSION_MISMATCH = "CONFIGURATION_VERSION_MISMATCH"
    STRATEGY_VERSION_MISMATCH = "STRATEGY_VERSION_MISMATCH"


class EvidenceReviewError(RuntimeError):
    """A governed review or trial request is not safe to apply."""


class EvidenceReviewConflictError(EvidenceReviewError):
    """An idempotency identity was reused with different immutable content."""


def _text(value: str, *, label: str, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueValidationError(f"{label} must be bounded non-empty text")
    return value


def _aware(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None:
        raise ValueValidationError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _nonnegative(value: Decimal, *, label: str) -> Decimal:
    if not isinstance(value, Decimal) or value.is_nan() or value.is_infinite() or value < 0:
        raise ValueValidationError(f"{label} must be a finite non-negative Decimal")
    return value


def _signed_decimal(value: Decimal, *, label: str) -> Decimal:
    if not isinstance(value, Decimal) or value.is_nan() or value.is_infinite():
        raise ValueValidationError(f"{label} must be a finite Decimal")
    return value


@dataclass(frozen=True, slots=True)
class StrategyEvidenceReviewPolicy:
    """One immutable, versioned, independently inspectable gate definition."""

    policy_id: str
    policy_version: str
    supported_strategy_family: StrategyFamily | None
    supported_strategy_id: str | None
    minimum_resolved_predictions: int
    minimum_distinct_events: int
    minimum_distinct_markets: int
    minimum_probability_range_coverage: int
    maximum_unresolved_integrity_warnings: int
    maximum_point_brier_difference: Decimal
    maximum_conservative_brier_difference: Decimal
    maximum_point_log_loss_difference: Decimal
    maximum_conservative_log_loss_difference: Decimal
    maximum_recent_degradation: Decimal
    minimum_recent_resolved: int
    minimum_calibration_coverage: Decimal
    maximum_conflict_frequency: Decimal
    maximum_evidence_age: timedelta
    require_configuration_lineage: bool
    require_outcome_lineage: bool
    configuration_hash: ContentHash
    created_at: datetime
    superseded_at: datetime | None = None

    def __post_init__(self) -> None:
        _text(self.policy_id, label="review policy ID")
        _text(self.policy_version, label="review policy version")
        if bool(self.supported_strategy_family) == bool(self.supported_strategy_id):
            raise ValueValidationError(
                "review policy must support exactly one strategy family or explicit strategy"
            )
        if self.supported_strategy_id is not None:
            _text(self.supported_strategy_id, label="supported strategy ID")
        for label, value, minimum, maximum in (
            ("minimum resolved predictions", self.minimum_resolved_predictions, 1, 1_000_000),
            ("minimum distinct events", self.minimum_distinct_events, 1, 1_000_000),
            ("minimum distinct markets", self.minimum_distinct_markets, 1, 1_000_000),
            (
                "minimum probability-range coverage",
                self.minimum_probability_range_coverage,
                1,
                PROBABILITY_BUCKET_COUNT,
            ),
            (
                "maximum unresolved integrity warnings",
                self.maximum_unresolved_integrity_warnings,
                0,
                1_000_000,
            ),
            ("minimum recent resolved", self.minimum_recent_resolved, 1, 1_000_000),
        ):
            if not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueValidationError(f"{label} is outside the supported range")
        decimal_limits: tuple[tuple[str, Decimal], ...] = (
            ("maximum point Brier difference", self.maximum_point_brier_difference),
            ("maximum conservative Brier difference", self.maximum_conservative_brier_difference),
            ("maximum point log-loss difference", self.maximum_point_log_loss_difference),
            (
                "maximum conservative log-loss difference",
                self.maximum_conservative_log_loss_difference,
            ),
            ("maximum recent degradation", self.maximum_recent_degradation),
            ("minimum calibration coverage", self.minimum_calibration_coverage),
            ("maximum conflict frequency", self.maximum_conflict_frequency),
        )
        for decimal_label, decimal_value in decimal_limits:
            _nonnegative(decimal_value, label=decimal_label)
        if self.minimum_calibration_coverage > 1 or self.maximum_conflict_frequency > 1:
            raise ValueValidationError("review-policy fractions must not exceed one")
        if self.maximum_evidence_age <= timedelta(0):
            raise ValueValidationError("maximum evidence age must be positive")
        created_at = _aware(self.created_at, label="policy creation timestamp")
        if (
            self.superseded_at is not None
            and _aware(self.superseded_at, label="policy supersession timestamp") <= created_at
        ):
            raise ValueValidationError("policy supersession must follow creation")

    @property
    def identity(self) -> tuple[str, str]:
        return (self.policy_id, self.policy_version)

    def supports(self, descriptor: StrategyDescriptor) -> bool:
        return (
            descriptor.strategy_id == self.supported_strategy_id
            if self.supported_strategy_id is not None
            else descriptor.family is self.supported_strategy_family
        )

    def is_current_at(self, at: datetime) -> bool:
        return self.superseded_at is None or _aware(at, label="review timestamp") < _aware(
            self.superseded_at, label="policy supersession timestamp"
        )


def initial_evidence_review_policies(now: datetime) -> tuple[StrategyEvidenceReviewPolicy, ...]:
    """Initial conservative definitions, not a claim of universal optimality."""

    timestamp = _aware(now, label="policy timestamp")

    def policy(family: StrategyFamily) -> StrategyEvidenceReviewPolicy:
        payload = {
            "version": EVIDENCE_REVIEW_POLICY_VERSION,
            "family": family.value,
            "minimum_resolved_predictions": 100,
            "minimum_distinct_events": 30,
            "minimum_distinct_markets": 30,
            "minimum_probability_range_coverage": 3,
            "maximum_unresolved_integrity_warnings": 0,
            "no_worse_than_consensus": NO_WORSE_THAN_CONSENSUS_VERSION,
            "maximum_recent_degradation": "0.03000000",
            "minimum_recent_resolved": 20,
            "minimum_calibration_coverage": "0.60000000",
            "maximum_conflict_frequency": "0.10000000",
            "maximum_evidence_age_seconds": 90 * 24 * 3600,
            "probability_range_coverage": PROBABILITY_RANGE_COVERAGE_VERSION,
            "calibration_coverage": CALIBRATION_COVERAGE_VERSION,
            "recent_degradation": RECENT_DEGRADATION_VERSION,
        }
        return StrategyEvidenceReviewPolicy(
            policy_id=f"{family.value.lower()}-paper-evidence-v1",
            policy_version=EVIDENCE_REVIEW_POLICY_VERSION,
            supported_strategy_family=family,
            supported_strategy_id=None,
            minimum_resolved_predictions=100,
            minimum_distinct_events=30,
            minimum_distinct_markets=30,
            minimum_probability_range_coverage=3,
            maximum_unresolved_integrity_warnings=0,
            maximum_point_brier_difference=Decimal(0),
            maximum_conservative_brier_difference=Decimal(0),
            maximum_point_log_loss_difference=Decimal(0),
            maximum_conservative_log_loss_difference=Decimal(0),
            maximum_recent_degradation=Decimal("0.03000000"),
            minimum_recent_resolved=20,
            minimum_calibration_coverage=Decimal("0.60000000"),
            maximum_conflict_frequency=Decimal("0.10000000"),
            maximum_evidence_age=timedelta(days=90),
            require_configuration_lineage=True,
            require_outcome_lineage=True,
            configuration_hash=ContentHash(content_digest(payload)),
            created_at=timestamp,
        )

    return (policy(StrategyFamily.MARKET_STRUCTURE), policy(StrategyFamily.MARKET_DYNAMICS))


@dataclass(frozen=True, slots=True)
class EvidenceReviewSource:
    """Immutable rows selected before one snapshot is calculated."""

    descriptor: StrategyDescriptor
    predictions: tuple[ShadowPrediction, ...]
    evaluations: tuple[ShadowOutcomeEvaluation, ...]
    integrity_warnings: tuple[str, ...] = ()
    conflict_count: int = 0
    opportunity_count: int = 0
    hypothetical_admission_count: int = 0
    hypothetical_rejection_count: int = 0
    paper_cycles: int = 0
    paper_orders: int = 0
    paper_positions: int = 0
    configuration_lineage: tuple[PredictionConfigurationLineage, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("conflict count", self.conflict_count),
            ("opportunity count", self.opportunity_count),
            ("hypothetical admission count", self.hypothetical_admission_count),
            ("hypothetical rejection count", self.hypothetical_rejection_count),
            ("paper cycle count", self.paper_cycles),
            ("paper order count", self.paper_orders),
            ("paper position count", self.paper_positions),
        ):
            if not isinstance(value, int) or value < 0:
                raise ValueValidationError(f"{label} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PredictionConfigurationLineage:
    """One non-mutating configuration classification for a retained prediction."""

    prediction_id: str
    configuration_hash: ContentHash | None
    state: StrategyConfigurationLineageState
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.prediction_id, label="prediction configuration lineage ID")
        if not self.reason_codes:
            raise ValueValidationError("prediction configuration lineage requires reasons")


@dataclass(frozen=True, slots=True)
class StrategyEvidenceSnapshot:
    snapshot_id: str
    strategy_id: str
    strategy_version: str
    configuration_hash: ContentHash
    policy_id: str
    policy_version: str
    cutoff_at: datetime
    full_history_started_at: datetime
    recent_history_started_at: datetime
    total_predictions: int
    unresolved_predictions: int
    resolved_predictions: int
    recent_resolved_predictions: int
    distinct_events: int
    distinct_markets: int
    probability_range_coverage: int
    calibration_coverage: Decimal
    point_brier: Decimal | None
    conservative_brier: Decimal | None
    consensus_brier: Decimal | None
    point_log_loss: Decimal | None
    conservative_log_loss: Decimal | None
    consensus_log_loss: Decimal | None
    point_brier_difference: Decimal | None
    conservative_brier_difference: Decimal | None
    point_log_loss_difference: Decimal | None
    conservative_log_loss_difference: Decimal | None
    recent_conservative_brier: Decimal | None
    recent_conservative_log_loss: Decimal | None
    recent_brier_degradation: Decimal | None
    recent_log_loss_degradation: Decimal | None
    direction_accuracy: Decimal | None
    calibration_summary_identity: ContentHash
    conflict_frequency: Decimal
    opportunity_frequency: Decimal
    hypothetical_admission_frequency: Decimal
    paper_cycles: int
    paper_orders: int
    paper_positions: int
    integrity_warnings: tuple[str, ...]
    configuration_lineage_complete: bool
    outcome_lineage_complete: bool
    source_set_hash: ContentHash
    canonical_input_hash: ContentHash
    canonical_snapshot_hash: ContentHash
    created_at: datetime
    configuration_exclusion_counts: tuple[tuple[str, int], ...] = ()

    @property
    def stale_at(self) -> datetime:
        return self.cutoff_at


@dataclass(frozen=True, slots=True)
class EvidenceCriterionResult:
    criterion_result_id: str
    snapshot_id: str
    criterion_code: str
    criterion_version: str
    observed_value: str
    required_value: str
    state: EvidenceCriterionState
    hard_gate: bool
    reason_codes: tuple[str, ...]
    canonical_input_hash: ContentHash
    canonical_result_hash: ContentHash


@dataclass(frozen=True, slots=True)
class BuildStrategyEvidenceSnapshotCommand:
    strategy_id: str
    strategy_version: str
    configuration_hash: ContentHash
    policy_id: str
    policy_version: str
    cutoff_at: datetime
    full_history_started_at: datetime
    recent_history_started_at: datetime
    idempotency_key: str

    def __post_init__(self) -> None:
        _text(self.strategy_id, label="strategy ID")
        _text(self.strategy_version, label="strategy version")
        _text(self.policy_id, label="review policy ID")
        _text(self.policy_version, label="review policy version")
        _text(self.idempotency_key, label="evidence snapshot idempotency key")
        cutoff = _aware(self.cutoff_at, label="evidence cutoff")
        full = _aware(self.full_history_started_at, label="full-history start")
        recent = _aware(self.recent_history_started_at, label="recent-history start")
        if not full < recent <= cutoff:
            raise ValueValidationError("evidence windows must satisfy full < recent <= cutoff")

    @property
    def request_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


def _mean(values: Iterable[Decimal]) -> Decimal | None:
    items = tuple(values)
    if not items:
        return None
    return (sum(items, Decimal(0)) / Decimal(len(items))).quantize(SCORE_QUANTUM)


def _log_loss(probability: Decimal) -> Decimal:
    clipped = min(max(probability, LOG_LOSS_FLOOR), Decimal(1) - LOG_LOSS_FLOOR)
    with localcontext() as context:
        context.prec = 50
        return (-clipped.ln()).quantize(SCORE_QUANTUM)


def _brier(probabilities: tuple[tuple[str, Decimal], ...], outcome: str) -> Decimal:
    return (
        sum(
            (probability - (Decimal(1) if outcome_id == outcome else Decimal(0))) ** 2
            for outcome_id, probability in probabilities
        )
        / Decimal(len(probabilities))
    ).quantize(SCORE_QUANTUM)


def _metric_string(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _criterion(
    *,
    snapshot_id: str,
    code: str,
    observed: object,
    required: object,
    state: EvidenceCriterionState,
    reasons: tuple[str, ...] = (),
    hard_gate: bool = True,
) -> EvidenceCriterionResult:
    payload = {
        "snapshot_id": snapshot_id,
        "criterion_code": code,
        "criterion_version": CRITERION_VERSION,
        "observed": _metric_string(observed),
        "required": _metric_string(required),
        "state": state.value,
        "hard_gate": hard_gate,
        "reasons": reasons,
    }
    digest = ContentHash(content_digest(payload))
    return EvidenceCriterionResult(
        criterion_result_id=deterministic_id("evidence_criterion", payload),
        snapshot_id=snapshot_id,
        criterion_code=code,
        criterion_version=CRITERION_VERSION,
        observed_value=str(payload["observed"]),
        required_value=str(payload["required"]),
        state=state,
        hard_gate=hard_gate,
        reason_codes=reasons,
        canonical_input_hash=digest,
        canonical_result_hash=digest,
    )


def evaluate_evidence_criteria(
    snapshot: StrategyEvidenceSnapshot, policy: StrategyEvidenceReviewPolicy
) -> tuple[EvidenceCriterionResult, ...]:
    """Evaluate every gate independently; there is intentionally no composite."""

    results: list[EvidenceCriterionResult] = []

    def minimum(code: str, observed: int, required: int) -> None:
        results.append(
            _criterion(
                snapshot_id=snapshot.snapshot_id,
                code=code,
                observed=observed,
                required=required,
                state=EvidenceCriterionState.PASS
                if observed >= required
                else EvidenceCriterionState.FAIL,
                reasons=() if observed >= required else (f"{code}_INSUFFICIENT",),
            )
        )

    minimum(
        "RESOLVED_PREDICTIONS", snapshot.resolved_predictions, policy.minimum_resolved_predictions
    )
    minimum("DISTINCT_EVENTS", snapshot.distinct_events, policy.minimum_distinct_events)
    minimum("DISTINCT_MARKETS", snapshot.distinct_markets, policy.minimum_distinct_markets)
    minimum(
        "PROBABILITY_RANGE_COVERAGE",
        snapshot.probability_range_coverage,
        policy.minimum_probability_range_coverage,
    )
    integrity_state = (
        EvidenceCriterionState.PASS
        if len(snapshot.integrity_warnings) <= policy.maximum_unresolved_integrity_warnings
        else EvidenceCriterionState.BLOCKED_INTEGRITY
    )
    results.append(
        _criterion(
            snapshot_id=snapshot.snapshot_id,
            code="INTEGRITY_WARNINGS",
            observed=len(snapshot.integrity_warnings),
            required=policy.maximum_unresolved_integrity_warnings,
            state=integrity_state,
            reasons=snapshot.integrity_warnings,
        )
    )

    def maximum(code: str, observed: Decimal | None, required: Decimal) -> None:
        reasons: tuple[str, ...]
        if observed is None:
            state = EvidenceCriterionState.UNKNOWN
            reasons = (f"{code}_UNAVAILABLE",)
        elif observed <= required:
            state = EvidenceCriterionState.PASS
            reasons = ()
        else:
            state = EvidenceCriterionState.FAIL
            reasons = (f"{code}_EXCEEDED",)
        results.append(
            _criterion(
                snapshot_id=snapshot.snapshot_id,
                code=code,
                observed=observed if observed is not None else "UNKNOWN",
                required=required,
                state=state,
                reasons=reasons,
            )
        )

    maximum(
        "POINT_BRIER_VS_CONSENSUS",
        snapshot.point_brier_difference,
        policy.maximum_point_brier_difference,
    )
    maximum(
        "CONSERVATIVE_BRIER_VS_CONSENSUS",
        snapshot.conservative_brier_difference,
        policy.maximum_conservative_brier_difference,
    )
    maximum(
        "POINT_LOG_LOSS_VS_CONSENSUS",
        snapshot.point_log_loss_difference,
        policy.maximum_point_log_loss_difference,
    )
    maximum(
        "CONSERVATIVE_LOG_LOSS_VS_CONSENSUS",
        snapshot.conservative_log_loss_difference,
        policy.maximum_conservative_log_loss_difference,
    )
    if snapshot.recent_resolved_predictions < policy.minimum_recent_resolved:
        results.extend(
            (
                _criterion(
                    snapshot_id=snapshot.snapshot_id,
                    code="RECENT_CONSERVATIVE_BRIER_DEGRADATION",
                    observed=snapshot.recent_resolved_predictions,
                    required=policy.minimum_recent_resolved,
                    state=EvidenceCriterionState.UNKNOWN,
                    reasons=("RECENT_WINDOW_INSUFFICIENT",),
                ),
                _criterion(
                    snapshot_id=snapshot.snapshot_id,
                    code="RECENT_CONSERVATIVE_LOG_LOSS_DEGRADATION",
                    observed=snapshot.recent_resolved_predictions,
                    required=policy.minimum_recent_resolved,
                    state=EvidenceCriterionState.UNKNOWN,
                    reasons=("RECENT_WINDOW_INSUFFICIENT",),
                ),
            )
        )
    else:
        maximum(
            "RECENT_CONSERVATIVE_BRIER_DEGRADATION",
            snapshot.recent_brier_degradation,
            policy.maximum_recent_degradation,
        )
        maximum(
            "RECENT_CONSERVATIVE_LOG_LOSS_DEGRADATION",
            snapshot.recent_log_loss_degradation,
            policy.maximum_recent_degradation,
        )
    maximum("CONFLICT_FREQUENCY", snapshot.conflict_frequency, policy.maximum_conflict_frequency)
    maximum(
        "CALIBRATION_COVERAGE_GAP",
        Decimal(1) - snapshot.calibration_coverage,
        Decimal(1) - policy.minimum_calibration_coverage,
    )
    for code, observed, required in (
        (
            "CONFIGURATION_LINEAGE",
            snapshot.configuration_lineage_complete,
            policy.require_configuration_lineage,
        ),
        ("OUTCOME_LINEAGE", snapshot.outcome_lineage_complete, policy.require_outcome_lineage),
    ):
        passed = not required or observed
        results.append(
            _criterion(
                snapshot_id=snapshot.snapshot_id,
                code=code,
                observed=observed,
                required=required,
                state=EvidenceCriterionState.PASS
                if passed
                else EvidenceCriterionState.BLOCKED_INTEGRITY,
                reasons=() if passed else (f"{code}_INCOMPLETE",),
            )
        )
    return tuple(results)


class EvidenceSnapshotStore(Protocol):
    def policy(self, policy_id: str, policy_version: str) -> StrategyEvidenceReviewPolicy: ...

    def evidence_source(
        self, *, strategy_id: str, strategy_version: str, cutoff_at: datetime
    ) -> EvidenceReviewSource: ...

    def save_snapshot(
        self,
        snapshot: StrategyEvidenceSnapshot,
        criteria: tuple[EvidenceCriterionResult, ...],
        *,
        idempotency_key: str,
    ) -> tuple[StrategyEvidenceSnapshot, tuple[EvidenceCriterionResult, ...]]: ...


class BuildStrategyEvidenceSnapshot:
    """Freeze retained forward evidence into one never-recomputed review packet."""

    def __init__(self, store: EvidenceSnapshotStore) -> None:
        self._store = store

    def build(
        self, command: BuildStrategyEvidenceSnapshotCommand
    ) -> tuple[StrategyEvidenceSnapshot, tuple[EvidenceCriterionResult, ...]]:
        policy = self._store.policy(command.policy_id, command.policy_version)
        source = self._store.evidence_source(
            strategy_id=command.strategy_id,
            strategy_version=command.strategy_version,
            cutoff_at=command.cutoff_at,
        )
        descriptor = source.descriptor
        if not policy.supports(descriptor):
            raise EvidenceReviewError("review policy is not compatible with this strategy")
        cutoff = _aware(command.cutoff_at, label="evidence cutoff")
        full_start = _aware(command.full_history_started_at, label="full-history start")
        recent_start = _aware(command.recent_history_started_at, label="recent-history start")
        requested_predictions = tuple(
            item
            for item in source.predictions
            if item.strategy_id == command.strategy_id
            and item.strategy_version == command.strategy_version
            and full_start <= item.decision_at <= cutoff
        )
        lineage_by_prediction = {item.prediction_id: item for item in source.configuration_lineage}
        eligible_states = {
            StrategyConfigurationLineageState.PLAN_CONFIGURATION_MATCH,
            StrategyConfigurationLineageState.UNPLANNED_CONFIGURATION_COMPLETE,
        }
        exclusion_counts: dict[str, int] = defaultdict(int)
        selected_predictions: list[ShadowPrediction] = []
        for item in requested_predictions:
            lineage = lineage_by_prediction.get(item.prediction_id)
            if lineage is None:
                # Focused in-memory fixtures predating configuration lineage
                # retain the descriptor's single governed configuration.
                if command.configuration_hash == descriptor.configuration_hash:
                    selected_predictions.append(item)
                    continue
                exclusion_counts[
                    StrategyConfigurationLineageState.CONFIGURATION_LINEAGE_MISSING.value
                ] += 1
                continue
            if lineage.state not in eligible_states:
                exclusion_counts[lineage.state.value] += 1
                continue
            if lineage.configuration_hash != command.configuration_hash:
                exclusion_counts["CONFIGURATION_HASH_NOT_SELECTED"] += 1
                continue
            selected_predictions.append(item)
        predictions = tuple(selected_predictions)
        by_prediction = {item.prediction_id: item for item in predictions}
        evaluations = tuple(
            item
            for item in source.evaluations
            if item.prediction_id in by_prediction and item.evaluated_at <= cutoff
        )
        evaluation_by_prediction = {item.prediction_id: item for item in evaluations}
        resolved_predictions = tuple(by_prediction[key] for key in evaluation_by_prediction)
        unresolved = len(predictions) - len(resolved_predictions)
        events = {item.event_id for item in resolved_predictions if item.event_id}
        markets = {item.instrument_id for item in resolved_predictions}
        conservative_brier_rows: list[Decimal] = []
        conservative_log_rows: list[Decimal] = []
        covered_buckets: set[int] = set()
        calibration_buckets: dict[int, list[tuple[Decimal, int]]] = defaultdict(list)
        recent_evaluations: list[tuple[ShadowPrediction, ShadowOutcomeEvaluation]] = []
        for prediction_id, evaluation in evaluation_by_prediction.items():
            prediction = by_prediction[prediction_id]
            outcome = evaluation.resolved_outcome
            conservative = dict(prediction.conservative_probabilities)
            point = dict(prediction.point_probabilities)
            if outcome not in conservative or outcome not in point:
                continue
            conservative_brier_rows.append(_brier(prediction.conservative_probabilities, outcome))
            conservative_log_rows.append(_log_loss(conservative[outcome]))
            bucket = min(
                PROBABILITY_BUCKET_COUNT - 1, int(point[outcome] * PROBABILITY_BUCKET_COUNT)
            )
            covered_buckets.add(bucket)
            calibration_buckets[bucket].append(
                (
                    point[outcome],
                    1 if evaluation.direction_correct else 0,
                )
            )
            if evaluation.evaluated_at >= recent_start:
                recent_evaluations.append((prediction, evaluation))
        point_brier = _mean(item.brier_score for item in evaluations)
        consensus_brier = _mean(item.market_baseline_brier_score for item in evaluations)
        point_log = _mean(item.log_loss for item in evaluations)
        consensus_log = _mean(item.market_baseline_log_loss for item in evaluations)
        conservative_brier = _mean(conservative_brier_rows)
        conservative_log = _mean(conservative_log_rows)
        recent_conservative_brier = _mean(
            _brier(prediction.conservative_probabilities, evaluation.resolved_outcome)
            for prediction, evaluation in recent_evaluations
        )
        recent_conservative_log = _mean(
            _log_loss(dict(prediction.conservative_probabilities)[evaluation.resolved_outcome])
            for prediction, evaluation in recent_evaluations
        )
        warnings = list(source.integrity_warnings)
        if len(evaluations) != len(resolved_predictions):
            warnings.append("EVALUATION_LINEAGE_INCOMPLETE")
        configuration_complete = all(
            (lineage_by_prediction.get(item.prediction_id) is None)
            or (
                lineage_by_prediction[item.prediction_id].state in eligible_states
                and lineage_by_prediction[item.prediction_id].configuration_hash
                == command.configuration_hash
            )
            for item in predictions
        )
        if not configuration_complete:
            warnings.append("CONFIGURATION_LINEAGE_INCOMPLETE")
        outcome_complete = all(
            item.settlement_fact_id and item.canonical_input_hash and item.canonical_result_hash
            for item in evaluations
        )
        if not outcome_complete:
            warnings.append("OUTCOME_LINEAGE_INCOMPLETE")
        calibration_coverage = (
            Decimal(len(calibration_buckets)) / Decimal(PROBABILITY_BUCKET_COUNT)
            if evaluations
            else Decimal(0)
        )
        source_payload = {
            "predictions": tuple(item.canonical_prediction_hash for item in predictions),
            "evaluations": tuple(item.canonical_result_hash for item in evaluations),
            "configuration_lineage": tuple(
                {
                    "prediction_id": item.prediction_id,
                    "configuration_hash": (
                        item.configuration_hash.value
                        if item.configuration_hash is not None
                        else None
                    ),
                    "state": item.state.value,
                    "reason_codes": item.reason_codes,
                }
                for item in sorted(
                    source.configuration_lineage, key=lambda item: item.prediction_id
                )
            ),
            "source_warnings": tuple(sorted(set(warnings))),
            "conflict_count": source.conflict_count,
            "opportunity_count": source.opportunity_count,
            "hypothetical_admission_count": source.hypothetical_admission_count,
            "hypothetical_rejection_count": source.hypothetical_rejection_count,
            "configuration_exclusion_counts": tuple(sorted(exclusion_counts.items())),
        }
        source_hash = ContentHash(content_digest(source_payload))
        snapshot_payload = {
            "command": command.request_hash.value,
            "source_hash": source_hash.value,
            "math_version": EVIDENCE_SNAPSHOT_MATH_VERSION,
            "probability_coverage_version": PROBABILITY_RANGE_COVERAGE_VERSION,
            "calibration_version": CALIBRATION_COVERAGE_VERSION,
            "recent_degradation_version": RECENT_DEGRADATION_VERSION,
        }
        snapshot_id = deterministic_id("strategy_evidence_snapshot", snapshot_payload)
        snapshot = StrategyEvidenceSnapshot(
            snapshot_id=snapshot_id,
            strategy_id=command.strategy_id,
            strategy_version=command.strategy_version,
            configuration_hash=command.configuration_hash,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            cutoff_at=cutoff,
            full_history_started_at=full_start,
            recent_history_started_at=recent_start,
            total_predictions=len(predictions),
            unresolved_predictions=unresolved,
            resolved_predictions=len(evaluations),
            recent_resolved_predictions=len(recent_evaluations),
            distinct_events=len(events),
            distinct_markets=len(markets),
            probability_range_coverage=len(covered_buckets),
            calibration_coverage=calibration_coverage,
            point_brier=point_brier,
            conservative_brier=conservative_brier,
            consensus_brier=consensus_brier,
            point_log_loss=point_log,
            conservative_log_loss=conservative_log,
            consensus_log_loss=consensus_log,
            point_brier_difference=(point_brier - consensus_brier).quantize(SCORE_QUANTUM)
            if point_brier is not None and consensus_brier is not None
            else None,
            conservative_brier_difference=(conservative_brier - consensus_brier).quantize(
                SCORE_QUANTUM
            )
            if conservative_brier is not None and consensus_brier is not None
            else None,
            point_log_loss_difference=(point_log - consensus_log).quantize(SCORE_QUANTUM)
            if point_log is not None and consensus_log is not None
            else None,
            conservative_log_loss_difference=(conservative_log - consensus_log).quantize(
                SCORE_QUANTUM
            )
            if conservative_log is not None and consensus_log is not None
            else None,
            recent_conservative_brier=recent_conservative_brier,
            recent_conservative_log_loss=recent_conservative_log,
            recent_brier_degradation=(recent_conservative_brier - conservative_brier).quantize(
                SCORE_QUANTUM
            )
            if recent_conservative_brier is not None and conservative_brier is not None
            else None,
            recent_log_loss_degradation=(recent_conservative_log - conservative_log).quantize(
                SCORE_QUANTUM
            )
            if recent_conservative_log is not None and conservative_log is not None
            else None,
            direction_accuracy=_mean(
                Decimal(1) if item.direction_correct else Decimal(0) for item in evaluations
            ),
            calibration_summary_identity=ContentHash(
                content_digest(
                    {
                        "version": CALIBRATION_COVERAGE_VERSION,
                        "buckets": {
                            str(key): tuple(
                                (str(probability), actual) for probability, actual in value
                            )
                            for key, value in sorted(calibration_buckets.items())
                        },
                    }
                )
            ),
            conflict_frequency=(
                Decimal(source.conflict_count) / Decimal(len(predictions))
                if predictions
                else Decimal(0)
            ),
            opportunity_frequency=(
                Decimal(source.opportunity_count) / Decimal(len(predictions))
                if predictions
                else Decimal(0)
            ),
            hypothetical_admission_frequency=(
                Decimal(source.hypothetical_admission_count)
                / Decimal(source.hypothetical_admission_count + source.hypothetical_rejection_count)
                if source.hypothetical_admission_count + source.hypothetical_rejection_count
                else Decimal(0)
            ),
            paper_cycles=source.paper_cycles,
            paper_orders=source.paper_orders,
            paper_positions=source.paper_positions,
            integrity_warnings=tuple(sorted(set(warnings))),
            configuration_lineage_complete=configuration_complete,
            outcome_lineage_complete=outcome_complete,
            source_set_hash=source_hash,
            canonical_input_hash=command.request_hash,
            canonical_snapshot_hash=ContentHash(content_digest(snapshot_payload)),
            created_at=cutoff,
            configuration_exclusion_counts=tuple(sorted(exclusion_counts.items())),
        )
        criteria = evaluate_evidence_criteria(snapshot, policy)
        return self._store.save_snapshot(
            snapshot, criteria, idempotency_key=command.idempotency_key
        )


@dataclass(frozen=True, slots=True)
class StrategyEvidenceReview:
    review_id: str
    strategy_id: str
    strategy_version: str
    snapshot_id: str
    requested_campaign_id: str
    state: StrategyEvidenceReviewState
    requested_at: datetime
    review_started_at: datetime | None
    decided_at: datetime | None
    expires_at: datetime
    requested_authority: StrategyAuthorityState
    request_action_id: str
    decision_action_id: str | None
    operator_reason: str
    reviewer_note: str | None
    idempotency_key: str
    canonical_request_hash: ContentHash
    canonical_result_hash: ContentHash | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperTrialLimits:
    paper_execution_policy_id: str
    paper_execution_policy_hash: ContentHash
    starts_at: datetime
    expires_at: datetime
    maximum_paper_cycles: int
    maximum_approved_opportunities: int
    maximum_paper_orders: int
    maximum_simultaneous_open_positions: int
    maximum_per_opportunity_risk_fraction: RiskFraction
    maximum_strategy_active_risk_fraction: RiskFraction
    maximum_cumulative_authorized_risk: Money
    maximum_cumulative_realized_paper_loss: Money
    maximum_paper_drawdown: Money
    maximum_capital_lockup_seconds: int
    allowed_instrument_class: str
    allowed_provider: str

    def __post_init__(self) -> None:
        _text(self.paper_execution_policy_id, label="paper execution policy ID")
        _text(self.allowed_instrument_class, label="allowed instrument class", maximum=80)
        _text(self.allowed_provider, label="allowed provider", maximum=80)
        starts_at = _aware(self.starts_at, label="trial start")
        if _aware(self.expires_at, label="trial expiry") <= starts_at:
            raise ValueValidationError("trial expiry must follow its start")
        for label, value, maximum in (
            ("maximum paper cycles", self.maximum_paper_cycles, 10_000),
            ("maximum approved opportunities", self.maximum_approved_opportunities, 10_000),
            ("maximum paper orders", self.maximum_paper_orders, 10_000),
            ("maximum simultaneous open positions", self.maximum_simultaneous_open_positions, 100),
            ("maximum capital lockup", self.maximum_capital_lockup_seconds, 86_400),
        ):
            if not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueValidationError(f"{label} is outside the supported range")
        monetary_limits: tuple[tuple[str, Money], ...] = (
            ("maximum cumulative authorized risk", self.maximum_cumulative_authorized_risk),
            ("maximum cumulative realized paper loss", self.maximum_cumulative_realized_paper_loss),
            ("maximum paper drawdown", self.maximum_paper_drawdown),
        )
        for monetary_label, monetary_value in monetary_limits:
            if monetary_value.amount <= 0:
                raise ValueValidationError(f"{monetary_label} must be positive")


@dataclass(frozen=True, slots=True)
class PaperTrialUsage:
    completed_cycles: int = 0
    approved_opportunities: int = 0
    paper_orders: int = 0
    open_positions: int = 0
    active_strategy_risk: Money = field(default_factory=lambda: Money(0))
    cumulative_authorized_risk: Money = field(default_factory=lambda: Money(0))
    # Result is deliberately signed: a negative result is the evidence used by
    # the independently non-negative loss and drawdown limits below.
    cumulative_realized_result: Decimal = Decimal(0)
    paper_drawdown: Money = field(default_factory=lambda: Money(0))
    maximum_capital_lockup_seconds: int = 0

    def __post_init__(self) -> None:
        _signed_decimal(self.cumulative_realized_result, label="cumulative realized result")


@dataclass(frozen=True, slots=True)
class PaperTrialAuthorization:
    authorization_id: str
    review_id: str
    strategy_id: str
    strategy_version: str
    campaign_id: str
    strategy_configuration_hash: ContentHash
    limits: PaperTrialLimits
    state: PaperTrialAuthorizationState
    authorized_at: datetime
    suspension_reason: str | None
    revocation_reason: str | None
    operator_action_id: str
    configuration_hash: ContentHash
    canonical_input_hash: ContentHash
    canonical_result_hash: ContentHash
    usage: PaperTrialUsage

    def active_at(self, at: datetime) -> bool:
        moment = _aware(at, label="trial activity timestamp")
        return (
            self.state is PaperTrialAuthorizationState.ACTIVE
            and self.limits.starts_at <= moment < self.limits.expires_at
        )


def paper_trial_confirmation_text(*, strategy_id: str, campaign_id: str, snapshot_id: str) -> str:
    return f"APPROVE_PAPER_TRIAL:{strategy_id}:{campaign_id}:{snapshot_id}"


def hard_gate_reasons(criteria: Iterable[EvidenceCriterionResult]) -> tuple[str, ...]:
    return tuple(
        f"{item.criterion_code}_{item.state.value}"
        for item in criteria
        if item.hard_gate and item.state is not EvidenceCriterionState.PASS
    )


@dataclass(frozen=True, slots=True)
class RequestStrategyEvidenceReviewCommand:
    strategy_id: str
    strategy_version: str
    snapshot_id: str
    campaign_id: str
    expires_at: datetime
    operator_reason: str
    operator_note: str
    idempotency_key: str

    @property
    def request_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class DecideStrategyEvidenceReviewCommand:
    review_id: str
    decision: StrategyEvidenceReviewState
    operator_reason: str
    operator_note: str
    idempotency_key: str
    confirmation_text: str | None = None

    def __post_init__(self) -> None:
        if self.decision not in (
            StrategyEvidenceReviewState.APPROVED,
            StrategyEvidenceReviewState.REJECTED,
        ):
            raise ValueValidationError("review decision must be APPROVED or REJECTED")


class EvidenceReviewWorkflowStore(EvidenceSnapshotStore, Protocol):
    def snapshot(self, snapshot_id: str) -> StrategyEvidenceSnapshot: ...

    def criteria(self, snapshot_id: str) -> tuple[EvidenceCriterionResult, ...]: ...

    def descriptor(self, strategy_id: str) -> StrategyDescriptor: ...

    def request_review(
        self, command: RequestStrategyEvidenceReviewCommand, *, now: datetime
    ) -> StrategyEvidenceReview: ...

    def review(self, review_id: str) -> StrategyEvidenceReview: ...

    def transition_review(
        self,
        review: StrategyEvidenceReview,
        *,
        state: StrategyEvidenceReviewState,
        reason_codes: tuple[str, ...],
        operator_reason: str,
        operator_note: str,
        idempotency_key: str,
        now: datetime,
        trial_limits: PaperTrialLimits | None = None,
    ) -> tuple[StrategyEvidenceReview, PaperTrialAuthorization | None]: ...

    def expire_reviews(self, *, now: datetime) -> tuple[StrategyEvidenceReview, ...]: ...

    def trial(self, authorization_id: str) -> PaperTrialAuthorization: ...

    def transition_trial(
        self,
        trial: PaperTrialAuthorization,
        *,
        state: PaperTrialAuthorizationState,
        reason: str,
        idempotency_key: str,
        now: datetime,
    ) -> PaperTrialAuthorization: ...

    def reconcile_trial(
        self, authorization_id: str, *, now: datetime
    ) -> PaperTrialAuthorization: ...


class RequestStrategyEvidenceReview:
    def __init__(self, store: EvidenceReviewWorkflowStore) -> None:
        self._store = store

    def request(
        self, command: RequestStrategyEvidenceReviewCommand, *, now: datetime
    ) -> StrategyEvidenceReview:
        snapshot = self._store.snapshot(command.snapshot_id)
        if (
            snapshot.strategy_id != command.strategy_id
            or snapshot.strategy_version != command.strategy_version
        ):
            raise EvidenceReviewError("review strategy must exactly match its frozen snapshot")
        return self._store.request_review(
            command, now=_aware(now, label="review request timestamp")
        )


class StartStrategyEvidenceReview:
    def __init__(self, store: EvidenceReviewWorkflowStore) -> None:
        self._store = store

    def start(
        self,
        *,
        review_id: str,
        operator_reason: str,
        operator_note: str,
        idempotency_key: str,
        now: datetime,
    ) -> StrategyEvidenceReview:
        review = self._store.review(review_id)
        if review.state is not StrategyEvidenceReviewState.REQUESTED:
            raise EvidenceReviewError("only requested evidence reviews may be started")
        updated, _ = self._store.transition_review(
            review,
            state=StrategyEvidenceReviewState.IN_REVIEW,
            reason_codes=("REVIEW_STARTED",),
            operator_reason=operator_reason,
            operator_note=operator_note,
            idempotency_key=idempotency_key,
            now=_aware(now, label="review start timestamp"),
        )
        return updated


class DecideStrategyEvidenceReview:
    def __init__(self, store: EvidenceReviewWorkflowStore) -> None:
        self._store = store

    def decide(
        self,
        command: DecideStrategyEvidenceReviewCommand,
        *,
        now: datetime,
        trial_limits: PaperTrialLimits | None = None,
    ) -> tuple[StrategyEvidenceReview, PaperTrialAuthorization | None]:
        review = self._store.review(command.review_id)
        moment = _aware(now, label="review decision timestamp")
        if review.state is not StrategyEvidenceReviewState.IN_REVIEW:
            raise EvidenceReviewError("only in-review evidence reviews may be decided")
        if review.expires_at <= moment:
            raise EvidenceReviewError("expired evidence review cannot be decided")
        if command.decision is StrategyEvidenceReviewState.REJECTED:
            return self._store.transition_review(
                review,
                state=StrategyEvidenceReviewState.REJECTED,
                reason_codes=("OPERATOR_REJECTED",),
                operator_reason=command.operator_reason,
                operator_note=command.operator_note,
                idempotency_key=command.idempotency_key,
                now=moment,
            )
        snapshot = self._store.snapshot(review.snapshot_id)
        policy = self._store.policy(snapshot.policy_id, snapshot.policy_version)
        descriptor = self._store.descriptor(review.strategy_id)
        reasons = list(hard_gate_reasons(self._store.criteria(snapshot.snapshot_id)))
        if descriptor.authority_state is not StrategyAuthorityState.SHADOW:
            reasons.append("STRATEGY_NOT_SHADOW")
        if descriptor.implementation_state not in (
            StrategyImplementationState.IMPLEMENTED,
            StrategyImplementationState.FIXTURE,
        ):
            reasons.append("STRATEGY_IMPLEMENTATION_UNAVAILABLE")
        if not policy.is_current_at(moment) or not policy.supports(descriptor):
            reasons.append("POLICY_NOT_CURRENT_OR_COMPATIBLE")
        if moment - snapshot.cutoff_at > policy.maximum_evidence_age:
            reasons.append("SNAPSHOT_STALE")
        expected = paper_trial_confirmation_text(
            strategy_id=review.strategy_id,
            campaign_id=review.requested_campaign_id,
            snapshot_id=review.snapshot_id,
        )
        if command.confirmation_text != expected:
            reasons.append("PAPER_TRIAL_CONFIRMATION_REQUIRED")
        if trial_limits is None:
            reasons.append("PAPER_TRIAL_LIMITS_REQUIRED")
        if reasons:
            return self._store.transition_review(
                review,
                state=StrategyEvidenceReviewState.BLOCKED,
                reason_codes=tuple(sorted(set(reasons))),
                operator_reason=command.operator_reason,
                operator_note=command.operator_note,
                idempotency_key=command.idempotency_key,
                now=moment,
            )
        return self._store.transition_review(
            review,
            state=StrategyEvidenceReviewState.APPROVED,
            reason_codes=("ALL_HARD_GATES_PASS", "PAPER_TRIAL_AUTHORIZED"),
            operator_reason=command.operator_reason,
            operator_note=command.operator_note,
            idempotency_key=command.idempotency_key,
            now=moment,
            trial_limits=trial_limits,
        )


class DeferStrategyEvidenceReview:
    def __init__(self, store: EvidenceReviewWorkflowStore) -> None:
        self._store = store

    def defer(
        self, *, review_id: str, reason: str, note: str, idempotency_key: str, now: datetime
    ) -> StrategyEvidenceReview:
        review = self._store.review(review_id)
        updated, _ = self._store.transition_review(
            review,
            state=StrategyEvidenceReviewState.DEFERRED,
            reason_codes=("OPERATOR_DEFERRED",),
            operator_reason=reason,
            operator_note=note,
            idempotency_key=idempotency_key,
            now=_aware(now, label="review deferral timestamp"),
        )
        return updated


class CancelStrategyEvidenceReview:
    def __init__(self, store: EvidenceReviewWorkflowStore) -> None:
        self._store = store

    def cancel(
        self, *, review_id: str, reason: str, note: str, idempotency_key: str, now: datetime
    ) -> StrategyEvidenceReview:
        review = self._store.review(review_id)
        updated, _ = self._store.transition_review(
            review,
            state=StrategyEvidenceReviewState.CANCELED,
            reason_codes=("OPERATOR_CANCELED",),
            operator_reason=reason,
            operator_note=note,
            idempotency_key=idempotency_key,
            now=_aware(now, label="review cancellation timestamp"),
        )
        return updated


class ExpireStrategyEvidenceReviews:
    def __init__(self, store: EvidenceReviewWorkflowStore) -> None:
        self._store = store

    def expire(self, *, now: datetime) -> tuple[StrategyEvidenceReview, ...]:
        return self._store.expire_reviews(now=_aware(now, label="review expiry timestamp"))


class _TrialAction:
    _state: PaperTrialAuthorizationState

    def __init__(self, store: EvidenceReviewWorkflowStore) -> None:
        self._store = store

    def apply(
        self, *, authorization_id: str, reason: str, idempotency_key: str, now: datetime
    ) -> PaperTrialAuthorization:
        trial = self._store.trial(authorization_id)
        return self._store.transition_trial(
            trial,
            state=self._state,
            reason=reason,
            idempotency_key=idempotency_key,
            now=_aware(now, label="paper trial action timestamp"),
        )


class PausePaperTrialAuthorization(_TrialAction):
    _state = PaperTrialAuthorizationState.PAUSED


class ResumePaperTrialAuthorization(_TrialAction):
    _state = PaperTrialAuthorizationState.ACTIVE


class RevokePaperTrialAuthorization(_TrialAction):
    _state = PaperTrialAuthorizationState.REVOKED


class CompletePaperTrialAuthorization(_TrialAction):
    _state = PaperTrialAuthorizationState.COMPLETED


class ReconcilePaperTrialLimits:
    def __init__(self, store: EvidenceReviewWorkflowStore) -> None:
        self._store = store

    def reconcile(self, *, authorization_id: str, now: datetime) -> PaperTrialAuthorization:
        return self._store.reconcile_trial(
            authorization_id, now=_aware(now, label="trial reconciliation timestamp")
        )


class CreatePaperTrialAuthorization:
    """Named service retained for explicit application composition in adapters/UI."""

    def __init__(self, store: EvidenceReviewWorkflowStore) -> None:
        self._store = store

    def create_after_approval(
        self,
        review: StrategyEvidenceReview,
        *,
        limits: PaperTrialLimits,
        reason: str,
        note: str,
        idempotency_key: str,
        now: datetime,
    ) -> PaperTrialAuthorization:
        updated, trial = self._store.transition_review(
            review,
            state=StrategyEvidenceReviewState.APPROVED,
            reason_codes=("ALL_HARD_GATES_PASS", "PAPER_TRIAL_AUTHORIZED"),
            operator_reason=reason,
            operator_note=note,
            idempotency_key=idempotency_key,
            now=_aware(now, label="paper trial authorization timestamp"),
            trial_limits=limits,
        )
        del updated
        if trial is None:
            raise EvidenceReviewError("approved review did not create a paper trial")
        return trial


class InMemoryEvidenceReviewStore:
    """A deterministic implementation used by focused policy and workflow tests."""

    def __init__(
        self,
        *,
        policies: Iterable[StrategyEvidenceReviewPolicy] = (),
        sources: Iterable[EvidenceReviewSource] = (),
    ) -> None:
        self._policies = {item.identity: item for item in policies}
        self._sources = {
            (item.descriptor.strategy_id, item.descriptor.version): item for item in sources
        }
        self._snapshots: dict[str, StrategyEvidenceSnapshot] = {}
        self._criteria: dict[str, tuple[EvidenceCriterionResult, ...]] = {}
        self._snapshot_idempotency: dict[str, str] = {}
        self._reviews: dict[str, StrategyEvidenceReview] = {}
        self._review_idempotency: dict[tuple[str, str], str] = {}
        self._trials: dict[str, PaperTrialAuthorization] = {}
        self._trial_actions: dict[tuple[str, str], str] = {}

    def policy(self, policy_id: str, policy_version: str) -> StrategyEvidenceReviewPolicy:
        try:
            return self._policies[(policy_id, policy_version)]
        except KeyError as error:
            raise EvidenceReviewError("review policy was not found") from error

    def descriptor(self, strategy_id: str) -> StrategyDescriptor:
        rows = [
            source.descriptor for (item, _), source in self._sources.items() if item == strategy_id
        ]
        if len(rows) != 1:
            raise EvidenceReviewError("governed strategy was not found")
        return rows[0]

    def evidence_source(
        self, *, strategy_id: str, strategy_version: str, cutoff_at: datetime
    ) -> EvidenceReviewSource:
        try:
            return self._sources[(strategy_id, strategy_version)]
        except KeyError as error:
            raise EvidenceReviewError("retained strategy evidence was not found") from error

    def save_snapshot(
        self,
        snapshot: StrategyEvidenceSnapshot,
        criteria: tuple[EvidenceCriterionResult, ...],
        *,
        idempotency_key: str,
    ) -> tuple[StrategyEvidenceSnapshot, tuple[EvidenceCriterionResult, ...]]:
        existing = self._snapshot_idempotency.get(idempotency_key)
        if existing is not None:
            retained = self._snapshots[existing]
            if retained.canonical_input_hash != snapshot.canonical_input_hash:
                raise EvidenceReviewConflictError(
                    "snapshot idempotency key has conflicting content"
                )
            return retained, self._criteria[existing]
        self._snapshots[snapshot.snapshot_id] = snapshot
        self._criteria[snapshot.snapshot_id] = criteria
        self._snapshot_idempotency[idempotency_key] = snapshot.snapshot_id
        return snapshot, criteria

    def snapshot(self, snapshot_id: str) -> StrategyEvidenceSnapshot:
        try:
            return self._snapshots[snapshot_id]
        except KeyError as error:
            raise EvidenceReviewError("evidence snapshot was not found") from error

    def criteria(self, snapshot_id: str) -> tuple[EvidenceCriterionResult, ...]:
        self.snapshot(snapshot_id)
        return self._criteria[snapshot_id]

    def request_review(
        self, command: RequestStrategyEvidenceReviewCommand, *, now: datetime
    ) -> StrategyEvidenceReview:
        key = (command.strategy_id, command.idempotency_key)
        existing_id = self._review_idempotency.get(key)
        if existing_id is not None:
            existing = self._reviews[existing_id]
            if existing.canonical_request_hash != command.request_hash:
                raise EvidenceReviewConflictError("review idempotency key has conflicting content")
            return existing
        review = StrategyEvidenceReview(
            review_id=deterministic_id(
                "strategy_evidence_review", {"command": command.request_hash.value}
            ),
            strategy_id=command.strategy_id,
            strategy_version=command.strategy_version,
            snapshot_id=command.snapshot_id,
            requested_campaign_id=command.campaign_id,
            state=StrategyEvidenceReviewState.REQUESTED,
            requested_at=now,
            review_started_at=None,
            decided_at=None,
            expires_at=_aware(command.expires_at, label="review expiry"),
            requested_authority=StrategyAuthorityState.PAPER,
            request_action_id=deterministic_id(
                "operator_action", {"review": command.request_hash.value}
            ),
            decision_action_id=None,
            operator_reason=_text(command.operator_reason, label="operator reason", maximum=500),
            reviewer_note=command.operator_note or None,
            idempotency_key=command.idempotency_key,
            canonical_request_hash=command.request_hash,
            canonical_result_hash=None,
            reason_codes=("REVIEW_REQUESTED",),
        )
        self._reviews[review.review_id] = review
        self._review_idempotency[key] = review.review_id
        return review

    def review(self, review_id: str) -> StrategyEvidenceReview:
        try:
            return self._reviews[review_id]
        except KeyError as error:
            raise EvidenceReviewError("strategy evidence review was not found") from error

    def transition_review(
        self,
        review: StrategyEvidenceReview,
        *,
        state: StrategyEvidenceReviewState,
        reason_codes: tuple[str, ...],
        operator_reason: str,
        operator_note: str,
        idempotency_key: str,
        now: datetime,
        trial_limits: PaperTrialLimits | None = None,
    ) -> tuple[StrategyEvidenceReview, PaperTrialAuthorization | None]:
        action_key = (review.review_id, idempotency_key)
        existing = self._trial_actions.get(action_key)
        if existing is not None:
            retained = self._reviews[review.review_id]
            return retained, self._trials.get(existing)
        allowed: dict[StrategyEvidenceReviewState, tuple[StrategyEvidenceReviewState, ...]] = {
            StrategyEvidenceReviewState.REQUESTED: (
                StrategyEvidenceReviewState.IN_REVIEW,
                StrategyEvidenceReviewState.CANCELED,
                StrategyEvidenceReviewState.EXPIRED,
            ),
            StrategyEvidenceReviewState.IN_REVIEW: (
                StrategyEvidenceReviewState.BLOCKED,
                StrategyEvidenceReviewState.APPROVED,
                StrategyEvidenceReviewState.REJECTED,
                StrategyEvidenceReviewState.DEFERRED,
                StrategyEvidenceReviewState.CANCELED,
                StrategyEvidenceReviewState.EXPIRED,
            ),
        }
        if state not in allowed.get(review.state, ()):
            raise EvidenceReviewError("evidence review state transition is invalid")
        result_hash = ContentHash(
            content_digest({"state": state.value, "reasons": reason_codes, "at": now})
        )
        updated = replace(
            review,
            state=state,
            review_started_at=now
            if state is StrategyEvidenceReviewState.IN_REVIEW
            else review.review_started_at,
            decided_at=now
            if state not in (StrategyEvidenceReviewState.IN_REVIEW,)
            else review.decided_at,
            decision_action_id=deterministic_id(
                "operator_action", {"review": review.review_id, "idempotency_key": idempotency_key}
            ),
            operator_reason=_text(operator_reason, label="operator reason", maximum=500),
            reviewer_note=operator_note or None,
            canonical_result_hash=result_hash,
            reason_codes=tuple(sorted(set(reason_codes))),
        )
        trial: PaperTrialAuthorization | None = None
        if state is StrategyEvidenceReviewState.APPROVED:
            if trial_limits is None:
                raise EvidenceReviewError("approved review needs paper trial limits")
            source = self.evidence_source(
                strategy_id=review.strategy_id,
                strategy_version=review.strategy_version,
                cutoff_at=now,
            )
            authorization_id = deterministic_id(
                "paper_trial_authorization",
                {"review_id": review.review_id, "limits": trial_limits},
            )
            initial_state = (
                PaperTrialAuthorizationState.ACTIVE
                if trial_limits.starts_at <= now < trial_limits.expires_at
                else PaperTrialAuthorizationState.SCHEDULED
            )
            trial_payload = {
                "review": review.review_id,
                "limits": trial_limits,
                "state": initial_state.value,
            }
            trial = PaperTrialAuthorization(
                authorization_id=authorization_id,
                review_id=review.review_id,
                strategy_id=review.strategy_id,
                strategy_version=review.strategy_version,
                campaign_id=review.requested_campaign_id,
                strategy_configuration_hash=source.descriptor.configuration_hash,
                limits=trial_limits,
                state=initial_state,
                authorized_at=now,
                suspension_reason=None,
                revocation_reason=None,
                operator_action_id=updated.decision_action_id or "",
                configuration_hash=ContentHash(content_digest({"limits": trial_limits})),
                canonical_input_hash=ContentHash(content_digest(trial_payload)),
                canonical_result_hash=ContentHash(content_digest(trial_payload)),
                usage=PaperTrialUsage(),
            )
            self._trials[trial.authorization_id] = trial
        self._reviews[review.review_id] = updated
        self._trial_actions[action_key] = trial.authorization_id if trial is not None else ""
        return updated, trial

    def expire_reviews(self, *, now: datetime) -> tuple[StrategyEvidenceReview, ...]:
        expired: list[StrategyEvidenceReview] = []
        for review in tuple(self._reviews.values()):
            if (
                review.state
                in (StrategyEvidenceReviewState.REQUESTED, StrategyEvidenceReviewState.IN_REVIEW)
                and review.expires_at <= now
            ):
                updated, _ = self.transition_review(
                    review,
                    state=StrategyEvidenceReviewState.EXPIRED,
                    reason_codes=("REVIEW_EXPIRED",),
                    operator_reason="review expired",
                    operator_note="",
                    idempotency_key=f"expire:{review.review_id}",
                    now=now,
                )
                expired.append(updated)
        return tuple(expired)

    def trial(self, authorization_id: str) -> PaperTrialAuthorization:
        try:
            return self._trials[authorization_id]
        except KeyError as error:
            raise EvidenceReviewError("paper trial authorization was not found") from error

    def transition_trial(
        self,
        trial: PaperTrialAuthorization,
        *,
        state: PaperTrialAuthorizationState,
        reason: str,
        idempotency_key: str,
        now: datetime,
    ) -> PaperTrialAuthorization:
        key = (trial.authorization_id, idempotency_key)
        existing = self._trial_actions.get(key)
        if existing is not None:
            return self.trial(trial.authorization_id)
        valid = {
            PaperTrialAuthorizationState.ACTIVE: (
                PaperTrialAuthorizationState.PAUSED,
                PaperTrialAuthorizationState.SUSPENDED_LIMIT,
                PaperTrialAuthorizationState.COMPLETED,
                PaperTrialAuthorizationState.REVOKED,
            ),
            PaperTrialAuthorizationState.SCHEDULED: (
                PaperTrialAuthorizationState.ACTIVE,
                PaperTrialAuthorizationState.REVOKED,
            ),
            PaperTrialAuthorizationState.PAUSED: (
                PaperTrialAuthorizationState.ACTIVE,
                PaperTrialAuthorizationState.REVOKED,
                PaperTrialAuthorizationState.COMPLETED,
            ),
            PaperTrialAuthorizationState.SUSPENDED_LIMIT: (
                PaperTrialAuthorizationState.REVOKED,
                PaperTrialAuthorizationState.COMPLETED,
            ),
        }
        if state not in valid.get(trial.state, ()):
            raise EvidenceReviewError("paper trial state transition is invalid")
        updated = replace(
            trial,
            state=state,
            suspension_reason=reason
            if state is PaperTrialAuthorizationState.SUSPENDED_LIMIT
            else trial.suspension_reason,
            revocation_reason=reason
            if state is PaperTrialAuthorizationState.REVOKED
            else trial.revocation_reason,
            canonical_result_hash=ContentHash(
                content_digest(
                    {
                        "prior": trial.canonical_result_hash.value,
                        "state": state.value,
                        "reason": reason,
                        "at": now,
                    }
                )
            ),
        )
        self._trials[trial.authorization_id] = updated
        self._trial_actions[key] = ""
        return updated

    def reconcile_trial(self, authorization_id: str, *, now: datetime) -> PaperTrialAuthorization:
        trial = self.trial(authorization_id)
        if trial.state is PaperTrialAuthorizationState.ACTIVE and now >= trial.limits.expires_at:
            return self.transition_trial(
                trial,
                state=PaperTrialAuthorizationState.COMPLETED,
                reason="TRIAL_EXPIRED",
                idempotency_key=f"expire:{authorization_id}",
                now=now,
            )
        return trial
