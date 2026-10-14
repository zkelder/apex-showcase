"""Adapter-free orchestration for quant research methodology and walk-forward
validation.

Composes the *existing* ``application.historical_replay`` foundation (a
completed or in-progress ``ReplayRun`` and its retained
``ReplayFeatureFrame``/``HistoricalResearchEvaluation`` evidence) with the
new ``domain.research_methodology`` controls -- it never re-steps the
replay clock, never recomputes a feature or an outcome, and never rewrites
a previously-persisted evaluation. It adds only the layer above completed
replay evidence: governed research phases, holdout-by-default governance,
parameter/configuration provenance, multiple-testing-aware trial families,
walk-forward folds built by slicing already-retained evaluations, reusable
robustness checks, a retained research conclusion, and a fail-closed
promotion boundary.

No service here has Strategy, Opportunity, PAPER, LIVE, or provider-write
authority; every retained record stays ``authority = "NONE"``. Nothing
here creates a strategy candidate, a PAPER order, or execution authority.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from apex.domain.historical_replay import (
    HistoricalResearchEvaluation,
    OutcomeCompletenessState,
    ReplayFeatureFrame,
    ReplayRun,
    ResearchMethodId,
    ResearchPeriodPolicy,
)
from apex.domain.research_methodology import (
    BenchmarkComparison,
    BenchmarkKind,
    FoldOutcomeState,
    FoldWindow,
    GovernanceIntent,
    HoldoutExposureEvent,
    MultipleTestingCorrectionMethod,
    PromotionEvaluation,
    PromotionPolicy,
    ResearchConclusion,
    ResearchConfiguration,
    ResearchGovernancePolicy,
    ResearchPhase,
    ResearchTrial,
    ResearchTrialFamily,
    RobustnessCheckResult,
    RobustnessCheckRun,
    RobustnessCheckType,
    SampleQualityState,
    TrainingWindowMode,
    TrialSelectionMethod,
    TrialTestResult,
    WalkForwardCompletionState,
    WalkForwardFold,
    WalkForwardRun,
    WalkForwardSummary,
    bound_statistical_decimal,
    build_diagnostics_v2,
    compare_to_benchmark,
    derive_conclusion_state,
    generate_fold_windows,
    resolve_minimum_sample_requirement,
    robustness_asset_cohort_exclusion,
    robustness_cost_stress,
    robustness_leave_one_period_out,
    robustness_missing_data_sensitivity,
    robustness_regime_split,
    robustness_subperiod_split,
    summarize_walk_forward_folds,
    two_sided_z_test_p_value,
)

MAXIMUM_LIST_RESULTS = 5_000
MAXIMUM_FOLDS_PER_INVOCATION = 200
DEFAULT_OUTCOME_HORIZON_SESSIONS = 30


class ResearchMethodologyApplicationError(RuntimeError):
    """A methodology application-layer precondition was violated."""


class MethodologyConflict(ResearchMethodologyApplicationError):
    """A declared identity was reconstructed with genuinely different
    content -- never silently overwritten."""


class MethodologyNotFound(ResearchMethodologyApplicationError):
    pass


class HistoricalReplayEvidenceCollaborator(Protocol):
    """The narrow slice of ``HistoricalReplayRepository`` this module reads
    -- it never writes to replay evidence, and it is satisfied structurally
    by the same repository the replay engine already uses."""

    def get_run(self, replay_run_id: str) -> ReplayRun | None: ...

    def list_evaluations(
        self, replay_run_id: str, *, limit: int
    ) -> tuple[HistoricalResearchEvaluation, ...]: ...

    def list_frames(self, replay_run_id: str, *, limit: int) -> tuple[ReplayFeatureFrame, ...]: ...

    def research_period_policy(self, replay_run_id: str) -> ResearchPeriodPolicy | None: ...


class ResearchMethodologyRepository(Protocol):
    def governance_policy_by_id(
        self, governance_policy_id: str
    ) -> ResearchGovernancePolicy | None: ...
    def save_governance_policy(
        self, policy: ResearchGovernancePolicy
    ) -> ResearchGovernancePolicy: ...
    def governance_policies_for_replay_run(
        self, replay_run_id: str
    ) -> tuple[ResearchGovernancePolicy, ...]: ...

    def save_holdout_exposure(self, event: HoldoutExposureEvent) -> HoldoutExposureEvent: ...
    def holdout_exposures_for_policy(
        self, governance_policy_id: str
    ) -> tuple[HoldoutExposureEvent, ...]: ...

    def trial_by_id(self, trial_id: str) -> ResearchTrial | None: ...
    def save_trial(self, trial: ResearchTrial) -> ResearchTrial: ...
    def trials_for_replay_run(self, replay_run_id: str) -> tuple[ResearchTrial, ...]: ...

    def trial_family_by_id(self, trial_family_id: str) -> ResearchTrialFamily | None: ...
    def save_trial_family(self, family: ResearchTrialFamily) -> ResearchTrialFamily: ...
    def trial_families_for_hypothesis(
        self, hypothesis_id: str
    ) -> tuple[ResearchTrialFamily, ...]: ...

    def walk_forward_run_by_idempotency(self, key: str) -> WalkForwardRun | None: ...
    def save_walk_forward_run(self, run: WalkForwardRun) -> WalkForwardRun: ...
    def get_walk_forward_run(self, walk_forward_run_id: str) -> WalkForwardRun | None: ...
    def list_walk_forward_runs(self) -> tuple[WalkForwardRun, ...]: ...

    def save_fold(self, fold: WalkForwardFold) -> WalkForwardFold: ...
    def folds_for_run(self, walk_forward_run_id: str) -> tuple[WalkForwardFold, ...]: ...

    def robustness_run_by_id(self, robustness_run_id: str) -> RobustnessCheckRun | None: ...
    def save_robustness_run(self, run: RobustnessCheckRun) -> RobustnessCheckRun: ...

    def conclusion_by_id(self, conclusion_id: str) -> ResearchConclusion | None: ...
    def save_conclusion(self, conclusion: ResearchConclusion) -> ResearchConclusion: ...
    def conclusions_for_hypothesis(
        self, hypothesis_version_id: str
    ) -> tuple[ResearchConclusion, ...]: ...

    def promotion_evaluation_by_id(
        self, promotion_evaluation_id: str
    ) -> PromotionEvaluation | None: ...
    def save_promotion_evaluation(self, evaluation: PromotionEvaluation) -> PromotionEvaluation: ...
    def promotion_evaluations_for_conclusion(
        self, conclusion_id: str
    ) -> tuple[PromotionEvaluation, ...]: ...


# --------------------------------------------------------------------------
# Section 2: holdout-by-default governance
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeclareGovernancePolicyCommand:
    hypothesis_version_id: str
    dataset_snapshot_id: str
    replay_run_id: str
    intent: GovernanceIntent | str
    period_policy: ResearchPeriodPolicy
    revision: int
    supersedes_governance_policy_id: str | None
    declared_at: datetime


class DeclareResearchGovernancePolicy:
    def __init__(self, *, repository: ResearchMethodologyRepository) -> None:
        self._repository = repository

    def execute(self, command: DeclareGovernancePolicyCommand) -> ResearchGovernancePolicy:
        policy = ResearchGovernancePolicy.create(
            hypothesis_version_id=command.hypothesis_version_id,
            dataset_snapshot_id=command.dataset_snapshot_id,
            replay_run_id=command.replay_run_id,
            intent=command.intent,
            period_policy=command.period_policy,
            revision=command.revision,
            supersedes_governance_policy_id=command.supersedes_governance_policy_id,
            declared_at=command.declared_at,
        )
        existing = self._repository.governance_policy_by_id(policy.governance_policy_id)
        if existing is not None:
            if existing.canonical_hash != policy.canonical_hash:
                raise MethodologyConflict("GOVERNANCE_POLICY_IDENTITY_CONFLICT")
            return existing
        return self._repository.save_governance_policy(policy)


@dataclass(frozen=True, slots=True)
class RecordHoldoutExposureCommand:
    governance_policy_id: str
    exposed_at: datetime
    actor: str
    purpose: str
    consumed_trial_id: str | None


class RecordHoldoutExposure:
    def __init__(self, *, repository: ResearchMethodologyRepository) -> None:
        self._repository = repository

    def execute(self, command: RecordHoldoutExposureCommand) -> HoldoutExposureEvent:
        policy = self._repository.governance_policy_by_id(command.governance_policy_id)
        if policy is None:
            raise MethodologyNotFound("GOVERNANCE_POLICY_NOT_FOUND")
        event = HoldoutExposureEvent.create(
            governance_policy=policy,
            exposed_at=command.exposed_at,
            actor=command.actor,
            purpose=command.purpose,
            consumed_trial_id=command.consumed_trial_id,
        )
        return self._repository.save_holdout_exposure(event)


# --------------------------------------------------------------------------
# Shared trial-construction helpers (used directly by RecordResearchTrial
# and internally, per fold, by WalkForwardEngine)
# --------------------------------------------------------------------------


def _sample_and_dated_ids(
    evaluations: Sequence[HistoricalResearchEvaluation],
) -> tuple[tuple[Decimal, ...], tuple[tuple[str, str], ...]]:
    values: list[Decimal] = []
    dated_ids: list[tuple[str, str]] = []
    for item in evaluations:
        if (
            item.outcome is not None
            and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
            and item.outcome.outcome_value is not None
        ):
            values.append(item.outcome.outcome_value)
            dated_ids.append((item.session_date, item.evaluation_id))
    return tuple(values), tuple(dated_ids)


def _build_trial(
    *,
    hypothesis_version_id: str,
    dataset_snapshot_id: str,
    replay_run_id: str,
    phase: ResearchPhase,
    market_scope: tuple[str, ...],
    configuration: ResearchConfiguration,
    sample_evaluations: Sequence[HistoricalResearchEvaluation],
    universe_evaluations: Sequence[HistoricalResearchEvaluation],
    benchmark_kind: BenchmarkKind | None,
    created_at: datetime,
    governance_policy: ResearchGovernancePolicy | None,
    outcome_horizon_sessions: int,
    bootstrap_seed: int | None,
    hac_lag: int | None,
    grouped_pairs: tuple[tuple[str, Decimal], ...] = (),
) -> ResearchTrial | None:
    """Returns ``None`` (never a fabricated trial) when the sample slice
    carries no resolved outcomes yet."""

    values, dated_ids = _sample_and_dated_ids(sample_evaluations)
    if not dated_ids:
        return None
    benchmark: BenchmarkComparison | None = None
    benchmark_values: tuple[Decimal, ...] | None = None
    if benchmark_kind is not None:
        benchmark_values, _ = _sample_and_dated_ids(universe_evaluations)
        if benchmark_values:
            sample_mean = sum(values, Decimal()) / Decimal(len(values))
            benchmark = compare_to_benchmark(
                sample_mean=sample_mean,
                benchmark_kind=benchmark_kind,
                benchmark_values=benchmark_values,
            )
    diagnostics = build_diagnostics_v2(
        sample_values=values,
        evaluations=sample_evaluations,
        outcome_horizon_sessions=outcome_horizon_sessions,
        benchmark_values=benchmark_values,
        grouped_pairs=grouped_pairs,
        hac_lag=hac_lag,
        bootstrap_seed=bootstrap_seed,
    )
    return ResearchTrial.create(
        hypothesis_version_id=hypothesis_version_id,
        dataset_snapshot_id=dataset_snapshot_id,
        replay_run_id=replay_run_id,
        phase=phase,
        market_scope=market_scope,
        configuration=configuration,
        dated_evaluation_ids=dated_ids,
        diagnostics=diagnostics,
        benchmark=benchmark,
        created_at=created_at,
        governance_policy=governance_policy,
    )


@dataclass(frozen=True, slots=True)
class RecordResearchTrialCommand:
    hypothesis_version_id: str
    dataset_snapshot_id: str
    replay_run_id: str
    phase: ResearchPhase | str
    market_scope: tuple[str, ...]
    configuration: ResearchConfiguration
    method_id: ResearchMethodId | str
    session_date_start: str
    session_date_end: str
    benchmark_kind: BenchmarkKind | str | None
    created_at: datetime
    governance_policy_id: str | None = None
    outcome_horizon_sessions: int = DEFAULT_OUTCOME_HORIZON_SESSIONS
    bootstrap_seed: int | None = None
    hac_lag: int | None = None


class RecordResearchTrial:
    """Selects evaluations for the declared method within the declared
    session-date range from the already-retained replay evidence, and
    records exactly one ``ResearchTrial`` -- it never re-evaluates or
    recomputes an outcome itself."""

    def __init__(
        self,
        *,
        repository: ResearchMethodologyRepository,
        historical_replay: HistoricalReplayEvidenceCollaborator,
    ) -> None:
        self._repository = repository
        self._historical_replay = historical_replay

    def execute(self, command: RecordResearchTrialCommand) -> ResearchTrial:
        method = ResearchMethodId(command.method_id)
        all_evaluations = self._historical_replay.list_evaluations(
            command.replay_run_id, limit=MAXIMUM_LIST_RESULTS
        )
        universe_evaluations = tuple(item for item in all_evaluations if item.method_id is method)
        sample_evaluations = tuple(
            item
            for item in universe_evaluations
            if command.session_date_start <= item.session_date <= command.session_date_end
        )
        governance_policy: ResearchGovernancePolicy | None = None
        if command.governance_policy_id is not None:
            governance_policy = self._repository.governance_policy_by_id(
                command.governance_policy_id
            )
            if governance_policy is None:
                raise MethodologyNotFound("GOVERNANCE_POLICY_NOT_FOUND")
        trial = _build_trial(
            hypothesis_version_id=command.hypothesis_version_id,
            dataset_snapshot_id=command.dataset_snapshot_id,
            replay_run_id=command.replay_run_id,
            phase=ResearchPhase(command.phase),
            market_scope=command.market_scope,
            configuration=command.configuration,
            sample_evaluations=sample_evaluations,
            universe_evaluations=universe_evaluations,
            benchmark_kind=(
                BenchmarkKind(command.benchmark_kind)
                if command.benchmark_kind is not None
                else None
            ),
            created_at=command.created_at,
            governance_policy=governance_policy,
            outcome_horizon_sessions=command.outcome_horizon_sessions,
            bootstrap_seed=command.bootstrap_seed,
            hac_lag=command.hac_lag,
        )
        if trial is None:
            raise ResearchMethodologyApplicationError("TRIAL_HAS_NO_RESOLVED_OUTCOMES_YET")
        existing = self._repository.trial_by_id(trial.trial_id)
        if existing is not None:
            if existing.canonical_hash != trial.canonical_hash:
                raise MethodologyConflict("TRIAL_IDENTITY_CONFLICT")
            return existing
        return self._repository.save_trial(trial)


@dataclass(frozen=True, slots=True)
class RecordGroupedResearchTrialCommand:
    """For a trial built from a *conditional* sample (e.g. the momentum
    "positive signal" group vs. an unconditional benchmark) rather than one
    method's whole outcome series directly -- ``grouped_pairs`` and
    ``sample_evaluations``/``universe_evaluations`` are supplied pre-built
    by the caller (see ``domain.research_methodology.
    pair_momentum_signal_with_breadth_outcome``), since pairing a signal
    with a reused outcome is a descriptive-method concern, not a generic
    application-layer one."""

    hypothesis_version_id: str
    dataset_snapshot_id: str
    replay_run_id: str
    phase: ResearchPhase | str
    market_scope: tuple[str, ...]
    configuration: ResearchConfiguration
    sample_evaluation_ids: tuple[tuple[str, str], ...]
    sample_values: tuple[Decimal, ...]
    universe_values: tuple[Decimal, ...]
    grouped_pairs: tuple[tuple[str, Decimal], ...]
    benchmark_kind: BenchmarkKind | str | None
    created_at: datetime
    governance_policy_id: str | None = None
    outcome_horizon_sessions: int = DEFAULT_OUTCOME_HORIZON_SESSIONS


class RecordGroupedResearchTrial:
    def __init__(self, *, repository: ResearchMethodologyRepository) -> None:
        self._repository = repository

    def execute(self, command: RecordGroupedResearchTrialCommand) -> ResearchTrial:
        if not command.sample_evaluation_ids:
            raise ResearchMethodologyApplicationError("TRIAL_HAS_NO_RESOLVED_OUTCOMES_YET")
        governance_policy: ResearchGovernancePolicy | None = None
        if command.governance_policy_id is not None:
            governance_policy = self._repository.governance_policy_by_id(
                command.governance_policy_id
            )
            if governance_policy is None:
                raise MethodologyNotFound("GOVERNANCE_POLICY_NOT_FOUND")
        benchmark: BenchmarkComparison | None = None
        kind = BenchmarkKind(command.benchmark_kind) if command.benchmark_kind is not None else None
        if kind is not None and command.universe_values:
            sample_mean = sum(command.sample_values, Decimal()) / Decimal(
                len(command.sample_values)
            )
            benchmark = compare_to_benchmark(
                sample_mean=sample_mean,
                benchmark_kind=kind,
                benchmark_values=command.universe_values,
            )
        diagnostics = build_diagnostics_v2(
            sample_values=command.sample_values,
            evaluations=(),
            outcome_horizon_sessions=command.outcome_horizon_sessions,
            benchmark_values=command.universe_values or None,
            grouped_pairs=command.grouped_pairs,
        )
        trial = ResearchTrial.create(
            hypothesis_version_id=command.hypothesis_version_id,
            dataset_snapshot_id=command.dataset_snapshot_id,
            replay_run_id=command.replay_run_id,
            phase=ResearchPhase(command.phase),
            market_scope=command.market_scope,
            configuration=command.configuration,
            dated_evaluation_ids=command.sample_evaluation_ids,
            diagnostics=diagnostics,
            benchmark=benchmark,
            created_at=command.created_at,
            governance_policy=governance_policy,
        )
        existing = self._repository.trial_by_id(trial.trial_id)
        if existing is not None:
            if existing.canonical_hash != trial.canonical_hash:
                raise MethodologyConflict("TRIAL_IDENTITY_CONFLICT")
            return existing
        return self._repository.save_trial(trial)


# --------------------------------------------------------------------------
# Section 6: multiple-testing awareness
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuildTrialFamilyCommand:
    hypothesis_id: str
    trial_ids: tuple[str, ...]
    candidate_count: int
    asset_count: int
    outcome_definition_count: int
    regime_slice_count: int
    selection_method: TrialSelectionMethod | str
    correction_method: MultipleTestingCorrectionMethod | str
    created_at: datetime


class BuildResearchTrialFamily:
    """Computes each member trial's p-value from its own diagnostics
    (z-test of the trial's sample mean against its recorded benchmark, using
    the *effective*, overlap-adjusted sample count) and applies the
    requested correction across the family."""

    def __init__(self, *, repository: ResearchMethodologyRepository) -> None:
        self._repository = repository

    def execute(self, command: BuildTrialFamilyCommand) -> ResearchTrialFamily:
        trials = []
        for trial_id in command.trial_ids:
            trial = self._repository.trial_by_id(trial_id)
            if trial is None:
                raise MethodologyNotFound(f"TRIAL_NOT_FOUND:{trial_id}")
            trials.append(trial)
        raw_results = tuple(
            TrialTestResult(
                trial.trial_id, trial.configuration.method_id.value, _trial_p_value(trial)
            )
            for trial in trials
        )
        family = ResearchTrialFamily.create(
            hypothesis_id=command.hypothesis_id,
            candidate_count=command.candidate_count,
            asset_count=command.asset_count,
            outcome_definition_count=command.outcome_definition_count,
            regime_slice_count=command.regime_slice_count,
            selection_method=command.selection_method,
            raw_results=raw_results,
            correction_method=command.correction_method,
            created_at=command.created_at,
        )
        existing = self._repository.trial_family_by_id(family.trial_family_id)
        if existing is not None:
            if existing.canonical_hash != family.canonical_hash:
                raise MethodologyConflict("TRIAL_FAMILY_IDENTITY_CONFLICT")
            return existing
        return self._repository.save_trial_family(family)


def _trial_p_value(trial: ResearchTrial) -> Decimal | None:
    if (
        trial.benchmark is None
        or trial.diagnostics.base.mean is None
        or trial.diagnostics.base.stdev is None
    ):
        return None
    return two_sided_z_test_p_value(
        sample_mean=trial.diagnostics.base.mean,
        benchmark_mean=trial.benchmark.benchmark_mean,
        sample_stdev=trial.diagnostics.base.stdev,
        effective_sample_count=trial.diagnostics.overlap.effective_sample_count,
    )


# --------------------------------------------------------------------------
# Section 4: walk-forward evaluation engine
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreateWalkForwardRunCommand:
    replay_run_id: str
    method_id: ResearchMethodId | str
    feature_policy_hash: str
    outcome_metric_id: str
    training_window_sessions: int
    validation_window_sessions: int
    test_window_sessions: int
    step_sessions: int
    window_mode: TrainingWindowMode | str
    minimum_samples: int
    cost_information_state: str
    start_date: str
    end_date: str
    hypothesis_version_id: str | None
    operator_action_id: str
    idempotency_key: str
    created_at: datetime


class CreateWalkForwardRun:
    def __init__(
        self,
        *,
        repository: ResearchMethodologyRepository,
        historical_replay: HistoricalReplayEvidenceCollaborator,
    ) -> None:
        self._repository = repository
        self._historical_replay = historical_replay

    def execute(self, command: CreateWalkForwardRunCommand) -> WalkForwardRun:
        run = self._historical_replay.get_run(command.replay_run_id)
        if run is None:
            raise MethodologyNotFound("REPLAY_RUN_NOT_FOUND")
        draft = WalkForwardRun.draft(
            replay_run_id=command.replay_run_id,
            dataset_snapshot_id=run.dataset_snapshot_id,
            universe=run.universe,
            method_id=command.method_id,
            feature_policy_hash=command.feature_policy_hash,
            outcome_metric_id=command.outcome_metric_id,
            training_window_sessions=command.training_window_sessions,
            validation_window_sessions=command.validation_window_sessions,
            test_window_sessions=command.test_window_sessions,
            step_sessions=command.step_sessions,
            window_mode=command.window_mode,
            minimum_samples=command.minimum_samples,
            cost_information_state=command.cost_information_state,
            start_date=command.start_date,
            end_date=command.end_date,
            hypothesis_version_id=command.hypothesis_version_id,
            operator_action_id=command.operator_action_id,
            idempotency_key=command.idempotency_key,
            created_at=command.created_at,
        )
        existing = self._repository.walk_forward_run_by_idempotency(command.idempotency_key)
        if existing is not None:
            if existing.canonical_input_hash != draft.canonical_input_hash:
                raise MethodologyConflict("WALK_FORWARD_RUN_IDEMPOTENCY_CONFLICT")
            return existing
        return self._repository.save_walk_forward_run(draft)


class WalkForwardEngine:
    """Slices already-retained replay evidence into folds -- it never steps
    the replay clock, never rebuilds a feature frame, and never recomputes
    an outcome. Session dates come from the bound replay run's own retained
    frames, so every fold boundary only ever references a date with
    genuine evidence."""

    def __init__(
        self,
        *,
        repository: ResearchMethodologyRepository,
        historical_replay: HistoricalReplayEvidenceCollaborator,
    ) -> None:
        self._repository = repository
        self._historical_replay = historical_replay

    def run_folds(
        self,
        *,
        run: WalkForwardRun,
        maximum_folds: int,
        market_scope: tuple[str, ...],
        configuration: ResearchConfiguration,
        benchmark_kind: BenchmarkKind | str | None,
        requested_at: datetime,
    ) -> WalkForwardRun:
        if not (1 <= maximum_folds <= MAXIMUM_FOLDS_PER_INVOCATION):
            raise ResearchMethodologyApplicationError("WALK_FORWARD_MAXIMUM_FOLDS_OUT_OF_BOUNDS")
        if run.completion_state is WalkForwardCompletionState.COMPLETE:
            return run
        if run.completion_state is WalkForwardCompletionState.FAILED:
            raise ResearchMethodologyApplicationError("WALK_FORWARD_RUN_FAILED_CANNOT_STEP")
        all_evaluations = self._historical_replay.list_evaluations(
            run.replay_run_id, limit=MAXIMUM_LIST_RESULTS
        )
        method_evaluations = tuple(
            item for item in all_evaluations if item.method_id is run.method_id
        )
        by_session_date: dict[str, list[HistoricalResearchEvaluation]] = {}
        for item in method_evaluations:
            by_session_date.setdefault(item.session_date, []).append(item)
        session_dates = tuple(sorted(by_session_date))
        windows = generate_fold_windows(
            session_dates=session_dates,
            training_window_sessions=run.training_window_sessions,
            validation_window_sessions=run.validation_window_sessions,
            test_window_sessions=run.test_window_sessions,
            step_sessions=run.step_sessions,
            window_mode=run.window_mode,
        )
        pending_windows = tuple(
            item for item in windows if item.fold_ordinal > run.cursor_fold_ordinal
        )
        try:
            for window in pending_windows[:maximum_folds]:
                fold, run = self._build_fold(
                    run=run,
                    window=window,
                    by_session_date=by_session_date,
                    method_evaluations=method_evaluations,
                    market_scope=market_scope,
                    configuration=configuration,
                    benchmark_kind=(
                        BenchmarkKind(benchmark_kind) if benchmark_kind is not None else None
                    ),
                    created_at=requested_at,
                    is_final=window.fold_ordinal == (windows[-1].fold_ordinal if windows else 0),
                )
                self._repository.save_fold(fold)
                run = self._repository.save_walk_forward_run(run)
        except ResearchMethodologyApplicationError:
            raise
        except Exception as error:
            reason = f"WALK_FORWARD_STEP_UNEXPECTED_ERROR:{error}"[:500]
            self._repository.save_walk_forward_run(run.fail(reason=reason))
            raise ResearchMethodologyApplicationError(reason) from error
        return run

    def _build_fold(
        self,
        *,
        run: WalkForwardRun,
        window: FoldWindow,
        by_session_date: dict[str, list[HistoricalResearchEvaluation]],
        method_evaluations: tuple[HistoricalResearchEvaluation, ...],
        market_scope: tuple[str, ...],
        configuration: ResearchConfiguration,
        benchmark_kind: BenchmarkKind | None,
        created_at: datetime,
        is_final: bool,
    ) -> tuple[WalkForwardFold, WalkForwardRun]:
        def _slice(start: str | None, end: str | None) -> tuple[HistoricalResearchEvaluation, ...]:
            if start is None or end is None:
                return ()
            selected: list[HistoricalResearchEvaluation] = []
            for session_date, items in by_session_date.items():
                if start <= session_date <= end:
                    selected.extend(items)
            return tuple(selected)

        train_evaluations = _slice(window.train_start, window.train_end)
        validation_evaluations = _slice(window.validation_start, window.validation_end)
        test_evaluations = _slice(window.test_start, window.test_end)

        train_values, _ = _sample_and_dated_ids(train_evaluations)
        validation_values, _ = _sample_and_dated_ids(validation_evaluations)
        from apex.domain.historical_replay import SummaryKind, summarize_values

        train_summary = (
            summarize_values(train_values, kind=SummaryKind.EXPLORATORY, group_label="fold_train")
            if train_values
            else None
        )
        validation_summary = (
            summarize_values(
                validation_values, kind=SummaryKind.VALIDATION, group_label="fold_validation"
            )
            if validation_values
            else None
        )

        test_complete_count = sum(
            1
            for item in test_evaluations
            if item.outcome is not None
            and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
        )
        test_pending = any(
            item.outcome is None
            or item.outcome.completeness is not OutcomeCompletenessState.COMPLETE
            for item in test_evaluations
        )
        trial: ResearchTrial | None = None
        if not test_evaluations:
            outcome_state = FoldOutcomeState.OUTCOME_PENDING
        elif test_complete_count < run.minimum_samples:
            outcome_state = (
                FoldOutcomeState.OUTCOME_PENDING
                if test_pending
                else FoldOutcomeState.INSUFFICIENT_SAMPLE
            )
        else:
            trial = _build_trial(
                hypothesis_version_id=run.hypothesis_version_id or "",
                dataset_snapshot_id=run.dataset_snapshot_id,
                replay_run_id=run.replay_run_id,
                phase=ResearchPhase.WALK_FORWARD,
                market_scope=market_scope,
                configuration=configuration,
                sample_evaluations=test_evaluations,
                universe_evaluations=method_evaluations,
                benchmark_kind=benchmark_kind,
                created_at=created_at,
                governance_policy=None,
                outcome_horizon_sessions=DEFAULT_OUTCOME_HORIZON_SESSIONS,
                bootstrap_seed=None,
                hac_lag=None,
            )
            outcome_state = (
                FoldOutcomeState.READY if trial is not None else FoldOutcomeState.OUTCOME_PENDING
            )
        if trial is not None:
            existing = self._repository.trial_by_id(trial.trial_id)
            trial = existing if existing is not None else self._repository.save_trial(trial)
        fold = WalkForwardFold.create(
            run=run,
            window=window,
            train_summary=train_summary,
            validation_summary=validation_summary,
            outcome_state=outcome_state,
            test_trial_id=trial.trial_id if trial is not None else None,
            created_at=created_at,
        )
        advanced_run = run.advance(fold_ordinal=window.fold_ordinal, is_final=is_final)
        return fold, advanced_run


@dataclass(frozen=True, slots=True)
class StepWalkForwardRunCommand:
    walk_forward_run_id: str
    maximum_folds: int
    market_scope: tuple[str, ...]
    configuration: ResearchConfiguration
    benchmark_kind: BenchmarkKind | str | None
    requested_at: datetime


class StartWalkForwardRun:
    def __init__(
        self, *, repository: ResearchMethodologyRepository, engine: WalkForwardEngine
    ) -> None:
        self._repository = repository
        self._engine = engine

    def execute(self, command: StepWalkForwardRunCommand) -> WalkForwardRun:
        run = self._repository.get_walk_forward_run(command.walk_forward_run_id)
        if run is None:
            raise MethodologyNotFound("WALK_FORWARD_RUN_NOT_FOUND")
        if run.completion_state is not WalkForwardCompletionState.NOT_STARTED:
            raise ResearchMethodologyApplicationError("WALK_FORWARD_RUN_ALREADY_STARTED")
        return self._engine.run_folds(
            run=run,
            maximum_folds=command.maximum_folds,
            market_scope=command.market_scope,
            configuration=command.configuration,
            benchmark_kind=command.benchmark_kind,
            requested_at=command.requested_at,
        )


class ResumeWalkForwardRun:
    def __init__(
        self, *, repository: ResearchMethodologyRepository, engine: WalkForwardEngine
    ) -> None:
        self._repository = repository
        self._engine = engine

    def execute(self, command: StepWalkForwardRunCommand) -> WalkForwardRun:
        run = self._repository.get_walk_forward_run(command.walk_forward_run_id)
        if run is None:
            raise MethodologyNotFound("WALK_FORWARD_RUN_NOT_FOUND")
        if run.completion_state is WalkForwardCompletionState.NOT_STARTED:
            raise ResearchMethodologyApplicationError("WALK_FORWARD_RUN_NOT_YET_STARTED")
        return self._engine.run_folds(
            run=run,
            maximum_folds=command.maximum_folds,
            market_scope=command.market_scope,
            configuration=command.configuration,
            benchmark_kind=command.benchmark_kind,
            requested_at=command.requested_at,
        )


def build_walk_forward_summary(
    *, repository: ResearchMethodologyRepository, walk_forward_run_id: str
) -> WalkForwardSummary:
    folds = repository.folds_for_run(walk_forward_run_id)
    if not folds:
        raise MethodologyNotFound("WALK_FORWARD_RUN_HAS_NO_FOLDS_YET")
    test_trial_means = []
    for fold in folds:
        if fold.test_trial_id is None:
            continue
        trial = repository.trial_by_id(fold.test_trial_id)
        if trial is not None and trial.diagnostics.base.mean is not None:
            test_trial_means.append((fold.test_trial_id, trial.diagnostics.base.mean))
    return summarize_walk_forward_folds(folds, test_trial_means=tuple(test_trial_means))


# --------------------------------------------------------------------------
# Section 11: robustness checks
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunRobustnessChecksCommand:
    base_trial_id: str
    momentum_lookback_windows: tuple[int, ...]
    momentum_positive_threshold: Decimal
    created_at: datetime


class RunRobustnessChecks:
    """Runs the eight reusable robustness checks (section 11) against a
    base trial's underlying evaluation set. Alternate-lookback and
    neighboring-threshold checks reuse
    ``pair_momentum_signal_with_breadth_outcome`` when the base trial's
    replay run has genuine breadth evaluations available; otherwise they
    honestly report insufficient data rather than fabricating a result."""

    def __init__(
        self,
        *,
        repository: ResearchMethodologyRepository,
        historical_replay: HistoricalReplayEvidenceCollaborator,
    ) -> None:
        self._repository = repository
        self._historical_replay = historical_replay

    def execute(self, command: RunRobustnessChecksCommand) -> RobustnessCheckRun:
        base_trial = self._repository.trial_by_id(command.base_trial_id)
        if base_trial is None:
            raise MethodologyNotFound("BASE_TRIAL_NOT_FOUND")
        if base_trial.diagnostics.base.mean is None:
            raise ResearchMethodologyApplicationError("BASE_TRIAL_HAS_NO_MEAN_TO_PERTURB")
        base_mean = base_trial.diagnostics.base.mean
        all_evaluations = self._historical_replay.list_evaluations(
            base_trial.replay_run_id, limit=MAXIMUM_LIST_RESULTS
        )
        frames = self._historical_replay.list_frames(
            base_trial.replay_run_id, limit=MAXIMUM_LIST_RESULTS
        )
        sample_evaluations = tuple(
            item for item in all_evaluations if item.evaluation_id in set(base_trial.evaluation_ids)
        )
        results: list[RobustnessCheckResult] = []
        results.extend(robustness_subperiod_split(sample_evaluations, base_mean=base_mean))
        results.extend(robustness_regime_split(sample_evaluations, frames, base_mean=base_mean))
        results.extend(robustness_leave_one_period_out(sample_evaluations, base_mean=base_mean))
        results.extend(robustness_asset_cohort_exclusion(sample_evaluations, base_mean=base_mean))
        results.append(robustness_missing_data_sensitivity(sample_evaluations, base_mean=base_mean))
        results.append(robustness_cost_stress())
        results.extend(
            self._momentum_perturbations(
                frames=frames,
                all_evaluations=all_evaluations,
                base_mean=base_mean,
                lookback_windows=command.momentum_lookback_windows,
                threshold=command.momentum_positive_threshold,
            )
        )
        run = RobustnessCheckRun.create(
            base_trial_id=base_trial.trial_id, results=tuple(results), created_at=command.created_at
        )
        existing = self._repository.robustness_run_by_id(run.robustness_run_id)
        if existing is not None:
            return existing
        return self._repository.save_robustness_run(run)

    @staticmethod
    def _momentum_perturbations(
        *,
        frames: tuple[ReplayFeatureFrame, ...],
        all_evaluations: tuple[HistoricalResearchEvaluation, ...],
        base_mean: Decimal,
        lookback_windows: tuple[int, ...],
        threshold: Decimal,
    ) -> tuple[RobustnessCheckResult, ...]:
        from apex.domain.research_methodology import pair_momentum_signal_with_breadth_outcome

        benchmark_id = next((frame.universe[0] for frame in frames if frame.universe), None)
        if benchmark_id is None or not lookback_windows:
            return (
                RobustnessCheckResult(
                    RobustnessCheckType.ALTERNATE_LOOKBACK,
                    "not_applicable",
                    0,
                    None,
                    None,
                    "NO_BENCHMARK_SECURITY_AVAILABLE_IN_RETAINED_FRAMES",
                ),
            )
        results = []
        for lookback in lookback_windows:
            pairs = pair_momentum_signal_with_breadth_outcome(
                frames,
                all_evaluations,
                benchmark_stable_security_id=benchmark_id,
                lookback_window=lookback,
                positive_threshold=threshold,
            )
            positive = [
                value for _date, label, value in pairs if label == "POSITIVE_MOMENTUM_SIGNAL"
            ]
            mean = (
                bound_statistical_decimal(sum(positive, Decimal()) / Decimal(len(positive)))
                if positive
                else None
            )
            results.append(
                RobustnessCheckResult(
                    check_type=RobustnessCheckType.ALTERNATE_LOOKBACK,
                    variant_label=f"lookback_{lookback}",
                    sample_count=len(positive),
                    result_mean=mean,
                    direction_consistent_with_base=(
                        None if mean is None or base_mean == 0 else (mean > 0) == (base_mean > 0)
                    ),
                    note="OK" if positive else "INSUFFICIENT_DATA_FOR_LOOKBACK",
                )
            )
        for offset in (Decimal("-0.01"), Decimal(0), Decimal("0.01")):
            neighbor_threshold = threshold + offset
            pairs = pair_momentum_signal_with_breadth_outcome(
                frames,
                all_evaluations,
                benchmark_stable_security_id=benchmark_id,
                lookback_window=lookback_windows[0],
                positive_threshold=neighbor_threshold,
            )
            positive = [
                value for _date, label, value in pairs if label == "POSITIVE_MOMENTUM_SIGNAL"
            ]
            mean = (
                bound_statistical_decimal(sum(positive, Decimal()) / Decimal(len(positive)))
                if positive
                else None
            )
            results.append(
                RobustnessCheckResult(
                    check_type=RobustnessCheckType.NEIGHBORING_THRESHOLD,
                    variant_label=f"threshold_{neighbor_threshold}",
                    sample_count=len(positive),
                    result_mean=mean,
                    direction_consistent_with_base=(
                        None if mean is None or base_mean == 0 else (mean > 0) == (base_mean > 0)
                    ),
                    note="OK" if positive else "INSUFFICIENT_DATA_FOR_THRESHOLD",
                )
            )
        return tuple(results)


# --------------------------------------------------------------------------
# Sections 12 + 13: research conclusion and promotion boundary
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuildConclusionCommand:
    hypothesis_version_id: str
    dataset_snapshot_id: str
    replay_run_id: str
    training_trial_id: str | None
    validation_trial_id: str | None
    holdout_trial_id: str | None
    walk_forward_run_id: str | None
    robustness_run_id: str | None
    trial_family_id: str | None
    point_in_time_limitations: tuple[str, ...]
    rationale: str
    created_at: datetime
    sample_size_phase: ResearchPhase = ResearchPhase.VALIDATION


class BuildResearchConclusion:
    def __init__(self, *, repository: ResearchMethodologyRepository) -> None:
        self._repository = repository

    def execute(self, command: BuildConclusionCommand) -> ResearchConclusion:
        validation_trial = (
            self._repository.trial_by_id(command.validation_trial_id)
            if command.validation_trial_id
            else None
        )
        holdout_trial = (
            self._repository.trial_by_id(command.holdout_trial_id)
            if command.holdout_trial_id
            else None
        )
        robustness = (
            self._repository.robustness_run_by_id(command.robustness_run_id)
            if command.robustness_run_id
            else None
        )
        family = (
            self._repository.trial_family_by_id(command.trial_family_id)
            if command.trial_family_id
            else None
        )
        walk_forward_fold_count = 0
        if command.walk_forward_run_id is not None:
            walk_forward_fold_count = len(
                self._repository.folds_for_run(command.walk_forward_run_id)
            )

        effective_sample = (
            validation_trial.diagnostics.overlap.effective_sample_count
            if validation_trial is not None
            else Decimal(0)
        )
        requirement = resolve_minimum_sample_requirement(phase=command.sample_size_phase)
        sample_quality = (
            SampleQualityState.SUFFICIENT
            if effective_sample >= requirement.minimum_effective_sample
            else SampleQualityState.INSUFFICIENT_EFFECTIVE_SAMPLE
        )
        multiple_testing_significant = None
        if family is not None:
            multiple_testing_significant = any(
                item.rejected_null
                and item.trial_id in (command.validation_trial_id, command.holdout_trial_id)
                for item in family.corrected_results
            )
        conclusion_phase = (
            ResearchPhase.LOCKED_HOLDOUT
            if holdout_trial is not None
            else (
                ResearchPhase.VALIDATION
                if validation_trial is not None
                else ResearchPhase.EXPLORATORY
            )
        )
        state = derive_conclusion_state(
            sample_quality=sample_quality,
            robustness_summary=robustness.summary if robustness is not None else None,
            validation_beats_benchmark=(
                validation_trial.benchmark.beats_benchmark
                if validation_trial is not None and validation_trial.benchmark is not None
                else None
            ),
            holdout_beats_benchmark=(
                holdout_trial.benchmark.beats_benchmark
                if holdout_trial is not None and holdout_trial.benchmark is not None
                else None
            ),
            multiple_testing_corrected_significant=multiple_testing_significant,
        )
        benchmark_kind = None
        beats_benchmark = None
        reference_trial = holdout_trial or validation_trial
        if reference_trial is not None and reference_trial.benchmark is not None:
            benchmark_kind = reference_trial.benchmark.benchmark_kind
            beats_benchmark = reference_trial.benchmark.beats_benchmark
        blockers: tuple[str, ...] = ()
        if sample_quality is not SampleQualityState.SUFFICIENT:
            blockers = ("INSUFFICIENT_EFFECTIVE_SAMPLE",)
        conclusion = ResearchConclusion.create(
            hypothesis_version_id=command.hypothesis_version_id,
            dataset_snapshot_id=command.dataset_snapshot_id,
            replay_run_id=command.replay_run_id,
            phase=conclusion_phase,
            training_trial_id=command.training_trial_id,
            validation_trial_id=command.validation_trial_id,
            holdout_trial_id=command.holdout_trial_id,
            walk_forward_run_id=command.walk_forward_run_id,
            walk_forward_fold_count=walk_forward_fold_count,
            robustness_run_id=command.robustness_run_id,
            trial_family_id=command.trial_family_id,
            sample_quality_state=sample_quality,
            point_in_time_limitations=command.point_in_time_limitations,
            benchmark_kind=benchmark_kind,
            beats_benchmark=beats_benchmark,
            conclusion_state=state,
            rationale=command.rationale,
            blockers=blockers,
            created_at=command.created_at,
        )
        existing = self._repository.conclusion_by_id(conclusion.conclusion_id)
        if existing is not None:
            return existing
        return self._repository.save_conclusion(conclusion)


@dataclass(frozen=True, slots=True)
class EvaluatePromotionCommand:
    conclusion_id: str
    policy: PromotionPolicy
    lookahead_violation: bool
    created_at: datetime


class EvaluatePromotion:
    def __init__(self, *, repository: ResearchMethodologyRepository) -> None:
        self._repository = repository

    def execute(self, command: EvaluatePromotionCommand) -> PromotionEvaluation:
        conclusion = self._repository.conclusion_by_id(command.conclusion_id)
        if conclusion is None:
            raise MethodologyNotFound("CONCLUSION_NOT_FOUND")
        holdout_exposed = False
        if conclusion.holdout_trial_id is not None:
            for policy in self._repository.governance_policies_for_replay_run(
                conclusion.replay_run_id
            ):
                if self._repository.holdout_exposures_for_policy(policy.governance_policy_id):
                    holdout_exposed = True
                    break
        robustness_completed = conclusion.robustness_run_id is not None
        evaluation = PromotionEvaluation.create(
            conclusion=conclusion,
            policy=command.policy,
            holdout_exposed=holdout_exposed,
            robustness_completed=robustness_completed,
            lookahead_violation=command.lookahead_violation,
            created_at=command.created_at,
        )
        existing = self._repository.promotion_evaluation_by_id(evaluation.promotion_evaluation_id)
        if existing is not None:
            return existing
        return self._repository.save_promotion_evaluation(evaluation)


# --------------------------------------------------------------------------
# Section 14: research audit trail (a bounded read composition -- nothing
# here is separately persisted; every fact already lives in one of the
# tables above or in the existing research-evidence tables)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditTrailEntry:
    kind: str
    entity_id: str
    occurred_at: datetime
    summary: str


@dataclass(frozen=True, slots=True)
class ResearchAuditTrail:
    hypothesis_version_id: str
    entries: tuple[AuditTrailEntry, ...]


def build_research_audit_trail(
    *, repository: ResearchMethodologyRepository, hypothesis_version_id: str
) -> ResearchAuditTrail:
    entries: list[AuditTrailEntry] = []
    for family in repository.trial_families_for_hypothesis(hypothesis_version_id):
        entries.append(
            AuditTrailEntry(
                kind="TRIAL_FAMILY",
                entity_id=family.trial_family_id,
                occurred_at=family.created_at,
                summary=(
                    f"{family.test_count} trial(s) tested of "
                    f"{family.candidate_count} candidate(s); "
                    f"correction={family.correction_method.value}"
                ),
            )
        )
    for conclusion in repository.conclusions_for_hypothesis(hypothesis_version_id):
        entries.append(
            AuditTrailEntry(
                kind="CONCLUSION",
                entity_id=conclusion.conclusion_id,
                occurred_at=conclusion.created_at,
                summary=f"phase={conclusion.phase.value} state={conclusion.conclusion_state.value}",
            )
        )
        for promotion in repository.promotion_evaluations_for_conclusion(conclusion.conclusion_id):
            entries.append(
                AuditTrailEntry(
                    kind="PROMOTION_EVALUATION",
                    entity_id=promotion.promotion_evaluation_id,
                    occurred_at=promotion.created_at,
                    summary=(
                        f"eligible={promotion.eligible} "
                        f"reasons={','.join(promotion.reasons) or 'NONE'}"
                    ),
                )
            )
    ordered = tuple(sorted(entries, key=lambda item: item.occurred_at))
    return ResearchAuditTrail(hypothesis_version_id=hypothesis_version_id, entries=ordered)


def research_methodology_warnings() -> tuple[str, ...]:
    from apex.domain.research_methodology import (
        research_methodology_warnings as _domain_warnings,
    )

    return _domain_warnings()


# --------------------------------------------------------------------------
# In-memory repository (tests only)
# --------------------------------------------------------------------------


class InMemoryResearchMethodologyRepository:
    def __init__(self) -> None:
        self._governance_policies: dict[str, ResearchGovernancePolicy] = {}
        self._holdout_exposures: dict[str, HoldoutExposureEvent] = {}
        self._trials: dict[str, ResearchTrial] = {}
        self._trial_families: dict[str, ResearchTrialFamily] = {}
        self._walk_forward_runs: dict[str, WalkForwardRun] = {}
        self._folds: dict[str, WalkForwardFold] = {}
        self._robustness_runs: dict[str, RobustnessCheckRun] = {}
        self._conclusions: dict[str, ResearchConclusion] = {}
        self._promotion_evaluations: dict[str, PromotionEvaluation] = {}

    def governance_policy_by_id(self, governance_policy_id: str) -> ResearchGovernancePolicy | None:
        return self._governance_policies.get(governance_policy_id)

    def save_governance_policy(self, policy: ResearchGovernancePolicy) -> ResearchGovernancePolicy:
        self._governance_policies[policy.governance_policy_id] = policy
        return policy

    def governance_policies_for_replay_run(
        self, replay_run_id: str
    ) -> tuple[ResearchGovernancePolicy, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._governance_policies.values()
                    if item.replay_run_id == replay_run_id
                ),
                key=lambda item: item.declared_at,
            )
        )

    def save_holdout_exposure(self, event: HoldoutExposureEvent) -> HoldoutExposureEvent:
        self._holdout_exposures[event.exposure_id] = event
        return event

    def holdout_exposures_for_policy(
        self, governance_policy_id: str
    ) -> tuple[HoldoutExposureEvent, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._holdout_exposures.values()
                    if item.governance_policy_id == governance_policy_id
                ),
                key=lambda item: item.exposed_at,
            )
        )

    def trial_by_id(self, trial_id: str) -> ResearchTrial | None:
        return self._trials.get(trial_id)

    def save_trial(self, trial: ResearchTrial) -> ResearchTrial:
        self._trials[trial.trial_id] = trial
        return trial

    def trials_for_replay_run(self, replay_run_id: str) -> tuple[ResearchTrial, ...]:
        return tuple(
            sorted(
                (item for item in self._trials.values() if item.replay_run_id == replay_run_id),
                key=lambda item: item.created_at,
            )
        )

    def trial_family_by_id(self, trial_family_id: str) -> ResearchTrialFamily | None:
        return self._trial_families.get(trial_family_id)

    def save_trial_family(self, family: ResearchTrialFamily) -> ResearchTrialFamily:
        self._trial_families[family.trial_family_id] = family
        return family

    def trial_families_for_hypothesis(self, hypothesis_id: str) -> tuple[ResearchTrialFamily, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._trial_families.values()
                    if item.hypothesis_id == hypothesis_id
                ),
                key=lambda item: item.created_at,
            )
        )

    def walk_forward_run_by_idempotency(self, key: str) -> WalkForwardRun | None:
        return next(
            (item for item in self._walk_forward_runs.values() if item.idempotency_key == key), None
        )

    def save_walk_forward_run(self, run: WalkForwardRun) -> WalkForwardRun:
        self._walk_forward_runs[run.walk_forward_run_id] = run
        return run

    def get_walk_forward_run(self, walk_forward_run_id: str) -> WalkForwardRun | None:
        return self._walk_forward_runs.get(walk_forward_run_id)

    def list_walk_forward_runs(self) -> tuple[WalkForwardRun, ...]:
        return tuple(sorted(self._walk_forward_runs.values(), key=lambda item: item.created_at))

    def save_fold(self, fold: WalkForwardFold) -> WalkForwardFold:
        self._folds[fold.fold_id] = fold
        return fold

    def folds_for_run(self, walk_forward_run_id: str) -> tuple[WalkForwardFold, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._folds.values()
                    if item.walk_forward_run_id == walk_forward_run_id
                ),
                key=lambda item: item.fold_ordinal,
            )
        )

    def robustness_run_by_id(self, robustness_run_id: str) -> RobustnessCheckRun | None:
        return self._robustness_runs.get(robustness_run_id)

    def save_robustness_run(self, run: RobustnessCheckRun) -> RobustnessCheckRun:
        self._robustness_runs[run.robustness_run_id] = run
        return run

    def conclusion_by_id(self, conclusion_id: str) -> ResearchConclusion | None:
        return self._conclusions.get(conclusion_id)

    def save_conclusion(self, conclusion: ResearchConclusion) -> ResearchConclusion:
        self._conclusions[conclusion.conclusion_id] = conclusion
        return conclusion

    def conclusions_for_hypothesis(
        self, hypothesis_version_id: str
    ) -> tuple[ResearchConclusion, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._conclusions.values()
                    if item.hypothesis_version_id == hypothesis_version_id
                ),
                key=lambda item: item.created_at,
            )
        )

    def promotion_evaluation_by_id(
        self, promotion_evaluation_id: str
    ) -> PromotionEvaluation | None:
        return self._promotion_evaluations.get(promotion_evaluation_id)

    def save_promotion_evaluation(self, evaluation: PromotionEvaluation) -> PromotionEvaluation:
        self._promotion_evaluations[evaluation.promotion_evaluation_id] = evaluation
        return evaluation

    def promotion_evaluations_for_conclusion(
        self, conclusion_id: str
    ) -> tuple[PromotionEvaluation, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._promotion_evaluations.values()
                    if item.conclusion_id == conclusion_id
                ),
                key=lambda item: item.created_at,
            )
        )
