"""Build bounded exact-event observation histories from retained public evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Protocol

from apex.domain.public_event import ConsistencyEvaluationState
from apex.domain.public_history import (
    EventHistoryAnalysis,
    EventHistoryAnalysisState,
    EventHistoryContinuityState,
    EventHistoryEvidence,
    EventHistoryObservation,
    EventHistoryObservationState,
    EventHistoryRun,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash, ValueValidationError, content_digest, deterministic_id

HISTORY_EVALUATOR_VERSION = "v1"
DEFAULT_CONTINUITY_GAP = timedelta(minutes=30)
MAXIMUM_COMPLETED_RUNS = 50


class EventObservationHistoryError(RuntimeError):
    """A bounded history request was invalid or could not retain immutable evidence."""


class EventObservationHistoryConflictError(EventObservationHistoryError):
    """A deterministic history identity was reused with different retained evidence."""


@dataclass(frozen=True, slots=True)
class EventHistoryRequest:
    provider_event_ticker: str
    first_ingestion_run_id: str | None = None
    last_ingestion_run_id: str | None = None
    maximum_completed_runs: int = 20
    continuity_gap: timedelta = DEFAULT_CONTINUITY_GAP

    def __post_init__(self) -> None:
        if (
            not self.provider_event_ticker
            or len(self.provider_event_ticker) > 300
            or not self.provider_event_ticker.isascii()
            or any(character.isspace() for character in self.provider_event_ticker)
        ):
            raise ValueValidationError(
                "event history requires one exact safe provider event ticker"
            )
        if not 3 <= self.maximum_completed_runs <= MAXIMUM_COMPLETED_RUNS:
            raise ValueValidationError("event history maximum completed runs is out of bounds")
        if not timedelta(seconds=1) <= self.continuity_gap <= timedelta(days=1):
            raise ValueValidationError("event history continuity gap is out of bounds")
        for value in (self.first_ingestion_run_id, self.last_ingestion_run_id):
            if value is not None and (not value or len(value) > 160):
                raise ValueValidationError("event history run identifier is invalid")


@dataclass(frozen=True, slots=True)
class EventHistoryReport:
    analysis: EventHistoryAnalysis
    observations: tuple[EventHistoryObservation, ...]
    idempotent_replay: bool


class EventObservationHistoryStore(Protocol):
    def select_completed_runs(
        self,
        *,
        provider: PublicMarketProvider,
        provider_event_ticker: str,
        first_ingestion_run_id: str | None,
        last_ingestion_run_id: str | None,
        maximum_completed_runs: int,
    ) -> tuple[EventHistoryRun, ...]: ...

    def get_history(self, analysis_id: str) -> EventHistoryReport | None: ...

    def append_history(
        self,
        analysis: EventHistoryAnalysis,
        observations: tuple[EventHistoryObservation, ...],
    ) -> None: ...


def _observation_state(evidence: EventHistoryEvidence | None) -> EventHistoryObservationState:
    if evidence is None:
        return EventHistoryObservationState.ABSENT
    return {
        ConsistencyEvaluationState.CANDIDATE: EventHistoryObservationState.PRESENT_CANDIDATE,
        ConsistencyEvaluationState.CONSISTENT: EventHistoryObservationState.PRESENT_CONSISTENT,
        ConsistencyEvaluationState.INELIGIBLE: EventHistoryObservationState.PRESENT_INELIGIBLE,
        ConsistencyEvaluationState.INVALID: EventHistoryObservationState.PRESENT_INVALID,
    }[evidence.evaluation.state]


def _candidate_is_complete(evidence: EventHistoryEvidence | None) -> bool:
    return (
        evidence is not None
        and evidence.evaluation.state is ConsistencyEvaluationState.CANDIDATE
        and evidence.evaluation.missing_quote_count == 0
        and evidence.evaluation.yes_ask_sum is not None
    )


def _evidence_compatible(previous: EventHistoryEvidence, current: EventHistoryEvidence) -> bool:
    return (
        previous.provider_event_ticker == current.provider_event_ticker
        and previous.rule_evidence_hash == current.rule_evidence_hash
        and previous.settlement_source_hash == current.settlement_source_hash
        and previous.relationship_evidence_hash == current.relationship_evidence_hash
        and previous.evaluation.evaluator_version == current.evaluation.evaluator_version
        and current.observed_at > previous.observed_at
    )


class BuildEventObservationHistory:
    """Persist a bounded, exact-ticker research timeline with no network authority."""

    def __init__(self, *, store: EventObservationHistoryStore) -> None:
        self._store = store

    @staticmethod
    def _validate_runs(runs: tuple[EventHistoryRun, ...], request: EventHistoryRequest) -> None:
        if len(runs) < 3:
            raise EventObservationHistoryError("at least three completed Kalshi runs are required")
        if len(runs) > request.maximum_completed_runs:
            raise EventObservationHistoryError("event history run selection exceeds its bound")
        previous: EventHistoryRun | None = None
        for run in runs:
            if run.provider is not PublicMarketProvider.KALSHI or run.state != "COMPLETED":
                raise EventObservationHistoryError(
                    "event history requires completed Kalshi market runs"
                )
            if (
                run.evidence is not None
                and run.evidence.provider_event_ticker != request.provider_event_ticker
            ):
                raise EventObservationHistoryError(
                    "event history evidence must match the exact requested ticker"
                )
            if previous is not None and run.observed_start_at <= previous.observed_end_at:
                raise EventObservationHistoryError(
                    "event history observation ordering is contradictory"
                )
            previous = run
        if (
            request.first_ingestion_run_id
            and runs[0].ingestion_run_id != request.first_ingestion_run_id
        ):
            raise EventObservationHistoryError(
                "event history first run bound is unknown or out of range"
            )
        if (
            request.last_ingestion_run_id
            and runs[-1].ingestion_run_id != request.last_ingestion_run_id
        ):
            raise EventObservationHistoryError(
                "event history last run bound is unknown or out of range"
            )

    @staticmethod
    def _configuration(request: EventHistoryRequest) -> dict[str, object]:
        return {
            "provider": PublicMarketProvider.KALSHI,
            "provider_event_ticker": request.provider_event_ticker,
            "history_evaluator_version": HISTORY_EVALUATOR_VERSION,
            "first_ingestion_run_id": request.first_ingestion_run_id,
            "last_ingestion_run_id": request.last_ingestion_run_id,
            "maximum_completed_runs": request.maximum_completed_runs,
            "continuity_gap_seconds": int(request.continuity_gap.total_seconds()),
        }

    @staticmethod
    def _input_payload(
        configuration: dict[str, object], runs: tuple[EventHistoryRun, ...]
    ) -> dict[str, object]:
        return {
            "configuration": configuration,
            "runs": tuple(
                {
                    "ingestion_run_id": run.ingestion_run_id,
                    "observed_start_at": run.observed_start_at,
                    "observed_end_at": run.observed_end_at,
                    "event_snapshot_id": run.evidence.event_snapshot_id if run.evidence else None,
                    "evaluation_hash": run.evidence.evaluation.canonical_hash
                    if run.evidence
                    else None,
                    "rule_evidence_hash": run.evidence.rule_evidence_hash if run.evidence else None,
                    "settlement_source_hash": run.evidence.settlement_source_hash
                    if run.evidence
                    else None,
                    "relationship_evidence_hash": (
                        run.evidence.relationship_evidence_hash if run.evidence else None
                    ),
                    "comparison_transition_id": run.comparison_transition_id,
                }
                for run in runs
            ),
        }

    @staticmethod
    def _build(
        *,
        analysis_id: str,
        request: EventHistoryRequest,
        configuration_hash: ContentHash,
        input_hash: ContentHash,
        runs: tuple[EventHistoryRun, ...],
    ) -> EventHistoryReport:
        values: list[EventHistoryObservation] = []
        previous_run: EventHistoryRun | None = None
        previous_present: EventHistoryEvidence | None = None
        current_streak: list[EventHistoryObservation] = []
        longest_streak: list[EventHistoryObservation] = []
        maximum_gap = timedelta(0)
        evidence_break_count = 0
        continuity_segments = 0

        for ordinal, run in enumerate(runs):
            evidence = run.evidence
            state = _observation_state(evidence)
            reasons: list[str] = []
            if evidence is None:
                continuity = EventHistoryContinuityState.NOT_APPLICABLE
                reasons.append("EVENT_ABSENT_FROM_COMPLETED_RUN")
            elif previous_present is None:
                continuity = EventHistoryContinuityState.FIRST_OBSERVATION
                reasons.append("FIRST_RETAINED_EVENT_OBSERVATION")
                continuity_segments += 1
            else:
                assert previous_run is not None
                gap = run.observed_start_at - previous_run.observed_end_at
                maximum_gap = max(maximum_gap, gap)
                if gap > request.continuity_gap:
                    continuity = EventHistoryContinuityState.OBSERVATION_GAP
                    reasons.append("OBSERVATION_GAP_EXCEEDS_CONFIGURATION")
                    continuity_segments += 1
                elif not _evidence_compatible(previous_present, evidence):
                    continuity = EventHistoryContinuityState.EVIDENCE_BREAK
                    evidence_break_count += 1
                    continuity_segments += 1
                    if previous_present.rule_evidence_hash != evidence.rule_evidence_hash:
                        reasons.append("RULE_EVIDENCE_CHANGED")
                    if previous_present.settlement_source_hash != evidence.settlement_source_hash:
                        reasons.append("SETTLEMENT_SOURCE_CHANGED")
                    if (
                        previous_present.relationship_evidence_hash
                        != evidence.relationship_evidence_hash
                    ):
                        reasons.append("RELATIONSHIP_EVIDENCE_CHANGED")
                    if (
                        previous_present.evaluation.evaluator_version
                        != evidence.evaluation.evaluator_version
                    ):
                        reasons.append("EVALUATOR_VERSION_CHANGED")
                    if evidence.observed_at <= previous_present.observed_at:
                        reasons.append("EVENT_OBSERVATION_ORDER_INVALID")
                else:
                    continuity = EventHistoryContinuityState.CONTINUOUS

            if previous_run is not None:
                gap = run.observed_start_at - previous_run.observed_end_at
                maximum_gap = max(maximum_gap, gap)
            if evidence is not None and previous_present is None and previous_run is not None:
                reasons.append("EVENT_REAPPEARED_AFTER_ABSENCE")

            deviation = (
                None
                if evidence is None or evidence.evaluation.yes_ask_sum is None
                else Decimal("1") - evidence.evaluation.yes_ask_sum
            )
            if (
                evidence is not None
                and previous_present is not None
                and continuity is EventHistoryContinuityState.CONTINUOUS
            ):
                previous_deviation = (
                    None
                    if previous_present.evaluation.yes_ask_sum is None
                    else Decimal("1") - previous_present.evaluation.yes_ask_sum
                )
                if deviation is not None and previous_deviation is not None:
                    if deviation > previous_deviation:
                        reasons.append("CONSISTENCY_DEVIATION_GREW")
                    elif deviation < previous_deviation:
                        reasons.append("CONSISTENCY_DEVIATION_SHRANK")
                if (
                    evidence.evaluation.quote_coverage_count
                    > previous_present.evaluation.quote_coverage_count
                ):
                    reasons.append("QUOTE_COVERAGE_IMPROVED")
                elif (
                    evidence.evaluation.quote_coverage_count
                    < previous_present.evaluation.quote_coverage_count
                ):
                    reasons.append("QUOTE_COVERAGE_DETERIORATED")
                prior_size = previous_present.evaluation.minimum_visible_executable_size
                size = evidence.evaluation.minimum_visible_executable_size
                if prior_size is not None and size is not None:
                    if size > prior_size:
                        reasons.append("VISIBLE_SIZE_IMPROVED")
                    elif size < prior_size:
                        reasons.append("VISIBLE_SIZE_DETERIORATED")

            input_payload = {
                "analysis_id": analysis_id,
                "ordinal": ordinal,
                "run": run,
                "state": state,
                "continuity": continuity,
            }
            observation = EventHistoryObservation(
                observation_id=deterministic_id("event_history_observation", input_payload),
                analysis_id=analysis_id,
                ordinal=ordinal,
                ingestion_run_id=run.ingestion_run_id,
                run_observed_start_at=run.observed_start_at,
                run_observed_end_at=run.observed_end_at,
                state=state,
                event_snapshot_id=evidence.event_snapshot_id if evidence else None,
                consistency_evaluation_id=evidence.evaluation.evaluation_id if evidence else None,
                comparison_transition_id=run.comparison_transition_id,
                evaluation_state=evidence.evaluation.state if evidence else None,
                consistency_deviation=deviation,
                yes_ask_sum=evidence.evaluation.yes_ask_sum if evidence else None,
                yes_bid_sum=evidence.evaluation.yes_bid_sum if evidence else None,
                quote_coverage_count=evidence.evaluation.quote_coverage_count if evidence else None,
                missing_quote_count=evidence.evaluation.missing_quote_count if evidence else None,
                minimum_visible_size=(
                    evidence.evaluation.minimum_visible_executable_size if evidence else None
                ),
                evaluator_version=evidence.evaluation.evaluator_version if evidence else None,
                rule_evidence_hash=evidence.rule_evidence_hash if evidence else None,
                settlement_source_hash=evidence.settlement_source_hash if evidence else None,
                relationship_evidence_hash=evidence.relationship_evidence_hash
                if evidence
                else None,
                continuity_state=continuity,
                reason_codes=tuple(reasons),
                canonical_input_hash=ContentHash(content_digest(input_payload)),
            )
            values.append(observation)

            qualifies = _candidate_is_complete(evidence)
            if qualifies:
                if (
                    continuity
                    in {
                        EventHistoryContinuityState.CONTINUOUS,
                        EventHistoryContinuityState.FIRST_OBSERVATION,
                    }
                    and current_streak
                ):
                    current_streak.append(observation)
                else:
                    current_streak = [observation]
                if len(current_streak) > len(longest_streak):
                    longest_streak = list(current_streak)
            else:
                current_streak = []
            previous_run = run
            previous_present = evidence

        def duration(streak: list[EventHistoryObservation]) -> timedelta | None:
            if not streak:
                return None
            return streak[-1].run_observed_end_at - streak[0].run_observed_end_at

        candidates = tuple(
            value
            for value in values
            if value.state is EventHistoryObservationState.PRESENT_CANDIDATE
        )
        compatible_candidates = tuple(
            value
            for value in values
            if value.state is EventHistoryObservationState.PRESENT_CANDIDATE
            and value.missing_quote_count == 0
            and value.consistency_deviation is not None
        )
        present_count = sum(
            value.state is not EventHistoryObservationState.ABSENT for value in values
        )
        current_value = values[-1]
        candidate_observed_at = tuple(
            run.evidence.observed_at
            for run in runs
            if run.evidence is not None and _candidate_is_complete(run.evidence)
        )
        analysis = EventHistoryAnalysis(
            analysis_id=analysis_id,
            provider=PublicMarketProvider.KALSHI,
            provider_event_ticker=request.provider_event_ticker,
            history_evaluator_version=HISTORY_EVALUATOR_VERSION,
            first_ingestion_run_id=runs[0].ingestion_run_id,
            last_ingestion_run_id=runs[-1].ingestion_run_id,
            requested_maximum_run_count=request.maximum_completed_runs,
            included_completed_run_count=len(runs),
            present_observation_count=present_count,
            absent_observation_count=len(runs) - present_count,
            candidate_observation_count=len(candidates),
            consistent_observation_count=sum(
                value.state is EventHistoryObservationState.PRESENT_CONSISTENT for value in values
            ),
            ineligible_observation_count=sum(
                value.state is EventHistoryObservationState.PRESENT_INELIGIBLE for value in values
            ),
            invalid_observation_count=sum(
                value.state is EventHistoryObservationState.PRESENT_INVALID for value in values
            ),
            continuity_segment_count=continuity_segments,
            evidence_break_count=evidence_break_count,
            current_candidate_streak_observation_count=len(current_streak),
            longest_candidate_streak_observation_count=len(longest_streak),
            current_candidate_streak_duration=duration(current_streak),
            longest_candidate_streak_duration=duration(longest_streak),
            first_candidate_observed_at=(
                candidate_observed_at[0] if candidate_observed_at else None
            ),
            latest_candidate_observed_at=(
                candidate_observed_at[-1] if candidate_observed_at else None
            ),
            current_consistency_deviation=current_value.consistency_deviation,
            maximum_compatible_consistency_deviation=(
                max(
                    (
                        value.consistency_deviation
                        for value in compatible_candidates
                        if value.consistency_deviation is not None
                    ),
                    default=None,
                )
            ),
            minimum_visible_size_current_streak=(
                min(
                    (
                        value.minimum_visible_size
                        for value in current_streak
                        if value.minimum_visible_size is not None
                    ),
                    default=None,
                )
            ),
            maximum_observation_gap=maximum_gap,
            state=(
                EventHistoryAnalysisState.PARTIAL
                if any(value.state is EventHistoryObservationState.ABSENT for value in values)
                else EventHistoryAnalysisState.COMPLETE
            ),
            canonical_configuration_hash=configuration_hash,
            canonical_input_hash=input_hash,
            created_at=runs[-1].observed_end_at,
        )
        return EventHistoryReport(
            analysis=analysis, observations=tuple(values), idempotent_replay=False
        )

    def build(self, request: EventHistoryRequest) -> EventHistoryReport:
        runs = self._store.select_completed_runs(
            provider=PublicMarketProvider.KALSHI,
            provider_event_ticker=request.provider_event_ticker,
            first_ingestion_run_id=request.first_ingestion_run_id,
            last_ingestion_run_id=request.last_ingestion_run_id,
            maximum_completed_runs=request.maximum_completed_runs,
        )
        self._validate_runs(runs, request)
        configuration = self._configuration(request)
        configuration_hash = ContentHash(content_digest(configuration))
        input_hash = ContentHash(content_digest(self._input_payload(configuration, runs)))
        analysis_id = deterministic_id(
            "event_history_analysis",
            {"configuration_hash": configuration_hash, "input_hash": input_hash},
        )
        existing = self._store.get_history(analysis_id)
        if existing is not None:
            if existing.analysis.canonical_input_hash != input_hash:
                raise EventObservationHistoryConflictError(
                    "event history analysis identity conflicts"
                )
            return EventHistoryReport(
                analysis=existing.analysis,
                observations=existing.observations,
                idempotent_replay=True,
            )
        report = self._build(
            analysis_id=analysis_id,
            request=request,
            configuration_hash=configuration_hash,
            input_hash=input_hash,
            runs=runs,
        )
        try:
            self._store.append_history(report.analysis, report.observations)
        except EventObservationHistoryConflictError:
            raise
        persisted = self._store.get_history(analysis_id)
        if persisted is None:
            raise EventObservationHistoryError("event history was not retained atomically")
        return persisted


class InMemoryEventObservationHistoryStore:
    """Fixture-only immutable history store; it has no network or financial access."""

    def __init__(self, runs: tuple[EventHistoryRun, ...]) -> None:
        self._runs = tuple(runs)
        self._reports: dict[str, EventHistoryReport] = {}

    def select_completed_runs(
        self,
        *,
        provider: PublicMarketProvider,
        provider_event_ticker: str,
        first_ingestion_run_id: str | None,
        last_ingestion_run_id: str | None,
        maximum_completed_runs: int,
    ) -> tuple[EventHistoryRun, ...]:
        values = tuple(
            sorted(
                (
                    run
                    for run in self._runs
                    if run.provider is provider and run.state == "COMPLETED"
                ),
                key=lambda value: (value.observed_start_at, value.ingestion_run_id),
            )
        )
        if first_ingestion_run_id is not None:
            start = next(
                (
                    index
                    for index, run in enumerate(values)
                    if run.ingestion_run_id == first_ingestion_run_id
                ),
                None,
            )
            if start is None:
                return ()
            values = values[start:]
        if last_ingestion_run_id is not None:
            end = next(
                (
                    index
                    for index, run in enumerate(values)
                    if run.ingestion_run_id == last_ingestion_run_id
                ),
                None,
            )
            if end is None:
                return ()
            values = values[: end + 1]
        if first_ingestion_run_id is None and last_ingestion_run_id is None:
            values = values[-maximum_completed_runs:]
        elif len(values) > maximum_completed_runs:
            return ()
        return values

    def get_history(self, analysis_id: str) -> EventHistoryReport | None:
        return self._reports.get(analysis_id)

    def append_history(
        self,
        analysis: EventHistoryAnalysis,
        observations: tuple[EventHistoryObservation, ...],
    ) -> None:
        existing = self._reports.get(analysis.analysis_id)
        candidate = EventHistoryReport(
            analysis=analysis, observations=observations, idempotent_replay=False
        )
        if existing is not None:
            if existing.analysis.canonical_result_hash != analysis.canonical_result_hash:
                raise EventObservationHistoryConflictError(
                    "event history immutable identity conflicts"
                )
            return
        self._reports[analysis.analysis_id] = candidate
