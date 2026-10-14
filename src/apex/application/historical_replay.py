"""Adapter-free orchestration for the point-in-time historical replay engine.

Composes the *existing* ``application.historical_market_data`` foundation (a
``HistoricalDatasetSnapshot`` and its point-in-time bar evidence),
``application.securities.SecuritiesCalendarPort`` (genuine exchange-calendar
evidence), and the existing ``domain.research_evidence`` hypothesis/experiment
lifecycle -- it adds only the layer above one point-in-time snapshot: a
durable, checkpointable, resumable replay run that steps a historical clock
forward, builds one deterministic feature frame per step, runs a small set of
descriptive research methods with structural outcome isolation, and can
construct a genuine research experiment from the accumulated evidence.

No service here has Strategy, Opportunity, PAPER, LIVE, or provider-write
authority; every retained record stays ``authority = "NONE"``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from apex.application.historical_market_data import HistoricalMarketDataRepository
from apex.application.real_securities_data import SecuritiesEvidencePort
from apex.application.securities import SecuritiesCalendarPort
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.historical_market_data import (
    HistoricalCaptureUniverseMember,
    HistoricalDatasetSnapshot,
    observation_view_from_crypto_daily_candle,
    point_in_time_slice,
)
from apex.domain.historical_replay import (
    FEATURE_POLICY_HASH_V1,
    FEATURE_POLICY_ID_V1,
    HISTORICAL_OPTIONS_PRICING_UNAVAILABLE,
    RESEARCH_POLICY_HASH_V1,
    RESEARCH_POLICY_ID_V1,
    TRANSACTION_COST_UNAVAILABLE,
    CalendarCoverageState,
    HistoricalReplayError,
    HistoricalResearchEvaluation,
    OutcomeAssessment,
    OutcomeCompletenessState,
    OutcomeOverlapWarning,
    ReplayCadence,
    ReplayCheckpoint,
    ReplayExperimentLink,
    ReplayFeatureFrame,
    ReplayRun,
    ReplayRunCompletionState,
    ReplayStatisticalSummary,
    ResearchMethodId,
    ResearchPeriodPolicy,
    ResearchPeriodRole,
    RiskToneLabel,
    SecurityFeatureVector,
    SummaryKind,
    VolatilityRegimeLabel,
    apply_cross_sectional_ranks,
    assert_minimum_sample,
    assess_dispersion_outcome,
    assess_outcome,
    build_market_regime_snapshot,
    build_security_feature_vector,
    compute_breadth,
    detect_overlapping_outcomes,
    evaluate_breadth_vs_subsequent_return,
    evaluate_crypto_equity_context_comparison,
    evaluate_volatility_regime_vs_subsequent_dispersion,
    group_summaries,
    guard_holdout_access,
    replay_bounds,
    step_replay_clock,
    summarize_values,
)
from apex.domain.historical_replay import (
    historical_replay_warnings as _domain_historical_replay_warnings,
)
from apex.domain.research_evidence import (
    DatasetMemberDecision,
    ExperimentMetric,
    ExperimentOutcomeDecision,
    ExperimentResultArtifact,
    ExperimentState,
    ReproducibilityStatus,
    ResearchDatasetSnapshot,
    ResearchExperimentOutcome,
    ResearchExperimentRun,
    ResearchHypothesisVersion,
)
from apex.domain.research_methodology import ResearchPhase, resolve_minimum_sample_requirement
from apex.domain.securities import SecuritiesBarEvidence, SecuritiesCalendarSession
from apex.domain.values import content_digest, deterministic_id

MAXIMUM_STEPS_PER_INVOCATION = 2_000
MAXIMUM_LIST_RESULTS = 5_000
_LOOKBACK_SESSIONS_FOR_252_WINDOW = 253


class HistoricalReplayApplicationError(RuntimeError):
    """A historical-replay operation is blocked without creating financial state."""


class HistoricalReplayConflict(HistoricalReplayApplicationError):
    """An idempotent or resumable replay operation diverged from its prior input."""


class HistoricalReplayNotFound(HistoricalReplayApplicationError):
    """A referenced run/checkpoint/frame/evaluation/policy does not exist."""


def historical_replay_warnings() -> tuple[str, ...]:
    return _domain_historical_replay_warnings()


# --------------------------------------------------------------------------
# Collaborator protocols
# --------------------------------------------------------------------------


class CryptoHistoricalEvidencePort(Protocol):
    """Satisfied directly by ``PostgresCryptoResearchRepository`` /
    ``InMemoryCryptoResearchRepository`` -- reused unmodified (section 20)."""

    def candles(self, capture_id: str) -> tuple[CryptoDailyCandle, ...]: ...


class HistoricalReplayRepository(Protocol):
    def run_by_idempotency(self, key: str) -> ReplayRun | None: ...
    def save_run(self, run: ReplayRun) -> ReplayRun: ...
    def get_run(self, replay_run_id: str) -> ReplayRun | None: ...
    def list_runs(self) -> tuple[ReplayRun, ...]: ...

    def save_checkpoint(self, checkpoint: ReplayCheckpoint) -> ReplayCheckpoint: ...
    def list_checkpoints(self, replay_run_id: str) -> tuple[ReplayCheckpoint, ...]: ...
    def latest_checkpoint(self, replay_run_id: str) -> ReplayCheckpoint | None: ...

    def frame_by_id(self, frame_id: str) -> ReplayFeatureFrame | None: ...
    def save_frame(self, frame: ReplayFeatureFrame) -> ReplayFeatureFrame: ...
    def list_frames(self, replay_run_id: str, *, limit: int) -> tuple[ReplayFeatureFrame, ...]: ...

    def evaluation_by_id(self, evaluation_id: str) -> HistoricalResearchEvaluation | None: ...
    def save_evaluation(
        self, evaluation: HistoricalResearchEvaluation
    ) -> HistoricalResearchEvaluation: ...
    def apply_outcome(
        self, evaluation_id: str, outcome: OutcomeAssessment
    ) -> HistoricalResearchEvaluation: ...
    def list_evaluations(
        self, replay_run_id: str, *, limit: int
    ) -> tuple[HistoricalResearchEvaluation, ...]: ...

    def save_research_period_policy(self, policy: ResearchPeriodPolicy) -> ResearchPeriodPolicy: ...
    def research_period_policy(self, replay_run_id: str) -> ResearchPeriodPolicy | None: ...

    def save_experiment_link(self, link: ReplayExperimentLink) -> ReplayExperimentLink: ...
    def experiment_links(self, replay_run_id: str) -> tuple[ReplayExperimentLink, ...]: ...


class ResearchEvidenceCollaborator(Protocol):
    """The exact subset of ``application.research_evidence.ResearchEvidenceRepository``
    that constructing a replay-derived experiment needs -- satisfied directly by
    ``PostgresResearchEvidenceRepository``/``InMemoryResearchEvidenceRepository``
    without modification."""

    def hypothesis_version(self, version_id: str) -> ResearchHypothesisVersion | None: ...
    def snapshot(self, snapshot_id: str) -> ResearchDatasetSnapshot | None: ...
    def save_snapshot(self, value: ResearchDatasetSnapshot) -> ResearchDatasetSnapshot: ...
    def run(self, run_id: str) -> ResearchExperimentRun | None: ...
    def save_run(self, value: ResearchExperimentRun) -> ResearchExperimentRun: ...
    def outcome_for_run(self, run_id: str) -> ResearchExperimentOutcome | None: ...
    def save_outcome(self, value: ResearchExperimentOutcome) -> ResearchExperimentOutcome: ...


# --------------------------------------------------------------------------
# Create a replay run (section 1): declare bindings, compute fixed as_of
# bounds, DRAFT / NOT_STARTED.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreateReplayRunCommand:
    dataset_snapshot_id: str
    start_date: str
    end_date: str
    calendar_identity: str | None
    hypothesis_version_id: str | None
    operator_action_id: str
    idempotency_key: str
    created_at: datetime


class CreateReplayRun:
    def __init__(
        self,
        *,
        repository: HistoricalReplayRepository,
        historical_data_repository: HistoricalMarketDataRepository,
        calendar: SecuritiesCalendarPort | None = None,
    ) -> None:
        self._repository = repository
        self._historical_data_repository = historical_data_repository
        self._calendar = calendar

    def execute(self, command: CreateReplayRunCommand) -> ReplayRun:
        snapshot = self._historical_data_repository.snapshot_by_id(command.dataset_snapshot_id)
        if snapshot is None:
            raise HistoricalReplayNotFound("HISTORICAL_DATASET_SNAPSHOT_NOT_FOUND")
        plan = self._historical_data_repository.get_plan(snapshot.plan_id)
        if plan is None:
            raise HistoricalReplayNotFound("HISTORICAL_CAPTURE_PLAN_NOT_FOUND")
        universe = tuple(sorted(member.stable_security_id for member in plan.universe))
        sessions: tuple[SecuritiesCalendarSession, ...] = ()
        if command.calendar_identity is not None:
            if self._calendar is None:
                raise HistoricalReplayApplicationError("REPLAY_RUN_CALENDAR_PORT_UNAVAILABLE")
            sessions = self._calendar.sessions_between(
                calendar_identity=command.calendar_identity,
                start_date=command.start_date,
                end_date=command.end_date,
            )
        try:
            initial_clock, _initial_date, final_clock, _final_date, coverage, warnings = (
                replay_bounds(
                    start_date=command.start_date,
                    end_date=command.end_date,
                    calendar_identity=command.calendar_identity,
                    sessions=sessions,
                )
            )
        except HistoricalReplayError as error:
            raise HistoricalReplayApplicationError(str(error)) from error
        run = ReplayRun.draft(
            dataset_snapshot_id=snapshot.snapshot_id,
            universe=universe,
            start_date=command.start_date,
            end_date=command.end_date,
            cadence=ReplayCadence.DAILY,
            calendar_identity=command.calendar_identity,
            feature_policy_id=FEATURE_POLICY_ID_V1,
            feature_policy_hash=FEATURE_POLICY_HASH_V1,
            research_policy_id=RESEARCH_POLICY_ID_V1,
            research_policy_hash=RESEARCH_POLICY_HASH_V1,
            initial_as_of=initial_clock.as_of,
            final_as_of=final_clock.as_of,
            calendar_coverage_state=coverage,
            calendar_warnings=warnings,
            hypothesis_version_id=command.hypothesis_version_id,
            operator_action_id=command.operator_action_id,
            idempotency_key=command.idempotency_key,
            created_at=command.created_at,
        )
        existing = self._repository.run_by_idempotency(command.idempotency_key)
        if existing is not None:
            if existing.canonical_input_hash != run.canonical_input_hash:
                raise HistoricalReplayConflict("REPLAY_RUN_IDEMPOTENCY_CONFLICT")
            return existing
        return self._repository.save_run(run)


# --------------------------------------------------------------------------
# Bounded, cached, point-in-time evidence access for one run's stepping loop
# --------------------------------------------------------------------------


class _SnapshotEvidence:
    """Loads a dataset snapshot's bars *once* (bounded by its own content-
    addressed batch set), indexed by instrument_id, and reuses that in-memory
    index across every step of one bounded invocation -- never reloading
    every observation for every replay date (section 18)."""

    def __init__(
        self,
        *,
        snapshot: HistoricalDatasetSnapshot,
        universe_members: tuple[HistoricalCaptureUniverseMember, ...],
        evidence: SecuritiesEvidencePort,
    ) -> None:
        self.snapshot = snapshot
        self.instrument_by_stable_id = {
            member.stable_security_id: member.instrument_id for member in universe_members
        }
        bars_by_instrument: dict[str, list[SecuritiesBarEvidence]] = {}
        for batch_id in snapshot.source_batch_ids:
            _instruments, _sessions, batch_bars, _actions = evidence.get_dataset(batch_id)
            for bar in batch_bars:
                bars_by_instrument.setdefault(bar.instrument_id, []).append(bar)
        self._bars_by_instrument = {
            instrument_id: tuple(sorted(bars, key=lambda item: item.period_end))
            for instrument_id, bars in bars_by_instrument.items()
        }

    def bars_as_of(
        self, stable_security_id: str, *, as_of: datetime
    ) -> tuple[SecuritiesBarEvidence, ...]:
        instrument_id = self.instrument_by_stable_id.get(stable_security_id)
        if instrument_id is None:
            return ()
        return point_in_time_slice(self._bars_by_instrument.get(instrument_id, ()), as_of=as_of)

    def raw_bars(self, stable_security_id: str) -> tuple[SecuritiesBarEvidence, ...]:
        """The unfiltered retained range -- only ``assess_outcome`` (which
        performs its own point-in-time gating) may use this."""

        instrument_id = self.instrument_by_stable_id.get(stable_security_id)
        if instrument_id is None:
            return ()
        return self._bars_by_instrument.get(instrument_id, ())


def crypto_return_and_volatility(
    candles: tuple[CryptoDailyCandle, ...], *, as_of: datetime
) -> tuple[Decimal | None, Decimal | None, int]:
    """Plain-Decimal return/volatility from genuine crypto daily candles,
    reusing the shared cross-venue observation shape and gated by the exact
    same ``available_at <= as_of`` rule as every other calculation here."""

    views = sorted(
        (
            observation_view_from_crypto_daily_candle(
                product_id=candle.product_id,
                bucket_start=candle.bucket_start,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                provider=candle.provider,
            )
            for candle in candles
        ),
        key=lambda item: item.event_at,
    )
    visible = tuple(view for view in views if view.available_at <= as_of)
    return_20 = None
    if len(visible) >= 21:
        first, last = visible[-21], visible[-1]
        return_20 = (last.close - first.close) / first.close
    volatility = None
    if len(visible) >= 21:
        closes = [item.close for item in visible[-21:]]
        returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
        average = sum(returns, Decimal(0)) / Decimal(len(returns))
        variance = sum(((value - average) ** 2 for value in returns), Decimal(0)) / Decimal(
            len(returns)
        )
        volatility = variance.sqrt() * Decimal(365).sqrt()
    return return_20, volatility, len(visible)


class HistoricalReplayEngine:
    """The bounded stepping loop shared by start and resume: build one
    ``ReplayFeatureFrame`` and its research evaluations per as_of, re-assess
    any evaluation whose outcome window has come further into view, and
    checkpoint after every step."""

    def __init__(
        self,
        *,
        repository: HistoricalReplayRepository,
        historical_data_repository: HistoricalMarketDataRepository,
        evidence: SecuritiesEvidencePort,
        calendar: SecuritiesCalendarPort | None = None,
        crypto_evidence: CryptoHistoricalEvidencePort | None = None,
    ) -> None:
        self._repository = repository
        self._historical_data_repository = historical_data_repository
        self._evidence = evidence
        self._calendar = calendar
        self._crypto_evidence = crypto_evidence

    def run_steps(
        self,
        *,
        run: ReplayRun,
        maximum_steps: int,
        requested_at: datetime,
        benchmark_stable_security_id: str | None,
        crypto_capture_id: str | None,
    ) -> ReplayRun:
        if not (1 <= maximum_steps <= MAXIMUM_STEPS_PER_INVOCATION):
            raise HistoricalReplayApplicationError("REPLAY_MAXIMUM_STEPS_OUT_OF_BOUNDS")
        if run.completion_state is ReplayRunCompletionState.COMPLETE:
            return run
        if run.completion_state is ReplayRunCompletionState.FAILED:
            raise HistoricalReplayApplicationError("REPLAY_RUN_FAILED_CANNOT_STEP")
        if (
            run.feature_policy_hash != FEATURE_POLICY_HASH_V1
            or run.research_policy_hash != RESEARCH_POLICY_HASH_V1
        ):
            raise HistoricalReplayConflict("REPLAY_RUN_POLICY_IDENTITY_CHANGED_SINCE_DRAFT")
        snapshot = self._historical_data_repository.snapshot_by_id(run.dataset_snapshot_id)
        if snapshot is None:
            raise HistoricalReplayConflict("REPLAY_RUN_DATASET_SNAPSHOT_NO_LONGER_FOUND")
        plan = self._historical_data_repository.get_plan(snapshot.plan_id)
        if plan is None:
            raise HistoricalReplayNotFound("HISTORICAL_CAPTURE_PLAN_NOT_FOUND")
        snapshot_evidence = _SnapshotEvidence(
            snapshot=snapshot, universe_members=plan.universe, evidence=self._evidence
        )
        sessions: tuple[SecuritiesCalendarSession, ...] = ()
        if run.calendar_identity is not None and self._calendar is not None:
            sessions = self._calendar.sessions_between(
                calendar_identity=run.calendar_identity,
                start_date=run.start_date,
                end_date=run.end_date,
            )
        crypto_candles: tuple[CryptoDailyCandle, ...] = ()
        if self._crypto_evidence is not None and crypto_capture_id is not None:
            crypto_candles = self._crypto_evidence.candles(crypto_capture_id)

        # One bounded fetch seeds every evaluation this run has already
        # produced that still awaits an outcome; reassessment below only ever
        # scans this small in-process set, never re-queries the full
        # evaluation history once per step (section 18 -- no full-table
        # rescans inside the stepping loop, so per-step cost stays bounded by
        # how many evaluations are currently pending, not by how many the
        # run has produced in total).
        pending: dict[str, HistoricalResearchEvaluation] = {
            item.evaluation_id: item
            for item in self._repository.list_evaluations(
                run.replay_run_id, limit=MAXIMUM_LIST_RESULTS
            )
            if item.outcome_window is not None
            and (
                item.outcome is None
                or item.outcome.completeness is not OutcomeCompletenessState.COMPLETE
            )
        }
        try:
            for _ in range(maximum_steps):
                # mypy narrows `run.completion_state` from the guards above and
                # does not widen it back across the loop's own reassignment of
                # `run` below; this check is genuinely reachable once a prior
                # iteration's `run.advance()` completes the run mid-loop.
                if run.completion_state is ReplayRunCompletionState.COMPLETE:  # type: ignore[comparison-overlap]
                    break
                clock, session_date, _coverage, _warnings = step_replay_clock(
                    after_date=run.cursor_session_date,
                    start_date=run.start_date,
                    end_date=run.end_date,
                    calendar_identity=run.calendar_identity,
                    sessions=sessions,
                )
                if clock is None or session_date is None:
                    raise HistoricalReplayConflict(
                        "REPLAY_RUN_CALENDAR_EVIDENCE_CHANGED_SINCE_DRAFT"
                    )
                frame, evaluations = self._build_step(
                    run=run,
                    snapshot=snapshot,
                    snapshot_evidence=snapshot_evidence,
                    as_of=clock.as_of,
                    session_date=session_date,
                    benchmark_stable_security_id=benchmark_stable_security_id,
                    crypto_candles=crypto_candles,
                    created_at=requested_at,
                )
                saved_frame = self._repository.save_frame(frame)
                saved_evaluations = tuple(
                    self._repository.save_evaluation(item) for item in evaluations
                )
                for item in saved_evaluations:
                    if item.outcome_window is not None:
                        pending[item.evaluation_id] = item
                reassessed_ids = self._reassess_pending_outcomes(
                    pending=pending,
                    run=run,
                    snapshot_evidence=snapshot_evidence,
                    benchmark_stable_security_id=benchmark_stable_security_id,
                    as_of=clock.as_of,
                    requested_at=requested_at,
                )
                run = run.advance(next_as_of=clock.as_of, session_date=session_date)
                checkpoint = ReplayCheckpoint.create(
                    run=run,
                    as_of=clock.as_of,
                    session_date=session_date,
                    frame_ids=(saved_frame.frame_id,),
                    evaluation_ids=tuple(item.evaluation_id for item in saved_evaluations)
                    + reassessed_ids,
                    created_at=requested_at,
                )
                self._repository.save_checkpoint(checkpoint)
                run = self._repository.save_run(run)
        except HistoricalReplayApplicationError:
            # A conflict/precondition failure means *this invocation's*
            # assumptions were wrong (stale policy, moved dataset, changed
            # calendar evidence) -- not that the run itself is broken. No
            # steps were mutated this iteration, so leave the run resumable.
            raise
        except Exception as error:
            reason = f"REPLAY_STEP_UNEXPECTED_ERROR:{error}"[:500]
            self._repository.save_run(run.fail(reason=reason))
            raise HistoricalReplayApplicationError(reason) from error
        return run

    def _build_step(
        self,
        *,
        run: ReplayRun,
        snapshot: HistoricalDatasetSnapshot,
        snapshot_evidence: _SnapshotEvidence,
        as_of: datetime,
        session_date: str,
        benchmark_stable_security_id: str | None,
        crypto_candles: tuple[CryptoDailyCandle, ...],
        created_at: datetime,
    ) -> tuple[ReplayFeatureFrame, tuple[HistoricalResearchEvaluation, ...]]:
        benchmark_bars = (
            snapshot_evidence.bars_as_of(benchmark_stable_security_id, as_of=as_of)
            if benchmark_stable_security_id is not None
            else ()
        )
        vectors: list[SecurityFeatureVector] = []
        excluded = 0
        excluded_reasons: set[str] = set()
        for stable_id in run.universe:
            if stable_id not in snapshot_evidence.instrument_by_stable_id:
                excluded += 1
                excluded_reasons.add("SECURITY_NOT_IN_DATASET_SNAPSHOT_UNIVERSE")
                continue
            bars = snapshot_evidence.bars_as_of(stable_id, as_of=as_of)
            vector = build_security_feature_vector(
                stable_security_id=stable_id,
                instrument_id=snapshot_evidence.instrument_by_stable_id[stable_id],
                bars=bars,
                benchmark_bars=benchmark_bars or None,
                as_of=as_of,
                session_date=session_date,
            )
            if not bars:
                excluded += 1
                excluded_reasons.add("NO_OBSERVATIONS_AT_OR_BEFORE_AS_OF")
            vectors.append(vector)
        ranked = apply_cross_sectional_ranks(tuple(vectors))

        one_session_returns = tuple(
            (item.stable_security_id, value)
            for item in ranked
            for window, value in item.returns
            if window == 1 and value is not None
        )
        above_ma = tuple(
            (item.stable_security_id, item.above_moving_average_20)
            for item in ranked
            if item.above_moving_average_20 is not None
        )
        breadth = compute_breadth(
            universe_size=len(run.universe),
            one_session_returns=one_session_returns,
            above_moving_average=above_ma,
        )
        twenty_session_returns = tuple(
            value
            for item in ranked
            for window, value in item.returns
            if window == 20 and value is not None
        )
        volatilities = tuple(
            item.rolling_volatility_20 for item in ranked if item.rolling_volatility_20 is not None
        )
        correlations = tuple(
            item.benchmark_correlation_60
            for item in ranked
            if item.benchmark_correlation_60 is not None
        )
        benchmark_vector = next(
            (item for item in ranked if item.stable_security_id == benchmark_stable_security_id),
            None,
        )
        benchmark_return_20 = (
            None if benchmark_vector is None else dict(benchmark_vector.returns).get(20)
        )
        benchmark_volatility = (
            None if benchmark_vector is None else benchmark_vector.rolling_volatility_20
        )
        regime = build_market_regime_snapshot(
            universe_size=len(run.universe),
            breadth=breadth,
            cross_sectional_return_stdev=_population_stdev(twenty_session_returns),
            cross_sectional_return_evaluable=len(twenty_session_returns),
            benchmark_annualized_volatility=(
                benchmark_volatility
                if benchmark_volatility is not None
                else _population_mean(volatilities)
            ),
            benchmark_return_20_session=(
                benchmark_return_20
                if benchmark_return_20 is not None
                else _population_mean(twenty_session_returns)
            ),
            average_pairwise_correlation=_population_mean(correlations),
            correlation_evaluable=len(correlations),
        )
        point_in_time_limitations = (
            snapshot.point_in_time_universe_state.value,
            snapshot.survivorship_state.value,
            HISTORICAL_OPTIONS_PRICING_UNAVAILABLE,
        )
        data_quality_flags = tuple(
            sorted(
                {
                    *excluded_reasons,
                    *(("SPARSE_HISTORY",) if any(item.missing_history for item in ranked) else ()),
                }
            )
        )
        frame_payload = (
            run.replay_run_id,
            snapshot.snapshot_id,
            as_of,
            session_date,
            run.feature_policy_hash,
        )
        frame = ReplayFeatureFrame(
            frame_id=deterministic_id("historical_replay_feature_frame", frame_payload),
            replay_run_id=run.replay_run_id,
            dataset_snapshot_id=snapshot.snapshot_id,
            as_of=as_of,
            session_date=session_date,
            feature_policy_id=run.feature_policy_id,
            feature_policy_hash=run.feature_policy_hash,
            universe=run.universe,
            securities=ranked,
            breadth=breadth,
            regime=regime,
            excluded_evidence_count=excluded,
            excluded_evidence_reasons=tuple(sorted(excluded_reasons)),
            point_in_time_limitations=point_in_time_limitations,
            data_quality_flags=data_quality_flags,
            created_at=created_at,
            canonical_hash=content_digest(frame_payload),
        )
        evaluations = [
            evaluate_breadth_vs_subsequent_return(
                evaluation_id="",
                replay_run_id=run.replay_run_id,
                frame=frame,
                hypothesis_id=run.hypothesis_version_id,
                created_at=created_at,
            ),
            evaluate_volatility_regime_vs_subsequent_dispersion(
                evaluation_id="",
                replay_run_id=run.replay_run_id,
                frame=frame,
                hypothesis_id=run.hypothesis_version_id,
                created_at=created_at,
            ),
        ]
        if crypto_candles:
            crypto_return, crypto_volatility, crypto_sample = crypto_return_and_volatility(
                crypto_candles, as_of=as_of
            )
            crypto_volatility_label = (
                VolatilityRegimeLabel.INSUFFICIENT_EVIDENCE
                if crypto_volatility is None
                else classify_crypto_volatility(crypto_volatility)
            )
            crypto_risk_tone = classify_crypto_risk_tone(
                return_20_session=crypto_return, volatility_label=crypto_volatility_label
            )
            evaluations.append(
                evaluate_crypto_equity_context_comparison(
                    evaluation_id="",
                    replay_run_id=run.replay_run_id,
                    frame=frame,
                    hypothesis_id=run.hypothesis_version_id,
                    crypto_risk_tone=crypto_risk_tone,
                    crypto_volatility=crypto_volatility_label,
                    crypto_evidence_note=f"GENUINE_CRYPTO_DAILY_CANDLES_VISIBLE_COUNT={crypto_sample}",
                    created_at=created_at,
                )
            )
        return frame, tuple(evaluations)

    def _reassess_pending_outcomes(
        self,
        *,
        pending: dict[str, HistoricalResearchEvaluation],
        run: ReplayRun,
        snapshot_evidence: _SnapshotEvidence,
        benchmark_stable_security_id: str | None,
        as_of: datetime,
        requested_at: datetime,
    ) -> tuple[str, ...]:
        due = tuple(
            item
            for item in pending.values()
            if item.outcome_window is not None and item.outcome_window.outcome_start <= as_of
        )
        updated: list[str] = []
        for evaluation in due:
            window = evaluation.outcome_window
            assert window is not None
            if (
                evaluation.method_id
                is ResearchMethodId.VOLATILITY_REGIME_VS_SUBSEQUENT_DISPERSION_V1
            ):
                per_security_bars = tuple(
                    (stable_id, snapshot_evidence.raw_bars(stable_id)) for stable_id in run.universe
                )
                assessment = assess_dispersion_outcome(
                    outcome_id=deterministic_id(
                        "historical_replay_outcome", (evaluation.evaluation_id, as_of)
                    ),
                    window=window,
                    outcome_bars_by_security=per_security_bars,
                    replay_as_of=as_of,
                    observed_at=requested_at,
                )
            else:
                outcome_bars = (
                    snapshot_evidence.raw_bars(benchmark_stable_security_id)
                    if benchmark_stable_security_id is not None
                    else ()
                )
                assessment = assess_outcome(
                    outcome_id=deterministic_id(
                        "historical_replay_outcome", (evaluation.evaluation_id, as_of)
                    ),
                    window=window,
                    outcome_bars=outcome_bars,
                    replay_as_of=as_of,
                    observed_at=requested_at,
                )
            if assessment.completeness is OutcomeCompletenessState.PENDING:
                continue
            self._repository.apply_outcome(evaluation.evaluation_id, assessment)
            updated.append(evaluation.evaluation_id)
            if assessment.completeness is OutcomeCompletenessState.COMPLETE:
                del pending[evaluation.evaluation_id]
            else:
                pending[evaluation.evaluation_id] = evaluation.attach_outcome(assessment)
        return tuple(sorted(updated))


def _population_mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal(0)) / Decimal(len(values))


def _population_stdev(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    average = _population_mean(values)
    assert average is not None
    variance = sum(((value - average) ** 2 for value in values), Decimal(0)) / Decimal(len(values))
    return variance.sqrt()


def classify_crypto_volatility(annualized_volatility: Decimal) -> VolatilityRegimeLabel:
    if annualized_volatility >= Decimal("0.80"):
        return VolatilityRegimeLabel.HIGH_VOLATILITY
    if annualized_volatility >= Decimal("0.55"):
        return VolatilityRegimeLabel.ELEVATED_VOLATILITY
    if annualized_volatility < Decimal("0.35"):
        return VolatilityRegimeLabel.LOW_VOLATILITY
    return VolatilityRegimeLabel.NORMAL_VOLATILITY


def classify_crypto_risk_tone(
    *, return_20_session: Decimal | None, volatility_label: VolatilityRegimeLabel
) -> RiskToneLabel:
    """Single-asset simplification of ``classify_risk_tone`` (crypto has no
    cross-sectional breadth of its own here): volatility and trend only."""

    if return_20_session is None or volatility_label is VolatilityRegimeLabel.INSUFFICIENT_EVIDENCE:
        return RiskToneLabel.INSUFFICIENT_EVIDENCE
    if volatility_label is VolatilityRegimeLabel.HIGH_VOLATILITY or return_20_session <= Decimal(
        "-0.10"
    ):
        return RiskToneLabel.RISK_OFF
    if volatility_label in (
        VolatilityRegimeLabel.LOW_VOLATILITY,
        VolatilityRegimeLabel.NORMAL_VOLATILITY,
    ) and return_20_session >= Decimal("0.10"):
        return RiskToneLabel.RISK_ON
    return RiskToneLabel.NEUTRAL


# --------------------------------------------------------------------------
# Start / resume commands (sections 1, 10)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepReplayRunCommand:
    replay_run_id: str
    maximum_steps: int
    requested_at: datetime
    benchmark_stable_security_id: str | None = None
    crypto_capture_id: str | None = None


class StartReplayRun:
    def __init__(
        self, *, repository: HistoricalReplayRepository, engine: HistoricalReplayEngine
    ) -> None:
        self._repository = repository
        self._engine = engine

    def execute(self, command: StepReplayRunCommand) -> ReplayRun:
        run = self._repository.get_run(command.replay_run_id)
        if run is None:
            raise HistoricalReplayNotFound("REPLAY_RUN_NOT_FOUND")
        if run.completion_state is not ReplayRunCompletionState.NOT_STARTED:
            raise HistoricalReplayApplicationError("REPLAY_RUN_ALREADY_STARTED_USE_RESUME")
        return self._engine.run_steps(
            run=run,
            maximum_steps=command.maximum_steps,
            requested_at=command.requested_at,
            benchmark_stable_security_id=command.benchmark_stable_security_id,
            crypto_capture_id=command.crypto_capture_id,
        )


class ResumeReplayRun:
    def __init__(
        self, *, repository: HistoricalReplayRepository, engine: HistoricalReplayEngine
    ) -> None:
        self._repository = repository
        self._engine = engine

    def execute(self, command: StepReplayRunCommand) -> ReplayRun:
        run = self._repository.get_run(command.replay_run_id)
        if run is None:
            raise HistoricalReplayNotFound("REPLAY_RUN_NOT_FOUND")
        if run.completion_state is ReplayRunCompletionState.NOT_STARTED:
            raise HistoricalReplayApplicationError("REPLAY_RUN_NOT_YET_STARTED_USE_START")
        if run.completion_state is ReplayRunCompletionState.COMPLETE:
            return run  # idempotent no-op: nothing left to resume
        return self._engine.run_steps(
            run=run,
            maximum_steps=command.maximum_steps,
            requested_at=command.requested_at,
            benchmark_stable_security_id=command.benchmark_stable_security_id,
            crypto_capture_id=command.crypto_capture_id,
        )


# --------------------------------------------------------------------------
# Research-quality period policy (section 12)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeclareResearchPeriodPolicyCommand:
    replay_run_id: str
    policy: ResearchPeriodPolicy


class DeclareResearchPeriodPolicy:
    def __init__(self, *, repository: HistoricalReplayRepository) -> None:
        self._repository = repository

    def execute(self, command: DeclareResearchPeriodPolicyCommand) -> ResearchPeriodPolicy:
        run = self._repository.get_run(command.replay_run_id)
        if run is None:
            raise HistoricalReplayNotFound("REPLAY_RUN_NOT_FOUND")
        existing = self._repository.research_period_policy(command.replay_run_id)
        if existing is not None:
            if existing.canonical_hash != command.policy.canonical_hash:
                raise HistoricalReplayConflict("RESEARCH_PERIOD_POLICY_IDENTITY_CONFLICT")
            return existing
        return self._repository.save_research_period_policy(command.policy)


class ListEvaluationsForPeriod:
    """The one guarded read path for period-scoped evaluations: a locked
    holdout fails closed here, structurally, rather than relying on every
    caller to remember to check (section 12)."""

    def __init__(self, *, repository: HistoricalReplayRepository) -> None:
        self._repository = repository

    def execute(
        self, *, replay_run_id: str, role: ResearchPeriodRole, purpose: str, limit: int
    ) -> tuple[HistoricalResearchEvaluation, ...]:
        policy = self._repository.research_period_policy(replay_run_id)
        if policy is None:
            raise HistoricalReplayApplicationError("RESEARCH_PERIOD_POLICY_NOT_DECLARED")
        guard_holdout_access(policy, role=role, purpose=purpose)
        bounded_limit = min(limit, MAXIMUM_LIST_RESULTS)
        evaluations = self._repository.list_evaluations(replay_run_id, limit=bounded_limit)
        return tuple(
            item for item in evaluations if policy.role_for_date(item.session_date) is role
        )


# --------------------------------------------------------------------------
# Historical experiment integration (section 11): construct a genuine
# ResearchDatasetSnapshot/ResearchExperimentRun/ResearchExperimentOutcome
# from accumulated replay evaluations -- the *existing* research_evidence
# domain types, unmodified.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConstructReplayExperimentCommand:
    replay_run_id: str
    code_revision: str
    actor: str
    decided_at: datetime


class ConstructReplayExperiment:
    def __init__(
        self,
        *,
        repository: HistoricalReplayRepository,
        historical_data_repository: HistoricalMarketDataRepository,
        research_evidence: ResearchEvidenceCollaborator,
    ) -> None:
        self._repository = repository
        self._historical_data_repository = historical_data_repository
        self._research_evidence = research_evidence

    def execute(self, command: ConstructReplayExperimentCommand) -> ReplayExperimentLink:
        run = self._repository.get_run(command.replay_run_id)
        if run is None:
            raise HistoricalReplayNotFound("REPLAY_RUN_NOT_FOUND")
        if run.hypothesis_version_id is None:
            raise HistoricalReplayApplicationError("REPLAY_RUN_HAS_NO_DECLARED_HYPOTHESIS")
        hypothesis = self._research_evidence.hypothesis_version(run.hypothesis_version_id)
        if hypothesis is None:
            raise HistoricalReplayNotFound("RESEARCH_HYPOTHESIS_VERSION_NOT_FOUND")
        snapshot = self._historical_data_repository.snapshot_by_id(run.dataset_snapshot_id)
        if snapshot is None:
            raise HistoricalReplayNotFound("HISTORICAL_DATASET_SNAPSHOT_NOT_FOUND")
        evaluations = self._repository.list_evaluations(
            run.replay_run_id, limit=MAXIMUM_LIST_RESULTS
        )
        # Replaces the old flat placeholder of 5 with the explicit, versioned
        # sample-size policy (quant research methodology milestone, section
        # 8). This experiment-linkage mechanism predates research phases, so
        # it is bound to the loosest (EXPLORATORY) floor -- a basic sanity
        # gate, not a governed promotion-track requirement.
        experiment_requirement = resolve_minimum_sample_requirement(phase=ResearchPhase.EXPLORATORY)
        assert_minimum_sample(len(evaluations), experiment_requirement.minimum_raw_sample)

        decisions = tuple(
            DatasetMemberDecision(
                canonical_security_id=stable_id,
                current_symbol=stable_id,
                eligible=True,
                reasons=(snapshot.point_in_time_universe_state.value,),
                execution_capability="NOT_APPLICABLE",
                execution_reasons=(),
                security_hash=content_digest(stable_id),
            )
            for stable_id in run.universe
        )
        research_snapshot = ResearchDatasetSnapshot.create(
            snapshot_id=deterministic_id(
                "replay_research_dataset_snapshot", (run.replay_run_id, run.dataset_snapshot_id)
            ),
            component_capture_ids=(run.dataset_snapshot_id,),
            evidence_class=snapshot.evidence_class,
            normalized_security_ids=run.universe,
            source_receipt_ids=(run.dataset_snapshot_id,),
            source_versions=(run.feature_policy_id,),
            parser_versions=(run.research_policy_id,),
            universe_id=f"historical-replay-universe:{run.replay_run_id}",
            universe_policy_id=run.universe_policy.value,
            universe_policy_hash=content_digest((run.universe_policy.value, run.universe)),
            total_considered=len(run.universe),
            eligible_count=len(run.universe),
            decisions=decisions,
            created_at=command.decided_at,
        )
        prior_snapshot = self._research_evidence.snapshot(research_snapshot.snapshot_id)
        if (
            prior_snapshot is not None
            and prior_snapshot.manifest_hash != research_snapshot.manifest_hash
        ):
            raise HistoricalReplayConflict("REPLAY_RESEARCH_DATASET_SNAPSHOT_IDENTITY_CONFLICT")
        research_snapshot = prior_snapshot or self._research_evidence.save_snapshot(
            research_snapshot
        )

        outcome_values = tuple(
            item.outcome.outcome_value
            for item in evaluations
            if item.outcome is not None
            and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
            and item.outcome.outcome_value is not None
        )
        summary = summarize_values(outcome_values, kind=SummaryKind.EXPLORATORY, group_label="all")
        overlap_warnings = detect_overlapping_outcomes(evaluations)
        state = ExperimentState.COMPLETED if outcome_values else ExperimentState.FAILED
        warnings: tuple[str, ...] = (
            *historical_replay_warnings(),
            TRANSACTION_COST_UNAVAILABLE,
            *(("OVERLAPPING_OUTCOME_WINDOWS_PRESENT_SEE_LINEAGE",) if overlap_warnings else ()),
        )
        metrics = (
            (
                ExperimentMetric("evaluation_count", str(len(evaluations))),
                ExperimentMetric("complete_outcome_count", str(len(outcome_values))),
                ExperimentMetric(
                    "mean_outcome", "UNAVAILABLE" if summary.mean is None else str(summary.mean)
                ),
                ExperimentMetric(
                    "hit_rate", "UNAVAILABLE" if summary.hit_rate is None else str(summary.hit_rate)
                ),
                ExperimentMetric("overlapping_outcome_window_count", str(len(overlap_warnings))),
            )
            if state is ExperimentState.COMPLETED
            else ()
        )
        run_record = ResearchExperimentRun.create(
            run_id=deterministic_id(
                "replay_research_experiment_run",
                (run.replay_run_id, research_snapshot.snapshot_id, command.code_revision),
            ),
            hypothesis_version_id=hypothesis.hypothesis_version_id,
            snapshot_id=research_snapshot.snapshot_id,
            executor_version=RESEARCH_POLICY_ID_V1,
            configuration=(
                ("replay_run_id", run.replay_run_id),
                ("dataset_snapshot_id", run.dataset_snapshot_id),
                ("feature_policy_hash", run.feature_policy_hash),
                ("research_policy_hash", run.research_policy_hash),
            ),
            feature_policy_versions=(run.feature_policy_id, run.research_policy_id),
            code_revision=command.code_revision,
            deterministic_seed=0,
            executed_at=command.decided_at,
            state=state,
            warnings=warnings,
            metrics=metrics,
            artifacts=(
                ExperimentResultArtifact(
                    "HISTORICAL_REPLAY_EVALUATIONS",
                    content_digest(tuple(sorted(item.evaluation_id for item in evaluations))),
                    (
                        (
                            "evaluation_ids",
                            tuple(sorted(item.evaluation_id for item in evaluations)),
                        ),
                    ),
                ),
            ),
            evidence_class=research_snapshot.evidence_class,
            reproducibility_status=ReproducibilityStatus.REPRODUCIBLE,
            failure_reason=None
            if state is ExperimentState.COMPLETED
            else "NO_COMPLETE_OUTCOMES_YET",
        )
        prior_run = self._research_evidence.run(run_record.run_id)
        if prior_run is not None:
            if prior_run.canonical_hash != run_record.canonical_hash:
                raise HistoricalReplayConflict("REPLAY_RESEARCH_EXPERIMENT_RUN_IDENTITY_CONFLICT")
            run_record = prior_run
        else:
            run_record = self._research_evidence.save_run(run_record)

        outcome_id: str | None = None
        if state is ExperimentState.COMPLETED:
            outcome_record = ResearchExperimentOutcome.create(
                outcome_id=deterministic_id(
                    "replay_research_experiment_outcome", (run_record.run_id,)
                ),
                run=run_record,
                decision=ExperimentOutcomeDecision.DEFERRED,
                decided_at=command.decided_at,
                evidence_hashes=(run_record.result_hash,),
                threshold_rules=("NO_STRATEGY_PROMOTION_THIS_MILESTONE_DESCRIPTIVE_METHOD_ONLY",),
                warnings=warnings,
                actor=command.actor,
                reason=(
                    "Descriptive historical-replay research method; not evaluated for "
                    "strategy promotion this milestone."
                ),
            )
            prior_outcome = self._research_evidence.outcome_for_run(run_record.run_id)
            outcome_record = prior_outcome or self._research_evidence.save_outcome(outcome_record)
            outcome_id = outcome_record.outcome_id

        link = ReplayExperimentLink.create(
            replay_run_id=run.replay_run_id,
            hypothesis_version_id=hypothesis.hypothesis_version_id,
            research_dataset_snapshot_id=research_snapshot.snapshot_id,
            research_experiment_run_id=run_record.run_id,
            research_experiment_outcome_id=outcome_id,
            created_at=command.decided_at,
        )
        return self._repository.save_experiment_link(link)


# --------------------------------------------------------------------------
# Historical research summary (statistics v1 + regime distribution +
# data-quality + holdout state) -- a read-only aggregation over already-
# persisted, immutable evidence. Never persisted itself; recomputed fresh.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalResearchSummary:
    replay_run_id: str
    evaluation_count: int
    complete_outcome_count: int
    pending_outcome_count: int
    overlapping_outcome_windows: tuple[OutcomeOverlapWarning, ...]
    overall_summary: ReplayStatisticalSummary
    summary_by_method: tuple[tuple[str, ReplayStatisticalSummary], ...]
    summary_by_regime_label: tuple[tuple[str, ReplayStatisticalSummary], ...]
    regime_label_distribution: tuple[tuple[str, int], ...]
    data_quality_flags: tuple[str, ...]
    holdout_declared: bool
    warnings: tuple[str, ...]


def build_historical_research_summary(
    *, repository: HistoricalReplayRepository, replay_run_id: str
) -> HistoricalResearchSummary:
    run = repository.get_run(replay_run_id)
    if run is None:
        raise HistoricalReplayNotFound("REPLAY_RUN_NOT_FOUND")
    evaluations = repository.list_evaluations(replay_run_id, limit=MAXIMUM_LIST_RESULTS)
    frames = repository.list_frames(replay_run_id, limit=MAXIMUM_LIST_RESULTS)
    complete = tuple(
        item
        for item in evaluations
        if item.outcome is not None
        and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
    )
    pending = tuple(
        item
        for item in evaluations
        if item.outcome_window is not None
        and (
            item.outcome is None
            or item.outcome.completeness is not OutcomeCompletenessState.COMPLETE
        )
    )
    outcome_values = tuple(
        item.outcome.outcome_value
        for item in complete
        if item.outcome is not None and item.outcome.outcome_value is not None
    )
    overall = summarize_values(outcome_values, kind=SummaryKind.EXPLORATORY, group_label="all")
    by_method_pairs = tuple(
        (item.method_id.value, item.outcome.outcome_value)
        for item in complete
        if item.outcome is not None and item.outcome.outcome_value is not None
    )
    by_regime_pairs = tuple(
        (
            next(
                (
                    classification.label
                    for classification in _frame_for(frames, item.frame_id).regime.classifications
                    if classification.dimension.value == "RISK_TONE"
                ),
                "UNKNOWN",
            ),
            item.outcome.outcome_value,
        )
        for item in complete
        if item.outcome is not None
        and item.outcome.outcome_value is not None
        and _frame_for(frames, item.frame_id) is not None
    )
    regime_counts: dict[str, int] = {}
    for frame in frames:
        for classification in frame.regime.classifications:
            if classification.dimension.value != "RISK_TONE":
                continue
            regime_counts[classification.label] = regime_counts.get(classification.label, 0) + 1
    data_quality_flags = tuple(
        sorted({flag for frame in frames for flag in frame.data_quality_flags})
    )
    policy = repository.research_period_policy(replay_run_id)
    warnings = list(historical_replay_warnings())
    if run.calendar_coverage_state is CalendarCoverageState.CALENDAR_EVIDENCE_UNAVAILABLE:
        warnings.append("CALENDAR_EVIDENCE_UNAVAILABLE_FOR_ONE_OR_MORE_STEPS")
    return HistoricalResearchSummary(
        replay_run_id=replay_run_id,
        evaluation_count=len(evaluations),
        complete_outcome_count=len(complete),
        pending_outcome_count=len(pending),
        overlapping_outcome_windows=detect_overlapping_outcomes(evaluations),
        overall_summary=overall,
        summary_by_method=group_summaries(by_method_pairs, kind=SummaryKind.EXPLORATORY),
        summary_by_regime_label=group_summaries(by_regime_pairs, kind=SummaryKind.EXPLORATORY),
        regime_label_distribution=tuple(sorted(regime_counts.items())),
        data_quality_flags=data_quality_flags,
        holdout_declared=policy is not None
        and any(item.role is ResearchPeriodRole.HOLDOUT for item in policy.periods),
        warnings=tuple(warnings),
    )


def _frame_for(frames: tuple[ReplayFeatureFrame, ...], frame_id: str) -> ReplayFeatureFrame:
    found = next((item for item in frames if item.frame_id == frame_id), None)
    if found is None:
        raise HistoricalReplayNotFound(f"REPLAY_FEATURE_FRAME_NOT_FOUND:{frame_id}")
    return found


# --------------------------------------------------------------------------
# Reference in-memory repository (tests and any future in-process caller)
# --------------------------------------------------------------------------


class InMemoryHistoricalReplayRepository:
    def __init__(self) -> None:
        self._runs: dict[str, ReplayRun] = {}
        self._runs_by_idempotency: dict[str, str] = {}
        self._checkpoints: dict[str, ReplayCheckpoint] = {}
        self._frames: dict[str, ReplayFeatureFrame] = {}
        self._evaluations: dict[str, HistoricalResearchEvaluation] = {}
        self._period_policies: dict[str, ResearchPeriodPolicy] = {}
        self._experiment_links: dict[str, ReplayExperimentLink] = {}

    def run_by_idempotency(self, key: str) -> ReplayRun | None:
        run_id = self._runs_by_idempotency.get(key)
        return None if run_id is None else self._runs[run_id]

    def save_run(self, run: ReplayRun) -> ReplayRun:
        self._runs[run.replay_run_id] = run
        self._runs_by_idempotency[run.idempotency_key] = run.replay_run_id
        return run

    def get_run(self, replay_run_id: str) -> ReplayRun | None:
        return self._runs.get(replay_run_id)

    def list_runs(self) -> tuple[ReplayRun, ...]:
        return tuple(sorted(self._runs.values(), key=lambda item: item.created_at))

    def save_checkpoint(self, checkpoint: ReplayCheckpoint) -> ReplayCheckpoint:
        self._checkpoints[checkpoint.checkpoint_id] = checkpoint
        return checkpoint

    def list_checkpoints(self, replay_run_id: str) -> tuple[ReplayCheckpoint, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._checkpoints.values()
                    if item.replay_run_id == replay_run_id
                ),
                key=lambda item: item.as_of,
            )
        )

    def latest_checkpoint(self, replay_run_id: str) -> ReplayCheckpoint | None:
        checkpoints = self.list_checkpoints(replay_run_id)
        return checkpoints[-1] if checkpoints else None

    def frame_by_id(self, frame_id: str) -> ReplayFeatureFrame | None:
        return self._frames.get(frame_id)

    def save_frame(self, frame: ReplayFeatureFrame) -> ReplayFeatureFrame:
        self._frames.setdefault(frame.frame_id, frame)
        return self._frames[frame.frame_id]

    def list_frames(self, replay_run_id: str, *, limit: int) -> tuple[ReplayFeatureFrame, ...]:
        matching = sorted(
            (item for item in self._frames.values() if item.replay_run_id == replay_run_id),
            key=lambda item: item.as_of,
        )
        return tuple(matching[:limit])

    def evaluation_by_id(self, evaluation_id: str) -> HistoricalResearchEvaluation | None:
        return self._evaluations.get(evaluation_id)

    def save_evaluation(
        self, evaluation: HistoricalResearchEvaluation
    ) -> HistoricalResearchEvaluation:
        self._evaluations.setdefault(evaluation.evaluation_id, evaluation)
        return self._evaluations[evaluation.evaluation_id]

    def apply_outcome(
        self, evaluation_id: str, outcome: OutcomeAssessment
    ) -> HistoricalResearchEvaluation:
        current = self._evaluations.get(evaluation_id)
        if current is None:
            raise HistoricalReplayNotFound("REPLAY_EVALUATION_NOT_FOUND")
        updated = current.attach_outcome(outcome)
        self._evaluations[evaluation_id] = updated
        return updated

    def list_evaluations(
        self, replay_run_id: str, *, limit: int
    ) -> tuple[HistoricalResearchEvaluation, ...]:
        matching = sorted(
            (item for item in self._evaluations.values() if item.replay_run_id == replay_run_id),
            key=lambda item: item.as_of,
        )
        return tuple(matching[:limit])

    def save_research_period_policy(self, policy: ResearchPeriodPolicy) -> ResearchPeriodPolicy:
        self._period_policies.setdefault(policy.replay_run_id, policy)
        return self._period_policies[policy.replay_run_id]

    def research_period_policy(self, replay_run_id: str) -> ResearchPeriodPolicy | None:
        return self._period_policies.get(replay_run_id)

    def save_experiment_link(self, link: ReplayExperimentLink) -> ReplayExperimentLink:
        self._experiment_links.setdefault(link.link_id, link)
        return self._experiment_links[link.link_id]

    def experiment_links(self, replay_run_id: str) -> tuple[ReplayExperimentLink, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._experiment_links.values()
                    if item.replay_run_id == replay_run_id
                ),
                key=lambda item: item.created_at,
            )
        )
