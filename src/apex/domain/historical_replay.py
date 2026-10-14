"""Point-in-time historical replay: durable, resumable replay runs; deterministic
point-in-time feature frames; a small descriptive market-regime layer; a research-
method evaluation seam with structural outcome isolation; research-quality controls
(train/validation/locked-holdout); and deterministic statistical summaries.

This is deliberately not a generic event-driven backtesting framework. The primary
unit is:

    historical as-of date -> point-in-time evidence -> market/context features
    -> research evaluation -> retained replay result

Everything here is built directly on the existing ``domain.historical_market_data``
foundation (``HistoricalClock``, ``point_in_time_slice``/``assert_no_lookahead``,
the reusable Decimal calculations) and feeds the existing ``domain.research_evidence``
hypothesis/experiment lifecycle -- it does not define a second evidence-class
vocabulary, a second point-in-time guard, or a parallel experiment-tracking
subsystem. No record here ever carries Strategy, Opportunity, PAPER, or LIVE
authority; every retained record below is ``authority = "NONE"``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from enum import StrEnum
from itertools import pairwise

from apex.domain.historical_market_data import (
    POINT_IN_TIME_UNIVERSE_INCOMPLETE,
    SURVIVORSHIP_WARNING,
    HistoricalClock,
    assert_no_lookahead,
    cross_sectional_rank,
    max_drawdown,
    point_in_time_slice,
    rolling_correlation,
    rolling_liquidity,
    rolling_volatility,
)
from apex.domain.securities import SecuritiesBarEvidence, SecuritiesCalendarSession
from apex.domain.values import content_digest, deterministic_id

HISTORICAL_REPLAY_V1 = "HISTORICAL_REPLAY_V1"
REPLAY_WARNING = (
    "HISTORICAL REPLAY IS RESEARCH EVIDENCE -- NOT A BACKTESTED STRATEGY, "
    "TRADING EDGE, OR EXECUTION AUTHORITY"
)
HISTORICAL_OPTIONS_PRICING_UNAVAILABLE = "HISTORICAL_OPTIONS_PRICING_UNAVAILABLE"
TRANSACTION_COST_UNAVAILABLE = "TRANSACTION_COST_MODEL_UNAVAILABLE_THIS_MILESTONE"
NO_EXCHANGE_CALENDAR_CONTINUOUS_STEPPING = "NO_EXCHANGE_CALENDAR_DECLARED_CONTINUOUS_DAILY_STEPPING"
CALENDAR_EVIDENCE_PARTIAL_FOR_RANGE = "CALENDAR_EVIDENCE_PARTIAL_FOR_DECLARED_RANGE"
RETURN_WINDOWS: tuple[int, ...] = (1, 5, 20, 60, 90, 252)
CROSS_SECTIONAL_RETURN_RANK_WINDOW = 20
CROSS_SECTIONAL_VOLATILITY_RANK_WINDOW = 20
BREADTH_MOVING_AVERAGE_WINDOW = 20
BENCHMARK_CORRELATION_WINDOW = 60
REGIME_RULE_VERSION = "MARKET_REGIME_RULE_V1"
OUTCOME_HORIZON_CALENDAR_DAYS = 30
SUBSEQUENT_BENCHMARK_RETURN_METRIC = "SUBSEQUENT_BENCHMARK_RETURN_20_SESSION_V1"
SUBSEQUENT_DISPERSION_METRIC = "SUBSEQUENT_CROSS_SECTIONAL_DISPERSION_20_SESSION_V1"
STATISTICS_METHOD_NORMAL_APPROXIMATION_V1 = "NORMAL_APPROXIMATION_V1"
MAXIMUM_OBSERVATION_AGE_DAYS_BEFORE_UNAVAILABLE = 10

# Fixed, content-addressed identities for this milestone's one feature policy
# and one research-method policy -- a caller can declare a different
# ``policy_id`` string, but the *hash* is always this exact function of the
# concrete rule constants above, so a resumed run can detect a genuine rule
# change (a code deploy that alters a threshold) even if the id string was
# left unchanged.
FEATURE_POLICY_ID_V1 = "HISTORICAL_REPLAY_FEATURE_POLICY_V1"
RESEARCH_POLICY_ID_V1 = "HISTORICAL_REPLAY_RESEARCH_POLICY_V1"


class HistoricalReplayError(ValueError):
    """A historical-replay evidence invariant was violated."""


class ReplayConflictError(HistoricalReplayError):
    """An idempotent replay operation was replayed with different input."""


class OutcomeIsolationError(HistoricalReplayError):
    """An operation would have let outcome evidence leak into a feature cutoff."""


class HoldoutViolationError(HistoricalReplayError):
    """A locked holdout research period was accessed for development."""


class ResearchQualityError(HistoricalReplayError):
    """A research-quality control (sample size, period declaration) was violated."""


# --------------------------------------------------------------------------
# Small helpers mirroring domain.historical_market_data's own private helpers
# (kept local rather than imported so each domain module stays independently
# readable -- matches that module's own precedent of locally mirroring
# domain.securities._bounded_decimal instead of importing it).
# --------------------------------------------------------------------------


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HistoricalReplayError(f"{label} must be timezone-aware")
    return value


def _iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise HistoricalReplayError(f"{label} must be an ISO date") from error


def _bounded_decimal(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return value.quantize(Decimal("0.00000001"))


def _mean(values: Sequence[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return sum(values, Decimal(0)) / Decimal(len(values))


def _decimal_sqrt(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        if value < 0:
            raise HistoricalReplayError("REPLAY_SQRT_NEGATIVE_INPUT")
        return +value.sqrt()


def _stdev(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        raise HistoricalReplayError("REPLAY_STDEV_REQUIRES_TWO_VALUES")
    average = _mean(values)
    with localcontext() as context:
        context.prec = 28
        variance = sum(((value - average) ** 2 for value in values), Decimal(0)) / Decimal(
            len(values)
        )
    return _decimal_sqrt(variance)


def _text(value: str, code: str, *, maximum: int = 500) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise HistoricalReplayError(code)
    return normalized


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class ReplayCadence(StrEnum):
    DAILY = "DAILY"


class ReplayUniversePolicy(StrEnum):
    """Only one policy exists: the replay's admissible universe is exactly the
    stable-security cohort already frozen into the bound
    ``HistoricalDatasetSnapshot`` -- never rediscovered or widened mid-replay."""

    DECLARED_FIXED_STABLE_COHORT = "DECLARED_FIXED_STABLE_COHORT"


class ReplayRunCompletionState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class CalendarCoverageState(StrEnum):
    CALENDAR_EVIDENCE_AVAILABLE = "CALENDAR_EVIDENCE_AVAILABLE"
    CALENDAR_EVIDENCE_UNAVAILABLE = "CALENDAR_EVIDENCE_UNAVAILABLE"


class EvidenceAvailabilityState(StrEnum):
    CURRENT = "CURRENT"
    STALE_LAST_KNOWN = "STALE_LAST_KNOWN"
    UNAVAILABLE = "UNAVAILABLE"


class RegimeDimension(StrEnum):
    RISK_TONE = "RISK_TONE"
    BREADTH_STRENGTH = "BREADTH_STRENGTH"
    DISPERSION = "DISPERSION"
    VOLATILITY_REGIME = "VOLATILITY_REGIME"
    TREND_STRENGTH = "TREND_STRENGTH"
    CORRELATION_REGIME = "CORRELATION_REGIME"


class RiskToneLabel(StrEnum):
    RISK_ON = "RISK_ON"
    NEUTRAL = "NEUTRAL"
    RISK_OFF = "RISK_OFF"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class BreadthStrengthLabel(StrEnum):
    BROADLY_ADVANCING = "BROADLY_ADVANCING"
    MIXED = "MIXED"
    BROADLY_DECLINING = "BROADLY_DECLINING"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class DispersionLabel(StrEnum):
    LOW_DISPERSION = "LOW_DISPERSION"
    MODERATE_DISPERSION = "MODERATE_DISPERSION"
    HIGH_DISPERSION = "HIGH_DISPERSION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class VolatilityRegimeLabel(StrEnum):
    LOW_VOLATILITY = "LOW_VOLATILITY"
    NORMAL_VOLATILITY = "NORMAL_VOLATILITY"
    ELEVATED_VOLATILITY = "ELEVATED_VOLATILITY"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class TrendStrengthLabel(StrEnum):
    STRONG_UPTREND = "STRONG_UPTREND"
    UPTREND = "UPTREND"
    RANGE_BOUND = "RANGE_BOUND"
    DOWNTREND = "DOWNTREND"
    STRONG_DOWNTREND = "STRONG_DOWNTREND"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class CorrelationRegimeLabel(StrEnum):
    LOW_CORRELATION = "LOW_CORRELATION"
    MODERATE_CORRELATION = "MODERATE_CORRELATION"
    HIGH_CORRELATION = "HIGH_CORRELATION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ResearchMethodId(StrEnum):
    BREADTH_VS_SUBSEQUENT_RETURN_V1 = "BREADTH_VS_SUBSEQUENT_RETURN_V1"
    VOLATILITY_REGIME_VS_SUBSEQUENT_DISPERSION_V1 = "VOLATILITY_REGIME_VS_SUBSEQUENT_DISPERSION_V1"
    CRYPTO_EQUITY_CONTEXT_COMPARISON_V1 = "CRYPTO_EQUITY_CONTEXT_COMPARISON_V1"
    # Added for the Cross-Market Feature, Regime, and Controlled Hypothesis
    # Research v1 milestone (``domain.cross_market_research``). One generic
    # method identity for every templated cross-market hypothesis trial --
    # the template/feature/outcome/horizon distinguishing one hypothesis
    # from another is retained on ``GeneratedHypothesis`` and on each
    # trial's own ``ResearchConfiguration.parameter_values``, not encoded
    # into a proliferating set of method ids here.
    CROSS_MARKET_CONTROLLED_HYPOTHESIS_V1 = "CROSS_MARKET_CONTROLLED_HYPOTHESIS_V1"
    # Added for Research Signal Calibration and Negative-Control Hardening v1
    # (``domain.research_calibration``). One generic method identity for every
    # null/negative-control trial in the calibration battery -- the family/
    # null-generation-method/source-feature/outcome/horizon distinguishing one
    # control from another is retained on ``NullControlSpec`` and on each
    # trial's own ``ResearchConfiguration.parameter_values``, exactly mirroring
    # how ``CROSS_MARKET_CONTROLLED_HYPOTHESIS_V1`` above already does this.
    RESEARCH_SIGNAL_CALIBRATION_NULL_CONTROL_V1 = "RESEARCH_SIGNAL_CALIBRATION_NULL_CONTROL_V1"
    # Added for Research Calibration v2: Template-Matched Nulls and Stability
    # (``domain.research_calibration_v2``). One generic method identity for
    # every template-matched null-control trial in the v2 battery, exactly
    # mirroring ``RESEARCH_SIGNAL_CALIBRATION_NULL_CONTROL_V1`` above -- the
    # research design/conditioning shape/matched stratum distinguishing one
    # v2 control from another is retained on ``NullControlSpecV2`` and on
    # each trial's own ``ResearchConfiguration.parameter_values``.
    TEMPLATE_MATCHED_CALIBRATION_NULL_CONTROL_V2 = "TEMPLATE_MATCHED_CALIBRATION_NULL_CONTROL_V2"
    # Added for Cross-Market Research v3: Fundamental and Market Evidence
    # Integration (``domain.corporate_market_research``). One generic method
    # identity for every corporate-evidence hypothesis trial (genuine or
    # negative-control) -- exactly mirroring
    # ``CROSS_MARKET_CONTROLLED_HYPOTHESIS_V1``'s own rationale above: the
    # template/feature/outcome/horizon distinguishing one corporate
    # hypothesis from another lives on ``GeneratedCorporateHypothesis`` and
    # on each trial's own ``ResearchConfiguration.parameter_values``, never
    # encoded here.
    CORPORATE_MARKET_CONTROLLED_HYPOTHESIS_V1 = "CORPORATE_MARKET_CONTROLLED_HYPOTHESIS_V1"
    # Added for Corporate Research Calibration Depth v1
    # (``domain.corporate_calibration_depth``). One generic method identity
    # for every deepened, structurally matched corporate null-control trial
    # -- exactly mirroring ``CORPORATE_MARKET_CONTROLLED_HYPOTHESIS_V1``'s
    # own rationale above, and kept distinct from it so a calibration-depth
    # control's trial evidence is never mistaken for a genuine or original
    # v3 negative-control hypothesis trial: the mechanism/stratum/seed
    # distinguishing one calibration-depth control from another lives on
    # ``CorporateNullControlSpec`` and on each trial's own
    # ``ResearchConfiguration.parameter_values``, never encoded here.
    CORPORATE_CALIBRATION_DEPTH_NULL_CONTROL_V1 = "CORPORATE_CALIBRATION_DEPTH_NULL_CONTROL_V1"


class OutcomeCompletenessState(StrEnum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class ResearchPeriodRole(StrEnum):
    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    HOLDOUT = "HOLDOUT"


class SummaryKind(StrEnum):
    EXPLORATORY = "EXPLORATORY"
    VALIDATION = "VALIDATION"
    HOLDOUT = "HOLDOUT"
    WALK_FORWARD_AGGREGATE = "WALK_FORWARD_AGGREGATE"


FEATURE_POLICY_HASH_V1 = content_digest(
    (
        FEATURE_POLICY_ID_V1,
        RETURN_WINDOWS,
        CROSS_SECTIONAL_RETURN_RANK_WINDOW,
        CROSS_SECTIONAL_VOLATILITY_RANK_WINDOW,
        BREADTH_MOVING_AVERAGE_WINDOW,
        BENCHMARK_CORRELATION_WINDOW,
        MAXIMUM_OBSERVATION_AGE_DAYS_BEFORE_UNAVAILABLE,
        REGIME_RULE_VERSION,
    )
)
RESEARCH_POLICY_HASH_V1 = content_digest(
    (
        RESEARCH_POLICY_ID_V1,
        tuple(item.value for item in ResearchMethodId),
        STATISTICS_METHOD_NORMAL_APPROXIMATION_V1,
    )
)


# --------------------------------------------------------------------------
# Historical clock semantics (section 2): deterministic, evidence-driven
# next-as-of. Never reads wall-clock time; every input is caller-supplied.
# --------------------------------------------------------------------------


def _end_of_day_utc(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, 23, 59, 59, tzinfo=UTC)


def step_replay_clock(
    *,
    after_date: str | None,
    start_date: str,
    end_date: str,
    calendar_identity: str | None,
    sessions: tuple[SecuritiesCalendarSession, ...],
) -> tuple[HistoricalClock | None, str | None, CalendarCoverageState, tuple[str, ...]]:
    """Deterministic next-as-of.

    ``after_date=None`` asks for the first as_of in the declared range;
    otherwise returns the next as_of strictly after ``after_date``. Returns
    ``(clock, session_date, coverage_state, warnings)`` where ``clock is
    None`` means the declared range is exhausted at or before ``end_date`` --
    never advances past ``end_date`` and never fabricates a session.

    When ``calendar_identity`` is declared, evidence comes only from
    ``sessions`` (typically loaded from ``SecuritiesCalendarPort`` by the
    caller): weekends and exchange holidays are excluded because genuine
    calendar evidence never lists them as sessions, not by a hand-rolled
    weekday check. When the declared range is not (or only partially)
    covered by retained calendar evidence, that limitation is reported
    explicitly rather than silently guessed.

    With no ``calendar_identity`` (e.g. a crypto-only replay), evidence
    trades continuously: every calendar day steps forward with no
    weekend/holiday concept, and that fallback is itself flagged.
    """

    if _iso_date(start_date, "replay start_date") > _iso_date(end_date, "replay end_date"):
        raise HistoricalReplayError("REPLAY_DATE_RANGE_INVALID")
    lower_bound = start_date if after_date is None else after_date

    if calendar_identity is not None:
        by_date = {
            item.session_date: item
            for item in sessions
            if start_date <= item.session_date <= end_date
        }
        relevant = tuple(sorted(by_date.values(), key=lambda item: item.session_date))
        if not relevant:
            return (
                None,
                None,
                CalendarCoverageState.CALENDAR_EVIDENCE_UNAVAILABLE,
                ("NO_CALENDAR_EVIDENCE_FOR_DECLARED_RANGE",),
            )
        candidates = [
            item
            for item in relevant
            if (
                item.session_date > lower_bound
                if after_date is not None
                else item.session_date >= lower_bound
            )
        ]
        warnings: tuple[str, ...] = (
            (CALENDAR_EVIDENCE_PARTIAL_FOR_RANGE,)
            if relevant[0].session_date > start_date or relevant[-1].session_date < end_date
            else ()
        )
        if not candidates:
            return None, None, CalendarCoverageState.CALENDAR_EVIDENCE_AVAILABLE, warnings
        chosen = candidates[0]
        return (
            HistoricalClock(as_of=chosen.closes_at),
            chosen.session_date,
            CalendarCoverageState.CALENDAR_EVIDENCE_AVAILABLE,
            warnings,
        )

    candidate_date = (
        _iso_date(lower_bound, "replay cursor date")
        if after_date is None
        else _iso_date(lower_bound, "replay cursor date") + timedelta(days=1)
    )
    if candidate_date.isoformat() > end_date:
        return None, None, CalendarCoverageState.CALENDAR_EVIDENCE_UNAVAILABLE, ()
    return (
        HistoricalClock(as_of=_end_of_day_utc(candidate_date)),
        candidate_date.isoformat(),
        CalendarCoverageState.CALENDAR_EVIDENCE_UNAVAILABLE,
        (NO_EXCHANGE_CALENDAR_CONTINUOUS_STEPPING,),
    )


def replay_bounds(
    *,
    start_date: str,
    end_date: str,
    calendar_identity: str | None,
    sessions: tuple[SecuritiesCalendarSession, ...],
) -> tuple[HistoricalClock, str, HistoricalClock, str, CalendarCoverageState, tuple[str, ...]]:
    """The declared run's fixed ``(initial_as_of, final_as_of)`` bounds --
    computed directly (never by looping ``step_replay_clock`` one day at a
    time, which would be wasteful over a multi-year range). Raises if the
    declared range has no as_of at all (e.g. no calendar evidence and an
    empty range)."""

    first_clock, first_date, coverage, warnings = step_replay_clock(
        after_date=None,
        start_date=start_date,
        end_date=end_date,
        calendar_identity=calendar_identity,
        sessions=sessions,
    )
    if first_clock is None or first_date is None:
        raise HistoricalReplayError("REPLAY_RUN_NO_AS_OF_IN_DECLARED_RANGE")
    if calendar_identity is not None:
        relevant = tuple(
            sorted(
                {
                    item.session_date: item
                    for item in sessions
                    if start_date <= item.session_date <= end_date
                }.values(),
                key=lambda item: item.session_date,
            )
        )
        last = relevant[-1]
        return (
            first_clock,
            first_date,
            HistoricalClock(as_of=last.closes_at),
            last.session_date,
            coverage,
            warnings,
        )
    last_date = _iso_date(end_date, "replay end_date")
    return (
        first_clock,
        first_date,
        HistoricalClock(as_of=_end_of_day_utc(last_date)),
        last_date.isoformat(),
        coverage,
        warnings,
    )


# --------------------------------------------------------------------------
# Replay run (section 1) and checkpoints (section 10)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayRun:
    """A durable, checkpointable, resumable, idempotent replay run.

    ``canonical_input_hash`` identifies only the *declared* bindings (dataset
    snapshot, date range, cadence, universe, policy versions) -- it never
    changes across the run's life. Mutable progress (``cursor_as_of``,
    ``completed_step_count``, ``completion_state``) is tracked separately so
    resuming a run can never silently reinterpret what was declared at
    creation; see ``ReplayRun.advance``/``ResumeReplayRun`` for the guarded
    transitions.
    """

    replay_run_id: str
    dataset_snapshot_id: str
    universe: tuple[str, ...]
    start_date: str
    end_date: str
    cadence: ReplayCadence
    universe_policy: ReplayUniversePolicy
    calendar_identity: str | None
    feature_policy_id: str
    feature_policy_hash: str
    research_policy_id: str
    research_policy_hash: str
    initial_as_of: datetime
    final_as_of: datetime
    calendar_coverage_state: CalendarCoverageState
    calendar_warnings: tuple[str, ...]
    cursor_as_of: datetime | None
    cursor_session_date: str | None
    completed_step_count: int
    completion_state: ReplayRunCompletionState
    failure_reason: str | None
    hypothesis_version_id: str | None
    operator_action_id: str
    idempotency_key: str
    created_at: datetime
    canonical_input_hash: str
    authority: str = "NONE"

    @classmethod
    def draft(
        cls,
        *,
        dataset_snapshot_id: str,
        universe: tuple[str, ...],
        start_date: str,
        end_date: str,
        cadence: ReplayCadence | str,
        calendar_identity: str | None,
        feature_policy_id: str,
        feature_policy_hash: str,
        research_policy_id: str,
        research_policy_hash: str,
        initial_as_of: datetime,
        final_as_of: datetime,
        calendar_coverage_state: CalendarCoverageState | str,
        calendar_warnings: tuple[str, ...],
        hypothesis_version_id: str | None,
        operator_action_id: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> ReplayRun:
        _aware(created_at, "replay run created_at")
        _aware(initial_as_of, "replay run initial_as_of")
        _aware(final_as_of, "replay run final_as_of")
        if initial_as_of > final_as_of:
            raise HistoricalReplayError("REPLAY_RUN_AS_OF_BOUNDS_INVALID")
        if not dataset_snapshot_id or not operator_action_id or not idempotency_key:
            raise HistoricalReplayError("REPLAY_RUN_IDENTITY_REQUIRED")
        ordered_universe = tuple(sorted(set(universe)))
        if not ordered_universe:
            raise HistoricalReplayError("REPLAY_RUN_UNIVERSE_REQUIRED")
        cadence_value = ReplayCadence(cadence)
        coverage_value = CalendarCoverageState(calendar_coverage_state)
        payload = (
            HISTORICAL_REPLAY_V1,
            dataset_snapshot_id,
            ordered_universe,
            start_date,
            end_date,
            cadence_value,
            ReplayUniversePolicy.DECLARED_FIXED_STABLE_COHORT,
            calendar_identity,
            feature_policy_id,
            feature_policy_hash,
            research_policy_id,
            research_policy_hash,
            hypothesis_version_id,
            operator_action_id,
            idempotency_key,
        )
        return cls(
            replay_run_id=deterministic_id("historical_replay_run", payload),
            dataset_snapshot_id=dataset_snapshot_id,
            universe=ordered_universe,
            start_date=start_date,
            end_date=end_date,
            cadence=cadence_value,
            universe_policy=ReplayUniversePolicy.DECLARED_FIXED_STABLE_COHORT,
            calendar_identity=calendar_identity,
            feature_policy_id=feature_policy_id,
            feature_policy_hash=feature_policy_hash,
            research_policy_id=research_policy_id,
            research_policy_hash=research_policy_hash,
            initial_as_of=initial_as_of,
            final_as_of=final_as_of,
            calendar_coverage_state=coverage_value,
            calendar_warnings=tuple(calendar_warnings),
            cursor_as_of=None,
            cursor_session_date=None,
            completed_step_count=0,
            completion_state=ReplayRunCompletionState.NOT_STARTED,
            failure_reason=None,
            hypothesis_version_id=hypothesis_version_id,
            operator_action_id=operator_action_id,
            idempotency_key=idempotency_key,
            created_at=created_at,
            canonical_input_hash=content_digest(payload),
        )

    def advance(self, *, next_as_of: datetime, session_date: str) -> ReplayRun:
        if self.completion_state is ReplayRunCompletionState.COMPLETE:
            raise HistoricalReplayError("REPLAY_RUN_ALREADY_COMPLETE")
        if self.completion_state is ReplayRunCompletionState.FAILED:
            raise HistoricalReplayError("REPLAY_RUN_FAILED_CANNOT_ADVANCE")
        _aware(next_as_of, "replay run next_as_of")
        if self.cursor_as_of is not None and next_as_of <= self.cursor_as_of:
            raise HistoricalReplayError("REPLAY_RUN_CURSOR_MUST_ADVANCE_FORWARD")
        if next_as_of > self.final_as_of:
            raise HistoricalReplayError("REPLAY_RUN_CURSOR_EXCEEDS_DECLARED_RANGE")
        completion = (
            ReplayRunCompletionState.COMPLETE
            if next_as_of >= self.final_as_of
            else ReplayRunCompletionState.IN_PROGRESS
        )
        return replace(
            self,
            cursor_as_of=next_as_of,
            cursor_session_date=session_date,
            completed_step_count=self.completed_step_count + 1,
            completion_state=completion,
        )

    def fail(self, *, reason: str) -> ReplayRun:
        if self.completion_state is ReplayRunCompletionState.COMPLETE:
            raise HistoricalReplayError("REPLAY_RUN_ALREADY_COMPLETE")
        return replace(
            self,
            completion_state=ReplayRunCompletionState.FAILED,
            failure_reason=_text(reason, "REPLAY_RUN_FAILURE_REASON_INVALID", maximum=500),
        )


@dataclass(frozen=True, slots=True)
class ReplayCheckpoint:
    """One immutable, append-only checkpoint recording exactly what a replay
    run had completed as of one ``as_of`` step -- resume replays these facts,
    it never recomputes them."""

    checkpoint_id: str
    replay_run_id: str
    as_of: datetime
    session_date: str
    frame_ids: tuple[str, ...]
    evaluation_ids: tuple[str, ...]
    dataset_snapshot_id: str
    feature_policy_hash: str
    research_policy_hash: str
    completed_step_count: int
    created_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        run: ReplayRun,
        as_of: datetime,
        session_date: str,
        frame_ids: tuple[str, ...],
        evaluation_ids: tuple[str, ...],
        created_at: datetime,
    ) -> ReplayCheckpoint:
        _aware(as_of, "checkpoint as_of")
        _aware(created_at, "checkpoint created_at")
        payload = (
            HISTORICAL_REPLAY_V1,
            run.replay_run_id,
            as_of,
            session_date,
            tuple(sorted(frame_ids)),
            tuple(sorted(evaluation_ids)),
            run.dataset_snapshot_id,
            run.feature_policy_hash,
            run.research_policy_hash,
            run.completed_step_count,
        )
        return cls(
            checkpoint_id=deterministic_id("historical_replay_checkpoint", payload),
            replay_run_id=run.replay_run_id,
            as_of=as_of,
            session_date=session_date,
            frame_ids=tuple(sorted(frame_ids)),
            evaluation_ids=tuple(sorted(evaluation_ids)),
            dataset_snapshot_id=run.dataset_snapshot_id,
            feature_policy_hash=run.feature_policy_hash,
            research_policy_hash=run.research_policy_hash,
            completed_step_count=run.completed_step_count,
            created_at=created_at,
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Historical feature frame (section 5)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SecurityFeatureVector:
    stable_security_id: str
    instrument_id: str
    returns: tuple[tuple[int, Decimal | None], ...]
    rolling_volatility_20: Decimal | None
    drawdown: Decimal | None
    rolling_liquidity_20: Decimal | None
    cross_sectional_return_rank: Decimal | None
    cross_sectional_volatility_rank: Decimal | None
    benchmark_correlation_60: Decimal | None
    above_moving_average_20: bool | None
    observation_count: int
    observation_age_days: int | None
    evidence_availability: EvidenceAvailabilityState
    missing_history: bool
    unavailable_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BreadthSnapshot:
    universe_size: int
    evaluable_count: int
    advancing_count: int
    declining_count: int
    unchanged_count: int
    fraction_advancing: Decimal | None
    fraction_above_moving_average: Decimal | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    dimension: RegimeDimension
    label: str
    confidence: Decimal
    evidence: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class MarketRegimeSnapshot:
    rule_version: str
    classifications: tuple[RegimeClassification, ...]

    def label_for(self, dimension: RegimeDimension) -> str:
        for item in self.classifications:
            if item.dimension is dimension:
                return item.label
        raise HistoricalReplayError("REGIME_DIMENSION_NOT_CLASSIFIED")


@dataclass(frozen=True, slots=True)
class ReplayFeatureFrame:
    """One deterministic, reusable point-in-time feature frame: the dataset
    slice, universe, and derived features Apex genuinely had available at
    exactly ``as_of`` -- never reinterpreting the underlying immutable
    dataset, and never inventing a value whose lookback was incomplete."""

    frame_id: str
    replay_run_id: str
    dataset_snapshot_id: str
    as_of: datetime
    session_date: str
    feature_policy_id: str
    feature_policy_hash: str
    universe: tuple[str, ...]
    securities: tuple[SecurityFeatureVector, ...]
    breadth: BreadthSnapshot
    regime: MarketRegimeSnapshot
    excluded_evidence_count: int
    excluded_evidence_reasons: tuple[str, ...]
    point_in_time_limitations: tuple[str, ...]
    data_quality_flags: tuple[str, ...]
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    def security(self, stable_security_id: str) -> SecurityFeatureVector | None:
        return next(
            (item for item in self.securities if item.stable_security_id == stable_security_id),
            None,
        )


def multi_window_returns(
    bars: Sequence[SecuritiesBarEvidence],
    *,
    as_of: datetime,
    windows: tuple[int, ...] = RETURN_WINDOWS,
) -> tuple[tuple[int, Decimal | None], ...]:
    """Simple returns over each trailing session window in ``windows``.

    A window whose lookback is not yet fully covered by point-in-time-safe
    evidence is reported as ``None`` rather than computed from a shorter,
    silently-substituted series.
    """

    ordered = assert_no_lookahead(bars, as_of=as_of)
    results: list[tuple[int, Decimal | None]] = []
    for window in windows:
        if window < 1:
            raise HistoricalReplayError("REPLAY_RETURN_WINDOW_INVALID")
        if len(ordered) < window + 1:
            results.append((window, None))
            continue
        first = ordered[-(window + 1)]
        last = ordered[-1]
        with localcontext() as context:
            context.prec = 28
            results.append((window, _bounded_decimal((last.close - first.close) / first.close)))
    return tuple(results)


def observation_age_days(bars: Sequence[SecuritiesBarEvidence], *, as_of: datetime) -> int | None:
    ordered = assert_no_lookahead(bars, as_of=as_of)
    if not ordered:
        return None
    return (as_of.date() - ordered[-1].period_end.date()).days


def evidence_availability(
    bars: Sequence[SecuritiesBarEvidence], *, as_of: datetime, session_date: str
) -> EvidenceAvailabilityState:
    ordered = assert_no_lookahead(bars, as_of=as_of)
    if not ordered:
        return EvidenceAvailabilityState.UNAVAILABLE
    latest = ordered[-1]
    if latest.period_end.date().isoformat() == session_date:
        return EvidenceAvailabilityState.CURRENT
    age = (_iso_date(session_date, "evidence session_date") - latest.period_end.date()).days
    if age > MAXIMUM_OBSERVATION_AGE_DAYS_BEFORE_UNAVAILABLE:
        return EvidenceAvailabilityState.UNAVAILABLE
    return EvidenceAvailabilityState.STALE_LAST_KNOWN


def moving_average_flag(
    bars: Sequence[SecuritiesBarEvidence], *, as_of: datetime, window: int
) -> bool | None:
    """Whether the latest point-in-time-safe close is above the trailing
    ``window``-session simple moving average -- ``None`` (never ``False`` by
    default) when the lookback is incomplete."""

    ordered = assert_no_lookahead(bars, as_of=as_of)
    if len(ordered) < window:
        return None
    tail = ordered[-window:]
    with localcontext() as context:
        context.prec = 28
        average = _mean([bar.close for bar in tail])
    return bool(ordered[-1].close > average)


def build_security_feature_vector(
    *,
    stable_security_id: str,
    instrument_id: str,
    bars: Sequence[SecuritiesBarEvidence],
    benchmark_bars: Sequence[SecuritiesBarEvidence] | None,
    as_of: datetime,
    session_date: str,
) -> SecurityFeatureVector:
    """Every feature-side calculation here -- like every calculation in
    ``domain.historical_market_data`` it is built on -- requires ``bars`` to
    already be a point-in-time-safe slice (see ``point_in_time_slice``) and
    rejects anything else loudly via ``assert_no_lookahead``. Callers building
    a replay step are expected to slice once per (security, as_of) before
    calling this; it never silently filters future evidence itself.
    """

    ordered = assert_no_lookahead(bars, as_of=as_of)
    reasons: list[str] = []
    if not ordered:
        reasons.append("NO_RETAINED_OBSERVATIONS_AT_OR_BEFORE_AS_OF")
    returns = multi_window_returns(ordered, as_of=as_of)
    drawdown = max_drawdown(ordered, as_of=as_of)
    volatility = rolling_volatility(
        ordered, as_of=as_of, window=CROSS_SECTIONAL_VOLATILITY_RANK_WINDOW
    )
    liquidity = rolling_liquidity(ordered, as_of=as_of, window=BREADTH_MOVING_AVERAGE_WINDOW)
    correlation = (
        rolling_correlation(
            ordered, benchmark_bars, as_of=as_of, window=BENCHMARK_CORRELATION_WINDOW
        )
        if benchmark_bars is not None and ordered
        else None
    )
    above_ma = moving_average_flag(ordered, as_of=as_of, window=BREADTH_MOVING_AVERAGE_WINDOW)
    missing_history = any(value is None for _window, value in returns)
    if missing_history:
        reasons.append("INSUFFICIENT_LOOKBACK_FOR_ONE_OR_MORE_RETURN_WINDOWS")
    return SecurityFeatureVector(
        stable_security_id=stable_security_id,
        instrument_id=instrument_id,
        returns=returns,
        rolling_volatility_20=volatility,
        drawdown=drawdown,
        rolling_liquidity_20=liquidity,
        cross_sectional_return_rank=None,
        cross_sectional_volatility_rank=None,
        benchmark_correlation_60=correlation,
        above_moving_average_20=above_ma,
        observation_count=len(ordered),
        observation_age_days=observation_age_days(ordered, as_of=as_of),
        evidence_availability=evidence_availability(
            ordered, as_of=as_of, session_date=session_date
        ),
        missing_history=missing_history,
        unavailable_reasons=tuple(reasons),
    )


def compute_breadth(
    *,
    universe_size: int,
    one_session_returns: tuple[tuple[str, Decimal], ...],
    above_moving_average: tuple[tuple[str, bool], ...],
) -> BreadthSnapshot:
    """Advancing/declining cohort and fraction above a moving-average
    condition, both deterministically defined from already-computed,
    point-in-time-safe per-security evidence -- never fabricated when
    evidence is thin."""

    reasons: list[str] = []
    advancing = sum(1 for _sid, value in one_session_returns if value > 0)
    declining = sum(1 for _sid, value in one_session_returns if value < 0)
    unchanged = sum(1 for _sid, value in one_session_returns if value == 0)
    evaluable = len(one_session_returns)
    fraction_advancing = (
        _bounded_decimal(Decimal(advancing) / Decimal(evaluable)) if evaluable else None
    )
    if not evaluable:
        reasons.append("NO_SECURITIES_WITH_ONE_SESSION_RETURN_EVIDENCE")
    ma_evaluable = len(above_moving_average)
    fraction_above_ma = (
        _bounded_decimal(
            Decimal(sum(1 for _sid, above in above_moving_average if above)) / Decimal(ma_evaluable)
        )
        if ma_evaluable
        else None
    )
    if not ma_evaluable:
        reasons.append("NO_SECURITIES_WITH_MOVING_AVERAGE_EVIDENCE")
    return BreadthSnapshot(
        universe_size=universe_size,
        evaluable_count=evaluable,
        advancing_count=advancing,
        declining_count=declining,
        unchanged_count=unchanged,
        fraction_advancing=fraction_advancing,
        fraction_above_moving_average=fraction_above_ma,
        reasons=tuple(reasons),
    )


def apply_cross_sectional_ranks(
    securities: tuple[SecurityFeatureVector, ...],
) -> tuple[SecurityFeatureVector, ...]:
    """Second pass: fill in cross-sectional rank fields once every security's
    own (independent) features are known. Only securities with a genuine
    value at the ranking window participate; the rest keep ``None``."""

    return_values = {
        item.stable_security_id: value
        for item in securities
        for window, value in item.returns
        if window == CROSS_SECTIONAL_RETURN_RANK_WINDOW and value is not None
    }
    volatility_values = {
        item.stable_security_id: item.rolling_volatility_20
        for item in securities
        if item.rolling_volatility_20 is not None
    }
    updated: list[SecurityFeatureVector] = []
    for item in securities:
        return_rank = (
            cross_sectional_rank(return_values, instrument_id=item.stable_security_id)
            if item.stable_security_id in return_values
            else None
        )
        volatility_rank = (
            cross_sectional_rank(volatility_values, instrument_id=item.stable_security_id)
            if item.stable_security_id in volatility_values
            else None
        )
        updated.append(
            replace(
                item,
                cross_sectional_return_rank=return_rank,
                cross_sectional_volatility_rank=volatility_rank,
            )
        )
    return tuple(updated)


# --------------------------------------------------------------------------
# Market-regime feature layer v1 (section 6)
# --------------------------------------------------------------------------


def classify_breadth_strength(fraction_advancing: Decimal | None) -> BreadthStrengthLabel:
    if fraction_advancing is None:
        return BreadthStrengthLabel.INSUFFICIENT_EVIDENCE
    if fraction_advancing >= Decimal("0.60"):
        return BreadthStrengthLabel.BROADLY_ADVANCING
    if fraction_advancing <= Decimal("0.40"):
        return BreadthStrengthLabel.BROADLY_DECLINING
    return BreadthStrengthLabel.MIXED


def classify_dispersion(cross_sectional_stdev: Decimal | None) -> DispersionLabel:
    if cross_sectional_stdev is None:
        return DispersionLabel.INSUFFICIENT_EVIDENCE
    if cross_sectional_stdev >= Decimal("0.05"):
        return DispersionLabel.HIGH_DISPERSION
    if cross_sectional_stdev >= Decimal("0.02"):
        return DispersionLabel.MODERATE_DISPERSION
    return DispersionLabel.LOW_DISPERSION


def classify_volatility_regime(annualized_volatility: Decimal | None) -> VolatilityRegimeLabel:
    if annualized_volatility is None:
        return VolatilityRegimeLabel.INSUFFICIENT_EVIDENCE
    if annualized_volatility >= Decimal("0.40"):
        return VolatilityRegimeLabel.HIGH_VOLATILITY
    if annualized_volatility >= Decimal("0.25"):
        return VolatilityRegimeLabel.ELEVATED_VOLATILITY
    if annualized_volatility < Decimal("0.15"):
        return VolatilityRegimeLabel.LOW_VOLATILITY
    return VolatilityRegimeLabel.NORMAL_VOLATILITY


def classify_trend_strength(return_20_session: Decimal | None) -> TrendStrengthLabel:
    if return_20_session is None:
        return TrendStrengthLabel.INSUFFICIENT_EVIDENCE
    if return_20_session >= Decimal("0.12"):
        return TrendStrengthLabel.STRONG_UPTREND
    if return_20_session >= Decimal("0.05"):
        return TrendStrengthLabel.UPTREND
    if return_20_session <= Decimal("-0.12"):
        return TrendStrengthLabel.STRONG_DOWNTREND
    if return_20_session <= Decimal("-0.05"):
        return TrendStrengthLabel.DOWNTREND
    return TrendStrengthLabel.RANGE_BOUND


def classify_correlation_regime(average_correlation: Decimal | None) -> CorrelationRegimeLabel:
    if average_correlation is None:
        return CorrelationRegimeLabel.INSUFFICIENT_EVIDENCE
    if average_correlation >= Decimal("0.60"):
        return CorrelationRegimeLabel.HIGH_CORRELATION
    if average_correlation >= Decimal("0.30"):
        return CorrelationRegimeLabel.MODERATE_CORRELATION
    return CorrelationRegimeLabel.LOW_CORRELATION


def classify_risk_tone(
    *,
    breadth: BreadthStrengthLabel,
    volatility: VolatilityRegimeLabel,
    trend: TrendStrengthLabel,
) -> RiskToneLabel:
    """Derived solely from already-classified evidence available at this
    as_of -- never a date-triggered label. Risk-off dominates (a single
    stressed dimension is enough to withdraw a supportive reading)."""

    insufficient = {
        BreadthStrengthLabel.INSUFFICIENT_EVIDENCE,
        VolatilityRegimeLabel.INSUFFICIENT_EVIDENCE,
        TrendStrengthLabel.INSUFFICIENT_EVIDENCE,
    }
    if breadth in insufficient or volatility in insufficient or trend in insufficient:
        return RiskToneLabel.INSUFFICIENT_EVIDENCE
    risk_off = (
        breadth is BreadthStrengthLabel.BROADLY_DECLINING
        or volatility is VolatilityRegimeLabel.HIGH_VOLATILITY
        or trend in (TrendStrengthLabel.STRONG_DOWNTREND, TrendStrengthLabel.DOWNTREND)
    )
    if risk_off:
        return RiskToneLabel.RISK_OFF
    risk_on = (
        breadth is BreadthStrengthLabel.BROADLY_ADVANCING
        and volatility
        in (VolatilityRegimeLabel.LOW_VOLATILITY, VolatilityRegimeLabel.NORMAL_VOLATILITY)
        and trend in (TrendStrengthLabel.UPTREND, TrendStrengthLabel.STRONG_UPTREND)
    )
    return RiskToneLabel.RISK_ON if risk_on else RiskToneLabel.NEUTRAL


def _confidence(evaluable: int, universe_size: int) -> Decimal:
    if universe_size <= 0:
        return Decimal("0")
    return _bounded_decimal(min(Decimal(1), Decimal(evaluable) / Decimal(universe_size)))


def build_market_regime_snapshot(
    *,
    universe_size: int,
    breadth: BreadthSnapshot,
    cross_sectional_return_stdev: Decimal | None,
    cross_sectional_return_evaluable: int,
    benchmark_annualized_volatility: Decimal | None,
    benchmark_return_20_session: Decimal | None,
    average_pairwise_correlation: Decimal | None,
    correlation_evaluable: int,
) -> MarketRegimeSnapshot:
    """Small deterministic regime-feature layer: six descriptive dimensions,
    each derived only from evidence already computed for this ``as_of`` and
    tagged with the exact evidence and a bounded confidence score -- never a
    date-triggered or hand-picked label."""

    breadth_label = classify_breadth_strength(breadth.fraction_advancing)
    dispersion_label = classify_dispersion(cross_sectional_return_stdev)
    volatility_label = classify_volatility_regime(benchmark_annualized_volatility)
    trend_label = classify_trend_strength(benchmark_return_20_session)
    correlation_label = classify_correlation_regime(average_pairwise_correlation)
    risk_tone_label = classify_risk_tone(
        breadth=breadth_label, volatility=volatility_label, trend=trend_label
    )
    classifications = (
        RegimeClassification(
            dimension=RegimeDimension.RISK_TONE,
            label=risk_tone_label.value,
            confidence=_confidence(breadth.evaluable_count, universe_size),
            evidence=(
                ("breadth_strength", breadth_label.value),
                ("volatility_regime", volatility_label.value),
                ("trend_strength", trend_label.value),
            ),
        ),
        RegimeClassification(
            dimension=RegimeDimension.BREADTH_STRENGTH,
            label=breadth_label.value,
            confidence=_confidence(breadth.evaluable_count, universe_size),
            evidence=(
                (
                    "fraction_advancing",
                    "UNAVAILABLE"
                    if breadth.fraction_advancing is None
                    else str(breadth.fraction_advancing),
                ),
                ("evaluable_count", str(breadth.evaluable_count)),
            ),
        ),
        RegimeClassification(
            dimension=RegimeDimension.DISPERSION,
            label=dispersion_label.value,
            confidence=_confidence(cross_sectional_return_evaluable, universe_size),
            evidence=(
                (
                    "cross_sectional_return_stdev",
                    "UNAVAILABLE"
                    if cross_sectional_return_stdev is None
                    else str(cross_sectional_return_stdev),
                ),
            ),
        ),
        RegimeClassification(
            dimension=RegimeDimension.VOLATILITY_REGIME,
            label=volatility_label.value,
            confidence=Decimal("1")
            if benchmark_annualized_volatility is not None
            else Decimal("0"),
            evidence=(
                (
                    "benchmark_annualized_volatility",
                    "UNAVAILABLE"
                    if benchmark_annualized_volatility is None
                    else str(benchmark_annualized_volatility),
                ),
            ),
        ),
        RegimeClassification(
            dimension=RegimeDimension.TREND_STRENGTH,
            label=trend_label.value,
            confidence=Decimal("1") if benchmark_return_20_session is not None else Decimal("0"),
            evidence=(
                (
                    "benchmark_return_20_session",
                    "UNAVAILABLE"
                    if benchmark_return_20_session is None
                    else str(benchmark_return_20_session),
                ),
            ),
        ),
        RegimeClassification(
            dimension=RegimeDimension.CORRELATION_REGIME,
            label=correlation_label.value,
            confidence=_confidence(correlation_evaluable, universe_size),
            evidence=(
                (
                    "average_pairwise_correlation",
                    "UNAVAILABLE"
                    if average_pairwise_correlation is None
                    else str(average_pairwise_correlation),
                ),
            ),
        ),
    )
    return MarketRegimeSnapshot(rule_version=REGIME_RULE_VERSION, classifications=classifications)


# --------------------------------------------------------------------------
# Outcome isolation (section 9)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutcomeWindowDeclaration:
    feature_cutoff: datetime
    outcome_start: datetime
    outcome_end: datetime
    outcome_metric_id: str

    @classmethod
    def create(
        cls,
        *,
        feature_cutoff: datetime,
        outcome_start: datetime,
        outcome_end: datetime,
        outcome_metric_id: str,
    ) -> OutcomeWindowDeclaration:
        _aware(feature_cutoff, "outcome window feature_cutoff")
        _aware(outcome_start, "outcome window outcome_start")
        _aware(outcome_end, "outcome window outcome_end")
        if outcome_start <= feature_cutoff:
            raise OutcomeIsolationError("OUTCOME_WINDOW_MUST_START_STRICTLY_AFTER_FEATURE_CUTOFF")
        if outcome_end < outcome_start:
            raise OutcomeIsolationError("OUTCOME_WINDOW_END_BEFORE_START")
        if not outcome_metric_id:
            raise OutcomeIsolationError("OUTCOME_METRIC_ID_REQUIRED")
        return cls(
            feature_cutoff=feature_cutoff,
            outcome_start=outcome_start,
            outcome_end=outcome_end,
            outcome_metric_id=outcome_metric_id,
        )


@dataclass(frozen=True, slots=True)
class OutcomeAssessment:
    outcome_id: str
    window: OutcomeWindowDeclaration
    observed_at: datetime
    outcome_value: Decimal | None
    completeness: OutcomeCompletenessState
    sample_bar_count: int


def assess_outcome(
    *,
    outcome_id: str,
    window: OutcomeWindowDeclaration,
    outcome_bars: Sequence[SecuritiesBarEvidence],
    replay_as_of: datetime,
    observed_at: datetime,
) -> OutcomeAssessment:
    """Structurally hard to violate: this is the *only* function that may
    read ``outcome_bars``, it is always invoked at a strictly later replay
    step than the evaluation whose feature cutoff it isolates, and it can
    only ever see evidence with ``available_at`` at or before
    ``min(window.outcome_end, replay_as_of)`` -- the feature generator for
    the same evaluation never receives this sequence at all.

    ``outcome_bars`` is expected to be the *full* retained candidate range
    (unlike every feature-side calculation, which requires an already
    point-in-time-sliced input and rejects anything else loudly) -- gating
    what is currently visible out of that immutable range is this
    function's entire job, so it filters via ``point_in_time_slice`` rather
    than raising on the (normal, expected) evidence beyond the cutoff.
    """

    _aware(replay_as_of, "outcome assessment replay_as_of")
    _aware(observed_at, "outcome assessment observed_at")
    if replay_as_of <= window.feature_cutoff:
        raise OutcomeIsolationError("OUTCOME_ASSESSMENT_REPLAY_CLOCK_HAS_NOT_ADVANCED_PAST_CUTOFF")
    visibility_cutoff = min(window.outcome_end, replay_as_of)
    visible = point_in_time_slice(outcome_bars, as_of=visibility_cutoff)
    windowed = tuple(
        bar for bar in visible if window.outcome_start <= bar.period_end <= window.outcome_end
    )
    if replay_as_of < window.outcome_start:
        return OutcomeAssessment(
            outcome_id=outcome_id,
            window=window,
            observed_at=observed_at,
            outcome_value=None,
            completeness=OutcomeCompletenessState.PENDING,
            sample_bar_count=0,
        )
    if not windowed:
        return OutcomeAssessment(
            outcome_id=outcome_id,
            window=window,
            observed_at=observed_at,
            outcome_value=None,
            completeness=OutcomeCompletenessState.UNAVAILABLE,
            sample_bar_count=0,
        )
    with localcontext() as context:
        context.prec = 28
        value = _bounded_decimal((windowed[-1].close - windowed[0].close) / windowed[0].close)
    complete = replay_as_of >= window.outcome_end
    return OutcomeAssessment(
        outcome_id=outcome_id,
        window=window,
        observed_at=observed_at,
        outcome_value=value,
        completeness=(
            OutcomeCompletenessState.COMPLETE if complete else OutcomeCompletenessState.PARTIAL
        ),
        sample_bar_count=len(windowed),
    )


def assess_dispersion_outcome(
    *,
    outcome_id: str,
    window: OutcomeWindowDeclaration,
    outcome_bars_by_security: tuple[tuple[str, tuple[SecuritiesBarEvidence, ...]], ...],
    replay_as_of: datetime,
    observed_at: datetime,
) -> OutcomeAssessment:
    """Same isolation guarantee as ``assess_outcome``, but the outcome value
    is the cross-sectional dispersion (population stdev) of each security's
    own window return rather than one benchmark's return -- what
    ``evaluate_volatility_regime_vs_subsequent_dispersion`` needs."""

    _aware(replay_as_of, "dispersion outcome replay_as_of")
    _aware(observed_at, "dispersion outcome observed_at")
    if replay_as_of <= window.feature_cutoff:
        raise OutcomeIsolationError("OUTCOME_ASSESSMENT_REPLAY_CLOCK_HAS_NOT_ADVANCED_PAST_CUTOFF")
    if replay_as_of < window.outcome_start:
        return OutcomeAssessment(
            outcome_id=outcome_id,
            window=window,
            observed_at=observed_at,
            outcome_value=None,
            completeness=OutcomeCompletenessState.PENDING,
            sample_bar_count=0,
        )
    visibility_cutoff = min(window.outcome_end, replay_as_of)
    per_security_returns: list[Decimal] = []
    for _stable_security_id, bars in outcome_bars_by_security:
        visible = point_in_time_slice(bars, as_of=visibility_cutoff)
        windowed = tuple(
            bar for bar in visible if window.outcome_start <= bar.period_end <= window.outcome_end
        )
        if len(windowed) < 2:
            continue
        with localcontext() as context:
            context.prec = 28
            per_security_returns.append(
                _bounded_decimal((windowed[-1].close - windowed[0].close) / windowed[0].close)
            )
    if len(per_security_returns) < 2:
        return OutcomeAssessment(
            outcome_id=outcome_id,
            window=window,
            observed_at=observed_at,
            outcome_value=None,
            completeness=OutcomeCompletenessState.UNAVAILABLE,
            sample_bar_count=len(per_security_returns),
        )
    complete = replay_as_of >= window.outcome_end
    return OutcomeAssessment(
        outcome_id=outcome_id,
        window=window,
        observed_at=observed_at,
        outcome_value=_bounded_decimal(_stdev(per_security_returns)),
        completeness=(
            OutcomeCompletenessState.COMPLETE if complete else OutcomeCompletenessState.PARTIAL
        ),
        sample_bar_count=len(per_security_returns),
    )


@dataclass(frozen=True, slots=True)
class OutcomeOverlapWarning:
    method_id: ResearchMethodId
    evaluation_id_a: str
    evaluation_id_b: str
    overlap_days: int


def detect_overlapping_outcomes(
    evaluations: Sequence[HistoricalResearchEvaluation],
) -> tuple[OutcomeOverlapWarning, ...]:
    """Flags -- never silently corrects -- the ordinary daily-rolling-window
    non-independence caveat: two evaluations of the same method whose
    *declared* outcome windows overlap in time are not independent
    observations. Uses the window declared at evaluation creation (available
    immediately), not only once an outcome has actually been assessed."""

    warnings: list[OutcomeOverlapWarning] = []
    by_method: dict[ResearchMethodId, list[HistoricalResearchEvaluation]] = {}
    for item in evaluations:
        if item.outcome_window is None:
            continue
        by_method.setdefault(item.method_id, []).append(item)
    for method_id, group in by_method.items():
        ordered = sorted(group, key=lambda item: item.outcome_window.outcome_start)  # type: ignore[union-attr]
        for earlier, later in pairwise(ordered):
            earlier_end = earlier.outcome_window.outcome_end  # type: ignore[union-attr]
            later_start = later.outcome_window.outcome_start  # type: ignore[union-attr]
            if later_start < earlier_end:
                warnings.append(
                    OutcomeOverlapWarning(
                        method_id=method_id,
                        evaluation_id_a=earlier.evaluation_id,
                        evaluation_id_b=later.evaluation_id,
                        overlap_days=(earlier_end - later_start).days,
                    )
                )
    return tuple(warnings)


# --------------------------------------------------------------------------
# Historical research evaluation seam (section 8)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalResearchEvaluation:
    """``ReplayFrame -> ResearchMethod -> HistoricalResearchEvaluation``.

    Retains method id/version, the exact feature frame it read, the input
    dataset snapshot, as_of, result, blockers, an uncertainty note, and
    evidence lineage. ``outcome`` starts ``None`` and is attached later, at a
    strictly later replay step, by ``attach_outcome`` -- see section 9.
    """

    evaluation_id: str
    replay_run_id: str
    frame_id: str
    dataset_snapshot_id: str
    method_id: ResearchMethodId
    method_version: str
    hypothesis_id: str | None
    as_of: datetime
    session_date: str
    result: tuple[tuple[str, str], ...]
    blockers: tuple[str, ...]
    uncertainty_note: str
    evidence_lineage: tuple[str, ...]
    outcome_window: OutcomeWindowDeclaration | None
    outcome: OutcomeAssessment | None
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        evaluation_id: str,
        replay_run_id: str,
        frame: ReplayFeatureFrame,
        method_id: ResearchMethodId,
        method_version: str,
        hypothesis_id: str | None,
        result: tuple[tuple[str, str], ...],
        blockers: tuple[str, ...],
        uncertainty_note: str,
        outcome_window: OutcomeWindowDeclaration | None,
        created_at: datetime,
    ) -> HistoricalResearchEvaluation:
        _aware(created_at, "evaluation created_at")
        if outcome_window is not None and outcome_window.feature_cutoff != frame.as_of:
            raise OutcomeIsolationError("OUTCOME_WINDOW_CUTOFF_MUST_EQUAL_EVALUATION_AS_OF")
        payload = (
            HISTORICAL_REPLAY_V1,
            replay_run_id,
            frame.frame_id,
            frame.dataset_snapshot_id,
            method_id,
            method_version,
            hypothesis_id,
            frame.as_of,
            result,
            tuple(sorted(blockers)),
            uncertainty_note,
            None
            if outcome_window is None
            else (
                outcome_window.outcome_start,
                outcome_window.outcome_end,
                outcome_window.outcome_metric_id,
            ),
        )
        return cls(
            evaluation_id=deterministic_id("historical_replay_evaluation", payload),
            replay_run_id=replay_run_id,
            frame_id=frame.frame_id,
            dataset_snapshot_id=frame.dataset_snapshot_id,
            method_id=method_id,
            method_version=method_version,
            hypothesis_id=hypothesis_id,
            as_of=frame.as_of,
            session_date=frame.session_date,
            result=result,
            blockers=tuple(sorted(blockers)),
            uncertainty_note=uncertainty_note,
            evidence_lineage=(frame.frame_id, frame.canonical_hash),
            outcome_window=outcome_window,
            outcome=None,
            created_at=created_at,
            canonical_hash=content_digest(payload),
        )

    def attach_outcome(self, outcome: OutcomeAssessment) -> HistoricalResearchEvaluation:
        if self.outcome_window is None:
            raise OutcomeIsolationError("EVALUATION_DOES_NOT_DECLARE_AN_OUTCOME_WINDOW")
        if outcome.window != self.outcome_window:
            raise OutcomeIsolationError("OUTCOME_WINDOW_DOES_NOT_MATCH_DECLARED_EVALUATION_WINDOW")
        if (
            self.outcome is not None
            and self.outcome.completeness is OutcomeCompletenessState.COMPLETE
        ):
            raise OutcomeIsolationError("EVALUATION_OUTCOME_ALREADY_COMPLETE")
        return replace(self, outcome=outcome)


def evaluate_breadth_vs_subsequent_return(
    *,
    evaluation_id: str,
    replay_run_id: str,
    frame: ReplayFeatureFrame,
    hypothesis_id: str | None,
    created_at: datetime,
) -> HistoricalResearchEvaluation:
    """Descriptive engineering research method: today's breadth reading,
    with a subsequent-return outcome to be attached later once genuinely
    available (section 9)."""

    breadth_label = frame.regime.label_for(RegimeDimension.BREADTH_STRENGTH)
    blockers = () if frame.breadth.fraction_advancing is not None else ("BREADTH_UNAVAILABLE",)
    result = (
        (
            "breadth_fraction_advancing",
            "UNAVAILABLE"
            if frame.breadth.fraction_advancing is None
            else str(frame.breadth.fraction_advancing),
        ),
        ("breadth_strength_label", breadth_label),
        ("universe_size", str(frame.breadth.universe_size)),
        ("evaluable_count", str(frame.breadth.evaluable_count)),
    )
    outcome_window = (
        None
        if blockers
        else OutcomeWindowDeclaration.create(
            feature_cutoff=frame.as_of,
            outcome_start=frame.as_of + timedelta(days=1),
            outcome_end=frame.as_of + timedelta(days=OUTCOME_HORIZON_CALENDAR_DAYS),
            outcome_metric_id=SUBSEQUENT_BENCHMARK_RETURN_METRIC,
        )
    )
    return HistoricalResearchEvaluation.create(
        evaluation_id=evaluation_id,
        replay_run_id=replay_run_id,
        frame=frame,
        method_id=ResearchMethodId.BREADTH_VS_SUBSEQUENT_RETURN_V1,
        method_version="1",
        hypothesis_id=hypothesis_id,
        result=result,
        blockers=blockers,
        uncertainty_note="SINGLE_EVALUATION_UNCERTAINTY_ONLY_MEANINGFUL_IN_AGGREGATE_SEE_STATISTICAL_SUMMARY",
        outcome_window=outcome_window,
        created_at=created_at,
    )


def evaluate_volatility_regime_vs_subsequent_dispersion(
    *,
    evaluation_id: str,
    replay_run_id: str,
    frame: ReplayFeatureFrame,
    hypothesis_id: str | None,
    created_at: datetime,
) -> HistoricalResearchEvaluation:
    """Descriptive engineering research method: today's realized-volatility
    regime, with a subsequent cross-sectional-dispersion outcome to be
    attached later (section 9)."""

    volatility_label = frame.regime.label_for(RegimeDimension.VOLATILITY_REGIME)
    dispersion_label = frame.regime.label_for(RegimeDimension.DISPERSION)
    volatility_classification = next(
        item
        for item in frame.regime.classifications
        if item.dimension is RegimeDimension.VOLATILITY_REGIME
    )
    blockers = (
        ()
        if volatility_label != VolatilityRegimeLabel.INSUFFICIENT_EVIDENCE.value
        else ("VOLATILITY_REGIME_UNAVAILABLE",)
    )
    result = (
        ("volatility_regime_label", volatility_label),
        ("current_dispersion_label", dispersion_label),
        *volatility_classification.evidence,
    )
    outcome_window = (
        None
        if blockers
        else OutcomeWindowDeclaration.create(
            feature_cutoff=frame.as_of,
            outcome_start=frame.as_of + timedelta(days=1),
            outcome_end=frame.as_of + timedelta(days=OUTCOME_HORIZON_CALENDAR_DAYS),
            outcome_metric_id=SUBSEQUENT_DISPERSION_METRIC,
        )
    )
    return HistoricalResearchEvaluation.create(
        evaluation_id=evaluation_id,
        replay_run_id=replay_run_id,
        frame=frame,
        method_id=ResearchMethodId.VOLATILITY_REGIME_VS_SUBSEQUENT_DISPERSION_V1,
        method_version="1",
        hypothesis_id=hypothesis_id,
        result=result,
        blockers=blockers,
        uncertainty_note="SINGLE_EVALUATION_UNCERTAINTY_ONLY_MEANINGFUL_IN_AGGREGATE_SEE_STATISTICAL_SUMMARY",
        outcome_window=outcome_window,
        created_at=created_at,
    )


def evaluate_crypto_equity_context_comparison(
    *,
    evaluation_id: str,
    replay_run_id: str,
    frame: ReplayFeatureFrame,
    hypothesis_id: str | None,
    crypto_risk_tone: RiskToneLabel,
    crypto_volatility: VolatilityRegimeLabel,
    crypto_evidence_note: str,
    created_at: datetime,
) -> HistoricalResearchEvaluation:
    """Contemporaneous cross-asset comparison: equity regime vs. crypto
    regime on the same as_of, using genuine retained crypto historical
    evidence. No outcome window -- this method compares two same-day
    readings, it does not forecast."""

    equity_risk_tone = frame.regime.label_for(RegimeDimension.RISK_TONE)
    equity_volatility = frame.regime.label_for(RegimeDimension.VOLATILITY_REGIME)
    agrees = (
        equity_risk_tone == crypto_risk_tone.value
        and equity_risk_tone != RiskToneLabel.INSUFFICIENT_EVIDENCE.value
    )
    result = (
        ("equity_risk_tone", equity_risk_tone),
        ("crypto_risk_tone", crypto_risk_tone.value),
        ("equity_volatility_regime", equity_volatility),
        ("crypto_volatility_regime", crypto_volatility.value),
        ("risk_tone_agrees", str(agrees).lower()),
        ("crypto_evidence_note", crypto_evidence_note),
    )
    return HistoricalResearchEvaluation.create(
        evaluation_id=evaluation_id,
        replay_run_id=replay_run_id,
        frame=frame,
        method_id=ResearchMethodId.CRYPTO_EQUITY_CONTEXT_COMPARISON_V1,
        method_version="1",
        hypothesis_id=hypothesis_id,
        result=result,
        blockers=(),
        uncertainty_note="CONTEMPORANEOUS_COMPARISON_NO_OUTCOME_WINDOW_APPLICABLE",
        outcome_window=None,
        created_at=created_at,
    )


# --------------------------------------------------------------------------
# Research quality controls (section 12)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResearchPeriodDeclaration:
    role: ResearchPeriodRole
    start_date: str
    end_date: str
    locked: bool


@dataclass(frozen=True, slots=True)
class ResearchPeriodPolicy:
    policy_id: str
    replay_run_id: str
    periods: tuple[ResearchPeriodDeclaration, ...]
    minimum_sample_count: int
    created_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        replay_run_id: str,
        periods: tuple[ResearchPeriodDeclaration, ...],
        minimum_sample_count: int,
        created_at: datetime,
    ) -> ResearchPeriodPolicy:
        _aware(created_at, "research period policy created_at")
        if minimum_sample_count < 1:
            raise ResearchQualityError("RESEARCH_PERIOD_MINIMUM_SAMPLE_INVALID")
        if not periods:
            raise ResearchQualityError("RESEARCH_PERIOD_DECLARATION_REQUIRED")
        roles = [item.role for item in periods]
        if len(set(roles)) != len(roles):
            raise ResearchQualityError("RESEARCH_PERIOD_ROLE_DUPLICATE")
        for item in periods:
            if item.start_date > item.end_date:
                raise ResearchQualityError("RESEARCH_PERIOD_RANGE_INVALID")
        ordered = tuple(sorted(periods, key=lambda item: item.start_date))
        for earlier, later in pairwise(ordered):
            if earlier.end_date >= later.start_date:
                raise ResearchQualityError("RESEARCH_PERIOD_RANGES_OVERLAP")
        payload = (
            HISTORICAL_REPLAY_V1,
            replay_run_id,
            tuple((item.role, item.start_date, item.end_date, item.locked) for item in ordered),
            minimum_sample_count,
        )
        return cls(
            policy_id=deterministic_id("historical_replay_research_period_policy", payload),
            replay_run_id=replay_run_id,
            periods=ordered,
            minimum_sample_count=minimum_sample_count,
            created_at=created_at,
            canonical_hash=content_digest(payload),
        )

    def role_for_date(self, session_date: str) -> ResearchPeriodRole | None:
        for item in self.periods:
            if item.start_date <= session_date <= item.end_date:
                return item.role
        return None


def assert_minimum_sample(count: int, minimum: int) -> None:
    if count < minimum:
        raise ResearchQualityError(f"MINIMUM_SAMPLE_NOT_MET:{count}<{minimum}")


def guard_holdout_access(
    policy: ResearchPeriodPolicy, *, role: ResearchPeriodRole, purpose: str
) -> None:
    """A deliberately declared unlocked research period may be iterated on;
    a locked holdout fails closed the instant something other than a final
    evaluation-only read tries to touch it."""

    period = next((item for item in policy.periods if item.role is role), None)
    if period is None:
        raise HoldoutViolationError("RESEARCH_PERIOD_ROLE_NOT_DECLARED")
    if role is ResearchPeriodRole.HOLDOUT and period.locked and purpose != "EVALUATION_ONLY":
        raise HoldoutViolationError("LOCKED_HOLDOUT_CANNOT_BE_USED_FOR_DEVELOPMENT")


# --------------------------------------------------------------------------
# Statistical summaries v1 (section 13)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    lower: Decimal
    upper: Decimal
    method_id: str


@dataclass(frozen=True, slots=True)
class ReplayStatisticalSummary:
    kind: SummaryKind
    group_label: str
    sample_count: int
    mean: Decimal | None
    median: Decimal | None
    stdev: Decimal | None
    percentiles: tuple[tuple[int, Decimal], ...]
    hit_rate: Decimal | None
    cumulative_outcome: Decimal | None
    max_drawdown: Decimal | None
    confidence_interval: ConfidenceInterval | None
    warnings: tuple[str, ...]


def _percentile(ordered: Sequence[Decimal], p: int) -> Decimal:
    if not ordered:
        raise HistoricalReplayError("REPLAY_PERCENTILE_REQUIRES_VALUES")
    with localcontext() as context:
        context.prec = 28
        rank = (Decimal(p) / Decimal(100)) * Decimal(len(ordered))
    index = int(rank.to_integral_value(rounding=ROUND_CEILING)) - 1
    index = max(0, min(len(ordered) - 1, index))
    return ordered[index]


def _cumulative_return(values: Sequence[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        product = Decimal(1)
        for value in values:
            product *= Decimal(1) + value
        return _bounded_decimal(product - 1)


def _max_drawdown_of_series(values: Sequence[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        equity = Decimal(1)
        peak = Decimal(1)
        worst = Decimal(0)
        for value in values:
            equity *= Decimal(1) + value
            peak = max(peak, equity)
            if peak > 0:
                worst = max(worst, (peak - equity) / peak)
        return _bounded_decimal(worst)


def summarize_values(
    values: Sequence[Decimal],
    *,
    kind: SummaryKind,
    group_label: str,
    is_return_series: bool = True,
) -> ReplayStatisticalSummary:
    """Deterministic diagnostics only -- no claim of statistical
    significance beyond a plainly labeled normal-approximation confidence
    interval, and explicit warnings whenever the sample is thin."""

    warnings: list[str] = []
    count = len(values)
    if count == 0:
        return ReplayStatisticalSummary(
            kind=kind,
            group_label=group_label,
            sample_count=0,
            mean=None,
            median=None,
            stdev=None,
            percentiles=(),
            hit_rate=None,
            cumulative_outcome=None,
            max_drawdown=None,
            confidence_interval=None,
            warnings=("NO_SAMPLES",),
        )
    ordered = sorted(values)
    mean = _bounded_decimal(_mean(ordered))
    middle = count // 2
    median = (
        _bounded_decimal(ordered[middle])
        if count % 2 == 1
        else _bounded_decimal((ordered[middle - 1] + ordered[middle]) / 2)
    )
    stdev = _bounded_decimal(_stdev(ordered)) if count >= 2 else None
    percentiles = tuple(
        (p, _bounded_decimal(_percentile(ordered, p))) for p in (10, 25, 50, 75, 90)
    )
    hit_rate = _bounded_decimal(Decimal(sum(1 for value in ordered if value > 0)) / Decimal(count))
    cumulative_outcome = _cumulative_return(values) if is_return_series else None
    drawdown = _max_drawdown_of_series(values) if is_return_series else None
    confidence_interval = None
    if stdev is not None:
        standard_error = _bounded_decimal(stdev / _decimal_sqrt(Decimal(count)))
        confidence_interval = ConfidenceInterval(
            lower=_bounded_decimal(mean - Decimal("1.96") * standard_error),
            upper=_bounded_decimal(mean + Decimal("1.96") * standard_error),
            method_id=STATISTICS_METHOD_NORMAL_APPROXIMATION_V1,
        )
        if count < 30:
            warnings.append("SMALL_SAMPLE_NORMAL_APPROXIMATION_MAY_BE_UNRELIABLE")
    else:
        warnings.append("INSUFFICIENT_SAMPLES_FOR_CONFIDENCE_INTERVAL")
    return ReplayStatisticalSummary(
        kind=kind,
        group_label=group_label,
        sample_count=count,
        mean=mean,
        median=median,
        stdev=stdev,
        percentiles=percentiles,
        hit_rate=hit_rate,
        cumulative_outcome=cumulative_outcome,
        max_drawdown=drawdown,
        confidence_interval=confidence_interval,
        warnings=tuple(warnings),
    )


def group_summaries(
    pairs: tuple[tuple[str, Decimal], ...], *, kind: SummaryKind
) -> tuple[tuple[str, ReplayStatisticalSummary], ...]:
    by_label: dict[str, list[Decimal]] = {}
    for label, value in pairs:
        by_label.setdefault(label, []).append(value)
    return tuple(
        (label, summarize_values(tuple(values), kind=kind, group_label=label))
        for label, values in sorted(by_label.items())
    )


# --------------------------------------------------------------------------
# Historical experiment integration (section 11): lineage only -- the
# hypothesis/dataset-snapshot/experiment-run/outcome records themselves stay
# exactly the existing ``domain.research_evidence`` types, constructed by the
# application layer. This is the join record proving the lineage:
# Hypothesis -> Dataset Snapshot -> Replay Run -> Frames/Evaluations ->
# Experiment Run -> Result.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayExperimentLink:
    link_id: str
    replay_run_id: str
    hypothesis_version_id: str
    research_dataset_snapshot_id: str
    research_experiment_run_id: str
    research_experiment_outcome_id: str | None
    created_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        replay_run_id: str,
        hypothesis_version_id: str,
        research_dataset_snapshot_id: str,
        research_experiment_run_id: str,
        research_experiment_outcome_id: str | None,
        created_at: datetime,
    ) -> ReplayExperimentLink:
        _aware(created_at, "replay experiment link created_at")
        payload = (
            HISTORICAL_REPLAY_V1,
            replay_run_id,
            hypothesis_version_id,
            research_dataset_snapshot_id,
            research_experiment_run_id,
            research_experiment_outcome_id,
        )
        return cls(
            link_id=deterministic_id("historical_replay_experiment_link", payload),
            replay_run_id=replay_run_id,
            hypothesis_version_id=hypothesis_version_id,
            research_dataset_snapshot_id=research_dataset_snapshot_id,
            research_experiment_run_id=research_experiment_run_id,
            research_experiment_outcome_id=research_experiment_outcome_id,
            created_at=created_at,
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Data-quality propagation (section 14)
# --------------------------------------------------------------------------


def historical_replay_warnings() -> tuple[str, ...]:
    return (REPLAY_WARNING, POINT_IN_TIME_UNIVERSE_INCOMPLETE, SURVIVORSHIP_WARNING)
