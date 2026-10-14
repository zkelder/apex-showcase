"""Pure exact-event history analysis for retained public observation evidence.

This module models repeated observations only.  A streak is not a statement
about unobserved market time, executable value, or trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from apex.domain.public_event import ConsistencyEvaluation, ConsistencyEvaluationState
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash, ValueValidationError, content_digest, parse_decimal


class EventHistoryAnalysisState(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"


class EventHistoryObservationState(StrEnum):
    PRESENT_CANDIDATE = "PRESENT_CANDIDATE"
    PRESENT_CONSISTENT = "PRESENT_CONSISTENT"
    PRESENT_INELIGIBLE = "PRESENT_INELIGIBLE"
    PRESENT_INVALID = "PRESENT_INVALID"
    ABSENT = "ABSENT"


class EventHistoryContinuityState(StrEnum):
    CONTINUOUS = "CONTINUOUS"
    FIRST_OBSERVATION = "FIRST_OBSERVATION"
    EVIDENCE_BREAK = "EVIDENCE_BREAK"
    OBSERVATION_GAP = "OBSERVATION_GAP"
    NOT_APPLICABLE = "NOT_APPLICABLE"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_decimal(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return parse_decimal(format(value.normalize(), "f"), allow_negative=True)


def _duration_microseconds(value: timedelta | None) -> int | None:
    if value is None:
        return None
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


@dataclass(frozen=True, slots=True)
class EventHistoryEvidence:
    """One exact event's immutable evidence within one completed market run."""

    event_snapshot_id: str
    provider: PublicMarketProvider
    provider_event_ticker: str
    observed_at: datetime
    settlement_source_hash: ContentHash
    rule_evidence_hash: ContentHash
    relationship_evidence_hash: ContentHash
    evaluation: ConsistencyEvaluation

    def __post_init__(self) -> None:
        if not self.event_snapshot_id or not self.provider_event_ticker:
            raise ValueValidationError("event history evidence identities must not be empty")
        if self.evaluation.event_snapshot_id != self.event_snapshot_id:
            raise ValueValidationError("event history evidence must retain its exact evaluation")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))


@dataclass(frozen=True, slots=True)
class EventHistoryRun:
    """A bounded completed Kalshi market run and one exact event occurrence, if any."""

    ingestion_run_id: str
    provider: PublicMarketProvider
    state: str
    observed_start_at: datetime
    observed_end_at: datetime
    evidence: EventHistoryEvidence | None
    comparison_transition_id: str | None = None

    def __post_init__(self) -> None:
        if not self.ingestion_run_id:
            raise ValueValidationError("event history run identity must not be empty")
        start = _aware(self.observed_start_at, "observed_start_at")
        end = _aware(self.observed_end_at, "observed_end_at")
        if end < start:
            raise ValueValidationError("event history run observation range is reversed")
        if self.state != "COMPLETED":
            raise ValueValidationError("event history requires completed ingestion runs")
        if self.evidence is not None and self.evidence.provider is not self.provider:
            raise ValueValidationError("event history evidence provider must match its run")
        object.__setattr__(self, "observed_start_at", start)
        object.__setattr__(self, "observed_end_at", end)


