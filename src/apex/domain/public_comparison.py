"""Pure temporal comparison of isolated public-market observations.

The comparison layer is deliberately evidence-only.  It never estimates value,
creates an order, or treats a repeated structural candidate as executable.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from apex.domain.public_event import ConsistencyEvaluation, ConsistencyEvaluationState
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash, ValueValidationError, content_digest, parse_decimal


class ScanComparisonState(StrEnum):
    COMPLETED = "COMPLETED"
    INVALID = "INVALID"


class MarketSnapshotDeltaState(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    UNCHANGED = "UNCHANGED"
    CHANGED = "CHANGED"
    INVALID = "INVALID"


class EventConsistencyTransitionState(StrEnum):
    NEW_CANDIDATE = "NEW_CANDIDATE"
    PERSISTING_CANDIDATE = "PERSISTING_CANDIDATE"
    RESOLVED_CANDIDATE = "RESOLVED_CANDIDATE"
    BECAME_INELIGIBLE = "BECAME_INELIGIBLE"
    BECAME_INVALID = "BECAME_INVALID"
    REMAINED_CONSISTENT = "REMAINED_CONSISTENT"
    REMAINED_INELIGIBLE = "REMAINED_INELIGIBLE"
    REMAINED_INVALID = "REMAINED_INVALID"
    ADDED_EVENT = "ADDED_EVENT"
    REMOVED_EVENT = "REMOVED_EVENT"
    CHANGED = "CHANGED"


class EvidenceCompatibilityState(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    CHANGED = "CHANGED"
    UNKNOWN = "UNKNOWN"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _delta(current: Decimal | None, baseline: Decimal | None) -> Decimal | None:
    """Return an exact delta only when both observed values exist."""

    return current - baseline if current is not None and baseline is not None else None


def _canonical_decimal(value: Decimal | None) -> Decimal | None:
    """Strip PostgreSQL's fixed display scale before hashing immutable evidence."""

    if value is None:
        return None
    return parse_decimal(format(value.normalize(), "f"), allow_negative=True)


def _spread(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    return ask - bid if bid is not None and ask is not None else None


def _compatibility(
    baseline: ContentHash | None, current: ContentHash | None
) -> EvidenceCompatibilityState:
    if baseline is None or current is None:
        return EvidenceCompatibilityState.UNKNOWN
    return (
        EvidenceCompatibilityState.COMPATIBLE
        if baseline == current
        else EvidenceCompatibilityState.CHANGED
    )


def _duration_microseconds(value: timedelta | None) -> int | None:
    if value is None:
        return None
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


@dataclass(frozen=True, slots=True)
class MarketScanObservation:
    """The exact comparable subset of one normalized market snapshot."""

    snapshot_id: str
    provider: PublicMarketProvider
    provider_market_ticker: str
    observed_at: datetime
    status: str | None
    rule_content_hash: ContentHash | None
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    yes_bid_size: Decimal | None
    yes_ask_size: Decimal | None
    volume: Decimal | None
    open_interest: Decimal | None

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.provider_market_ticker:
            raise ValueValidationError("market scan observation identities must not be empty")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        for name in (
            "yes_bid",
            "yes_ask",
            "no_bid",
            "no_ask",
            "yes_bid_size",
            "yes_ask_size",
            "volume",
            "open_interest",
        ):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class EventScanObservation:
    """An exact event snapshot and its already-persisted consistency evaluation."""

    event_snapshot_id: str
    provider: PublicMarketProvider
    provider_event_ticker: str
    observed_at: datetime
    settlement_source_hash: ContentHash
    relationship_evidence_hash: ContentHash | None
    evaluation: ConsistencyEvaluation

    def __post_init__(self) -> None:
        if not self.event_snapshot_id or not self.provider_event_ticker:
            raise ValueValidationError("event scan observation identities must not be empty")
        if self.evaluation.event_snapshot_id != self.event_snapshot_id:
            raise ValueValidationError("event observation must retain its exact evaluation")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))


@dataclass(frozen=True, slots=True)
class PersistedPublicScan:
    """A completed market-ingestion run with bounded related event evidence."""

    ingestion_run_id: str
    provider: PublicMarketProvider
    state: str
    observed_start_at: datetime
    observed_end_at: datetime
    markets: tuple[MarketScanObservation, ...]
    events: tuple[EventScanObservation, ...]

    def __post_init__(self) -> None:
        if not self.ingestion_run_id:
            raise ValueValidationError("public scan run identity must not be empty")
        start = _aware(self.observed_start_at, "observed_start_at")
        end = _aware(self.observed_end_at, "observed_end_at")
        if end < start:
            raise ValueValidationError("public scan observation range is reversed")
        object.__setattr__(self, "observed_start_at", start)
        object.__setattr__(self, "observed_end_at", end)


@dataclass(frozen=True, slots=True)
class MarketSnapshotDelta:
    delta_id: str
    comparison_id: str
    provider: PublicMarketProvider
    provider_market_ticker: str
    baseline_snapshot_id: str | None
    current_snapshot_id: str | None
    state: MarketSnapshotDeltaState
    baseline_observed_at: datetime | None
    current_observed_at: datetime | None
    yes_bid_delta: Decimal | None
    yes_ask_delta: Decimal | None
    no_bid_delta: Decimal | None
    no_ask_delta: Decimal | None
    yes_spread_delta: Decimal | None
    no_spread_delta: Decimal | None
    yes_bid_size_delta: Decimal | None
    yes_ask_size_delta: Decimal | None
    volume_delta: Decimal | None
    open_interest_delta: Decimal | None
    rule_compatibility: EvidenceCompatibilityState
    lifecycle_status_transition: str
    reason_codes: tuple[str, ...]
    canonical_input_hash: ContentHash

    def __post_init__(self) -> None:
        if not self.delta_id or not self.comparison_id or not self.provider_market_ticker:
            raise ValueValidationError("market delta identities must not be empty")
        if self.baseline_observed_at is not None:
            object.__setattr__(
                self,
                "baseline_observed_at",
                _aware(self.baseline_observed_at, "baseline_observed_at"),
            )
        if self.current_observed_at is not None:
            object.__setattr__(
                self,
                "current_observed_at",
                _aware(self.current_observed_at, "current_observed_at"),
            )
        for name in (
            "yes_bid_delta",
            "yes_ask_delta",
            "no_bid_delta",
            "no_ask_delta",
            "yes_spread_delta",
            "no_spread_delta",
            "yes_bid_size_delta",
            "yes_ask_size_delta",
            "volume_delta",
            "open_interest_delta",
        ):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class EventConsistencyTransition:
    transition_id: str
    comparison_id: str
    provider: PublicMarketProvider
    provider_event_ticker: str
    baseline_event_snapshot_id: str | None
    current_event_snapshot_id: str | None
    baseline_evaluation_id: str | None
    current_evaluation_id: str | None
    baseline_state: ConsistencyEvaluationState | None
    current_state: ConsistencyEvaluationState | None
    state: EventConsistencyTransitionState
    baseline_yes_ask_sum: Decimal | None
    current_yes_ask_sum: Decimal | None
    consistency_deviation_delta: Decimal | None
    baseline_quote_coverage_count: int | None
    current_quote_coverage_count: int | None
    baseline_minimum_visible_size: Decimal | None
    current_minimum_visible_size: Decimal | None
    rule_compatibility: EvidenceCompatibilityState
    settlement_source_compatibility: EvidenceCompatibilityState
    exhaustiveness_transition: str
    observation_interval: timedelta | None
    reason_codes: tuple[str, ...]
    canonical_input_hash: ContentHash

    def __post_init__(self) -> None:
        if not self.transition_id or not self.comparison_id or not self.provider_event_ticker:
            raise ValueValidationError("event transition identities must not be empty")
        if self.observation_interval is not None and self.observation_interval.total_seconds() < 0:
            raise ValueValidationError("event transition interval must not be negative")
        for name in (
            "baseline_yes_ask_sum",
            "current_yes_ask_sum",
            "consistency_deviation_delta",
            "baseline_minimum_visible_size",
            "current_minimum_visible_size",
        ):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))

    @property
    def canonical_hash(self) -> ContentHash:
        payload = {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name != "observation_interval"
        }
        payload["observation_interval_microseconds"] = _duration_microseconds(
            self.observation_interval
        )
        return ContentHash(content_digest(payload))