@dataclass(frozen=True, slots=True)
class EventHistoryObservation:
    observation_id: str
    analysis_id: str
    ordinal: int
    ingestion_run_id: str
    run_observed_start_at: datetime
    run_observed_end_at: datetime
    state: EventHistoryObservationState
    event_snapshot_id: str | None
    consistency_evaluation_id: str | None
    comparison_transition_id: str | None
    evaluation_state: ConsistencyEvaluationState | None
    consistency_deviation: Decimal | None
    yes_ask_sum: Decimal | None
    yes_bid_sum: Decimal | None
    quote_coverage_count: int | None
    missing_quote_count: int | None
    minimum_visible_size: Decimal | None
    evaluator_version: str | None
    rule_evidence_hash: ContentHash | None
    settlement_source_hash: ContentHash | None
    relationship_evidence_hash: ContentHash | None
    continuity_state: EventHistoryContinuityState
    reason_codes: tuple[str, ...]
    canonical_input_hash: ContentHash

    def __post_init__(self) -> None:
        if not self.observation_id or not self.analysis_id or not self.ingestion_run_id:
            raise ValueValidationError("event history observation identities must not be empty")
        if self.ordinal < 0:
            raise ValueValidationError("event history observation ordinal must not be negative")
        start = _aware(self.run_observed_start_at, "run_observed_start_at")
        end = _aware(self.run_observed_end_at, "run_observed_end_at")
        if end < start:
            raise ValueValidationError("event history observation range is reversed")
        object.__setattr__(self, "run_observed_start_at", start)
        object.__setattr__(self, "run_observed_end_at", end)
        for name in (
            "consistency_deviation",
            "yes_ask_sum",
            "yes_bid_sum",
            "minimum_visible_size",
        ):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))
        if self.quote_coverage_count is not None and self.quote_coverage_count < 0:
            raise ValueValidationError("event history quote coverage must not be negative")
        if self.missing_quote_count is not None and self.missing_quote_count < 0:
            raise ValueValidationError("event history missing quote count must not be negative")
        absent = self.state is EventHistoryObservationState.ABSENT
        if absent != (self.event_snapshot_id is None):
            raise ValueValidationError("absent history observations must preserve event presence")
        if absent and any(
            value is not None
            for value in (
                self.consistency_evaluation_id,
                self.evaluation_state,
                self.consistency_deviation,
                self.evaluator_version,
                self.rule_evidence_hash,
                self.settlement_source_hash,
                self.relationship_evidence_hash,
            )
        ):
            raise ValueValidationError(
                "absent history observations cannot fabricate event evidence"
            )
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class EventHistoryAnalysis:
    analysis_id: str
    provider: PublicMarketProvider
    provider_event_ticker: str
    history_evaluator_version: str
    first_ingestion_run_id: str
    last_ingestion_run_id: str
    requested_maximum_run_count: int
    included_completed_run_count: int
    present_observation_count: int
    absent_observation_count: int
    candidate_observation_count: int
    consistent_observation_count: int
    ineligible_observation_count: int
    invalid_observation_count: int
    continuity_segment_count: int
    evidence_break_count: int
    current_candidate_streak_observation_count: int
    longest_candidate_streak_observation_count: int
    current_candidate_streak_duration: timedelta | None
    longest_candidate_streak_duration: timedelta | None
    first_candidate_observed_at: datetime | None
    latest_candidate_observed_at: datetime | None
    current_consistency_deviation: Decimal | None
    maximum_compatible_consistency_deviation: Decimal | None
    minimum_visible_size_current_streak: Decimal | None
    maximum_observation_gap: timedelta
    state: EventHistoryAnalysisState
    canonical_configuration_hash: ContentHash
    canonical_input_hash: ContentHash
    created_at: datetime

    def __post_init__(self) -> None:
        if not all(
            (
                self.analysis_id,
                self.provider_event_ticker,
                self.history_evaluator_version,
                self.first_ingestion_run_id,
                self.last_ingestion_run_id,
            )
        ):
            raise ValueValidationError("event history analysis identities must not be empty")
        if self.requested_maximum_run_count < 3 or self.included_completed_run_count < 3:
            raise ValueValidationError(
                "event history analysis requires at least three completed runs"
            )
        if self.included_completed_run_count > self.requested_maximum_run_count:
            raise ValueValidationError("event history analysis exceeds its requested run bound")
        for name in (
            "present_observation_count",
            "absent_observation_count",
            "candidate_observation_count",
            "consistent_observation_count",
            "ineligible_observation_count",
            "invalid_observation_count",
            "continuity_segment_count",
            "evidence_break_count",
            "current_candidate_streak_observation_count",
            "longest_candidate_streak_observation_count",
        ):
            if getattr(self, name) < 0:
                raise ValueValidationError("event history analysis counts must not be negative")
        if (
            self.present_observation_count + self.absent_observation_count
            != self.included_completed_run_count
        ):
            raise ValueValidationError("event history presence counts must cover every run")
        if self.maximum_observation_gap.total_seconds() < 0:
            raise ValueValidationError("event history maximum gap must not be negative")
        for name in ("created_at", "first_candidate_observed_at", "latest_candidate_observed_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, name))
        for name in (
            "current_consistency_deviation",
            "maximum_compatible_consistency_deviation",
            "minimum_visible_size_current_streak",
        ):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))

    @property
    def canonical_result_hash(self) -> ContentHash:
        payload = {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name
            not in {
                "current_candidate_streak_duration",
                "longest_candidate_streak_duration",
                "maximum_observation_gap",
            }
        }
        payload["current_candidate_streak_duration_microseconds"] = _duration_microseconds(
            self.current_candidate_streak_duration
        )
        payload["longest_candidate_streak_duration_microseconds"] = _duration_microseconds(
            self.longest_candidate_streak_duration
        )
        payload["maximum_observation_gap_microseconds"] = _duration_microseconds(
            self.maximum_observation_gap
        )
        return ContentHash(content_digest(payload))