@dataclass(frozen=True, slots=True)
class ScanComparison:
    comparison_id: str
    provider: PublicMarketProvider
    baseline_ingestion_run_id: str
    current_ingestion_run_id: str
    comparison_version: str
    baseline_observed_start_at: datetime
    baseline_observed_end_at: datetime
    current_observed_start_at: datetime
    current_observed_end_at: datetime
    compared_at: datetime
    state: ScanComparisonState
    matched_market_count: int
    added_market_count: int
    removed_market_count: int
    changed_market_count: int
    matched_event_count: int
    candidate_transition_counts: tuple[tuple[str, int], ...]
    canonical_configuration_hash: ContentHash
    canonical_input_hash: ContentHash

    def __post_init__(self) -> None:
        if (
            not self.comparison_id
            or not self.baseline_ingestion_run_id
            or not self.current_ingestion_run_id
        ):
            raise ValueValidationError("scan comparison identities must not be empty")
        if self.baseline_ingestion_run_id == self.current_ingestion_run_id:
            raise ValueValidationError("scan comparison runs must be distinct")
        for name in (
            "baseline_observed_start_at",
            "baseline_observed_end_at",
            "current_observed_start_at",
            "current_observed_end_at",
            "compared_at",
        ):
            object.__setattr__(self, name, _aware(getattr(self, name), name))
        if self.baseline_observed_end_at >= self.current_observed_start_at:
            raise ValueValidationError("scan comparison observations must not overlap or reverse")
        if (
            min(
                self.matched_market_count,
                self.added_market_count,
                self.removed_market_count,
                self.changed_market_count,
                self.matched_event_count,
            )
            < 0
        ):
            raise ValueValidationError("scan comparison counts must not be negative")
        object.__setattr__(
            self,
            "candidate_transition_counts",
            tuple(sorted(self.candidate_transition_counts)),
        )

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


def compare_market_observations(
    *,
    comparison_id: str,
    baseline: MarketScanObservation | None,
    current: MarketScanObservation | None,
    provider_market_ticker: str,
) -> MarketSnapshotDelta:
    """Build one immutable exact-ticker delta without filling absent values with zero."""

    if baseline is None and current is None:
        raise ValueValidationError("a market delta requires at least one observation")
    provider = (current or baseline).provider  # type: ignore[union-attr]
    if baseline is not None and current is not None and baseline.provider != current.provider:
        raise ValueValidationError("market delta provider identities must match")
    reasons: list[str] = []
    if baseline is None:
        state = MarketSnapshotDeltaState.ADDED
        rule_compatibility = EvidenceCompatibilityState.UNKNOWN
        reasons.append("MARKET_ADDED")
    elif current is None:
        state = MarketSnapshotDeltaState.REMOVED
        rule_compatibility = EvidenceCompatibilityState.UNKNOWN
        reasons.append("MARKET_REMOVED")
    else:
        rule_compatibility = _compatibility(baseline.rule_content_hash, current.rule_content_hash)
        if rule_compatibility is EvidenceCompatibilityState.CHANGED:
            reasons.append("RULE_HASH_CHANGED")
        elif rule_compatibility is EvidenceCompatibilityState.UNKNOWN:
            reasons.append("RULE_HASH_UNKNOWN")
        changed = (
            baseline.status != current.status
            or baseline.rule_content_hash != current.rule_content_hash
            or any(
                getattr(baseline, name) != getattr(current, name)
                for name in (
                    "yes_bid",
                    "yes_ask",
                    "no_bid",
                    "no_ask",
                    "yes_bid_size",
                    "yes_ask_size",
                    "volume",
                    "open_interest",
                )
            )
        )
        state = MarketSnapshotDeltaState.CHANGED if changed else MarketSnapshotDeltaState.UNCHANGED
    comparable = rule_compatibility is EvidenceCompatibilityState.COMPATIBLE
    if baseline is not None and current is not None and not comparable:
        reasons.append("QUOTE_DELTA_NOT_COMPARABLE")

    def comparable_delta(name: str) -> Decimal | None:
        if not comparable or baseline is None or current is None:
            return None
        return _delta(getattr(current, name), getattr(baseline, name))

    volume_delta = comparable_delta("volume")
    if volume_delta is not None and volume_delta < 0:
        volume_delta = None
        reasons.append("VOLUME_DELTA_NOT_SEMANTICALLY_VALID")
    if baseline is not None and current is not None:
        for name in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
            if getattr(baseline, name) is None or getattr(current, name) is None:
                reasons.append(f"{name.upper()}_ABSENT")
        status_transition = f"{baseline.status or 'ABSENT'}->{current.status or 'ABSENT'}"
    elif baseline is None:
        status_transition = f"ABSENT->{current.status or 'ABSENT'}"  # type: ignore[union-attr]
    else:
        status_transition = f"{baseline.status or 'ABSENT'}->ABSENT"
    payload = {
        "comparison_id": comparison_id,
        "baseline": baseline,
        "current": current,
        "ticker": provider_market_ticker,
        "state": state,
        "rule_compatibility": rule_compatibility,
    }
    return MarketSnapshotDelta(
        delta_id=f"market_snapshot_delta_{content_digest(payload)[:24]}",
        comparison_id=comparison_id,
        provider=provider,
        provider_market_ticker=provider_market_ticker,
        baseline_snapshot_id=baseline.snapshot_id if baseline else None,
        current_snapshot_id=current.snapshot_id if current else None,
        state=state,
        baseline_observed_at=baseline.observed_at if baseline else None,
        current_observed_at=current.observed_at if current else None,
        yes_bid_delta=comparable_delta("yes_bid"),
        yes_ask_delta=comparable_delta("yes_ask"),
        no_bid_delta=comparable_delta("no_bid"),
        no_ask_delta=comparable_delta("no_ask"),
        yes_spread_delta=(
            _delta(
                _spread(current.yes_bid, current.yes_ask),
                _spread(baseline.yes_bid, baseline.yes_ask),
            )
            if comparable and baseline is not None and current is not None
            else None
        ),
        no_spread_delta=(
            _delta(
                _spread(current.no_bid, current.no_ask), _spread(baseline.no_bid, baseline.no_ask)
            )
            if comparable and baseline is not None and current is not None
            else None
        ),
        yes_bid_size_delta=comparable_delta("yes_bid_size"),
        yes_ask_size_delta=comparable_delta("yes_ask_size"),
        volume_delta=volume_delta,
        open_interest_delta=comparable_delta("open_interest"),
        rule_compatibility=rule_compatibility,
        lifecycle_status_transition=status_transition,
        reason_codes=tuple(reasons),
        canonical_input_hash=ContentHash(content_digest(payload)),
    )


def compare_event_observations(
    *,
    comparison_id: str,
    baseline: EventScanObservation | None,
    current: EventScanObservation | None,
    provider_event_ticker: str,
) -> EventConsistencyTransition:
    """Classify an exact-ticker event transition and candidate persistence."""

    if baseline is None and current is None:
        raise ValueValidationError("an event transition requires at least one observation")
    provider = (current or baseline).provider  # type: ignore[union-attr]
    reasons: list[str] = []
    if baseline is not None and current is not None:
        rule_compatibility = _compatibility(
            baseline.relationship_evidence_hash, current.relationship_evidence_hash
        )
        source_compatibility = _compatibility(
            baseline.settlement_source_hash, current.settlement_source_hash
        )
        if rule_compatibility is not EvidenceCompatibilityState.COMPATIBLE:
            reasons.append("RULE_EVIDENCE_NOT_COMPATIBLE")
        if source_compatibility is not EvidenceCompatibilityState.COMPATIBLE:
            reasons.append("SETTLEMENT_SOURCE_NOT_COMPATIBLE")
    else:
        rule_compatibility = EvidenceCompatibilityState.UNKNOWN
        source_compatibility = EvidenceCompatibilityState.UNKNOWN
    base_state = baseline.evaluation.state if baseline else None
    current_state = current.evaluation.state if current else None
    if baseline is None:
        state = (
            EventConsistencyTransitionState.NEW_CANDIDATE
            if current_state is ConsistencyEvaluationState.CANDIDATE
            else EventConsistencyTransitionState.ADDED_EVENT
        )
        reasons.append("EVENT_ADDED")
    elif current is None:
        state = (
            EventConsistencyTransitionState.RESOLVED_CANDIDATE
            if base_state is ConsistencyEvaluationState.CANDIDATE
            else EventConsistencyTransitionState.REMOVED_EVENT
        )
        reasons.append("EVENT_REMOVED")
    elif current_state is ConsistencyEvaluationState.CANDIDATE:
        complete = (
            baseline.evaluation.missing_quote_count == 0
            and current.evaluation.missing_quote_count == 0
            and baseline.evaluation.yes_ask_sum is not None
            and current.evaluation.yes_ask_sum is not None
        )
        persistent = (
            base_state is ConsistencyEvaluationState.CANDIDATE
            and complete
            and rule_compatibility is EvidenceCompatibilityState.COMPATIBLE
            and source_compatibility is EvidenceCompatibilityState.COMPATIBLE
            and current.observed_at > baseline.observed_at
        )
        state = (
            EventConsistencyTransitionState.PERSISTING_CANDIDATE
            if persistent
            else EventConsistencyTransitionState.NEW_CANDIDATE
        )
        if persistent:
            reasons.append("REPEATED_COMPATIBLE_CANDIDATE")
        else:
            reasons.append("CURRENT_CANDIDATE_NOT_PERSISTENT")
    elif base_state is ConsistencyEvaluationState.CANDIDATE:
        if current_state is ConsistencyEvaluationState.INVALID:
            state = EventConsistencyTransitionState.BECAME_INVALID
        elif current_state is ConsistencyEvaluationState.INELIGIBLE:
            state = EventConsistencyTransitionState.BECAME_INELIGIBLE
        else:
            state = EventConsistencyTransitionState.RESOLVED_CANDIDATE
    elif current_state is ConsistencyEvaluationState.INVALID:
        state = (
            EventConsistencyTransitionState.REMAINED_INVALID
            if base_state is ConsistencyEvaluationState.INVALID
            else EventConsistencyTransitionState.BECAME_INVALID
        )
    elif current_state is ConsistencyEvaluationState.INELIGIBLE:
        state = (
            EventConsistencyTransitionState.REMAINED_INELIGIBLE
            if base_state is ConsistencyEvaluationState.INELIGIBLE
            else EventConsistencyTransitionState.BECAME_INELIGIBLE
        )
    elif (
        base_state is ConsistencyEvaluationState.CONSISTENT
        and current_state is ConsistencyEvaluationState.CONSISTENT
    ):
        state = EventConsistencyTransitionState.REMAINED_CONSISTENT
    else:
        state = EventConsistencyTransitionState.CHANGED
    payload = {
        "comparison_id": comparison_id,
        "baseline": baseline,
        "current": current,
        "ticker": provider_event_ticker,
        "state": state,
    }
    return EventConsistencyTransition(
        transition_id=f"event_consistency_transition_{content_digest(payload)[:24]}",
        comparison_id=comparison_id,
        provider=provider,
        provider_event_ticker=provider_event_ticker,
        baseline_event_snapshot_id=baseline.event_snapshot_id if baseline else None,
        current_event_snapshot_id=current.event_snapshot_id if current else None,
        baseline_evaluation_id=baseline.evaluation.evaluation_id if baseline else None,
        current_evaluation_id=current.evaluation.evaluation_id if current else None,
        baseline_state=base_state,
        current_state=current_state,
        state=state,
        baseline_yes_ask_sum=baseline.evaluation.yes_ask_sum if baseline else None,
        current_yes_ask_sum=current.evaluation.yes_ask_sum if current else None,
        consistency_deviation_delta=(
            _delta(current.evaluation.yes_ask_sum, baseline.evaluation.yes_ask_sum)
            if baseline is not None
            and current is not None
            and rule_compatibility is EvidenceCompatibilityState.COMPATIBLE
            and source_compatibility is EvidenceCompatibilityState.COMPATIBLE
            else None
        ),
        baseline_quote_coverage_count=baseline.evaluation.quote_coverage_count
        if baseline
        else None,
        current_quote_coverage_count=current.evaluation.quote_coverage_count if current else None,
        baseline_minimum_visible_size=(
            baseline.evaluation.minimum_visible_executable_size if baseline else None
        ),
        current_minimum_visible_size=(
            current.evaluation.minimum_visible_executable_size if current else None
        ),
        rule_compatibility=rule_compatibility,
        settlement_source_compatibility=source_compatibility,
        exhaustiveness_transition=(
            f"{baseline.evaluation.exhaustiveness_evidence.value if baseline else 'ABSENT'}"
            f"->{current.evaluation.exhaustiveness_evidence.value if current else 'ABSENT'}"
        ),
        observation_interval=(current.observed_at - baseline.observed_at)
        if baseline is not None and current is not None
        else None,
        reason_codes=tuple(reasons),
        canonical_input_hash=ContentHash(content_digest(payload)),
    )


def rank_shadow_research_priorities(
    transitions: tuple[EventConsistencyTransition, ...],
) -> tuple[EventConsistencyTransition, ...]:
    """Rank only temporal shadow candidates using transparent deterministic fields."""

    candidates = tuple(
        transition
        for transition in transitions
        if transition.state
        in {
            EventConsistencyTransitionState.PERSISTING_CANDIDATE,
            EventConsistencyTransitionState.NEW_CANDIDATE,
        }
    )
    return tuple(
        sorted(
            candidates,
            key=lambda value: (
                0 if value.state is EventConsistencyTransitionState.PERSISTING_CANDIDATE else 1,
                0
                if value.rule_compatibility is EvidenceCompatibilityState.COMPATIBLE
                and value.settlement_source_compatibility is EvidenceCompatibilityState.COMPATIBLE
                else 1,
                -(value.current_quote_coverage_count or 0),
                -(Decimal(1) - (value.current_yes_ask_sum or Decimal(1))),
                -((value.observation_interval or timedelta(0)).total_seconds()),
                -(value.current_minimum_visible_size or Decimal(0)),
                value.provider_event_ticker,
            ),
        )
    )
