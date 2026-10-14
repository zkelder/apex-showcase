"""Governed experiment families and immutable all-attempt research evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from apex.domain.research_integrity import (
    CSCV_POLICY_ID,
    DSR_POLICY_ID,
    EFFECTIVE_TRIAL_POLICY_ID,
    INTEGRITY_POLICY_ID,
    PSR_POLICY_ID,
    RESEARCH_METRICS_POLICY_ID,
    CscvPartitionResult,
    ResearchFrequency,
    ResearchIntegrityError,
    ReturnObservation,
    cscv_probability_of_backtest_overfitting,
    deflated_sharpe_ratio,
    estimate_effective_trial_count,
    normalize_return_metrics,
    research_integrity_reasons,
    selection_stability,
)
from apex.domain.values import content_digest, deterministic_id


class ResearchFamilyState(StrEnum):
    DRAFT = "DRAFT"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELED = "CANCELED"
    INVALIDATED = "INVALIDATED"


class ResearchTrialSourceType(StrEnum):
    CANONICAL_BASELINE = "CANONICAL_BASELINE"
    SECURITIES_RESEARCH = "SECURITIES_RESEARCH"
    SECURITIES_SIMULATION = "SECURITIES_SIMULATION"
    STRATEGY_LAB_TRAINING = "STRATEGY_LAB_TRAINING"
    STRATEGY_LAB_CANDIDATE = "STRATEGY_LAB_CANDIDATE"
    STRATEGY_LAB_SHADOW = "STRATEGY_LAB_SHADOW"
    EXTERNAL_SIMULATOR = "EXTERNAL_SIMULATOR"
    FIXTURE_VALIDATION = "FIXTURE_VALIDATION"


class ResearchTrialState(StrEnum):
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED_INTEGRITY = "REJECTED_INTEGRITY"
    CANCELED = "CANCELED"


class ResearchSeriesType(StrEnum):
    GROSS_RETURN = "GROSS_RETURN"
    NET_RETURN = "NET_RETURN"
    BENCHMARK_RETURN = "BENCHMARK_RETURN"
    EXCESS_RETURN = "EXCESS_RETURN"
    EQUITY_CURVE = "EQUITY_CURVE"
    DRAWDOWN = "DRAWDOWN"


class MultipleTestingState(StrEnum):
    COMPLETED = "COMPLETED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"
    INCOMPARABLE_TRIALS = "INCOMPARABLE_TRIALS"
    FAILED = "FAILED"


class ResearchRecommendation(StrEnum):
    CONTINUE_RESEARCH = "CONTINUE_RESEARCH"
    REJECT_SELECTION_BIAS = "REJECT_SELECTION_BIAS"
    ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW = "ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW"
    REQUIRE_MORE_INDEPENDENT_EVIDENCE = "REQUIRE_MORE_INDEPENDENT_EVIDENCE"
    INVALID_RESEARCH_FAMILY = "INVALID_RESEARCH_FAMILY"


class ResearchIntegrityApplicationError(RuntimeError):
    """The governed research boundary rejected an operation."""


class ResearchIdempotencyConflict(ResearchIntegrityApplicationError):
    """An idempotency key was replayed with different canonical input."""


@dataclass(frozen=True, slots=True)
class ResearchExperimentFamily:
    experiment_id: str
    name: str
    vertical: str
    research_family: str
    research_question: str
    target_label_identity: str
    benchmark_identities: tuple[str, ...]
    data_scope_identities: tuple[str, ...]
    data_scope_hashes: tuple[str, ...]
    universe_hash: str
    period_start: str
    period_end: str
    cutoff_at: datetime
    feature_schema_hashes: tuple[str, ...]
    permitted_families: tuple[str, ...]
    parameter_space: dict[str, object]
    parameter_space_hash: str
    maximum_trials: int
    maximum_feature_variants: int
    maximum_model_variants: int
    maximum_simulator_variants: int
    evaluation_policy_id: str
    evaluation_policy_hash: str
    cost_execution_assumptions: dict[str, object]
    cost_execution_hash: str
    frequency: ResearchFrequency
    state: ResearchFamilyState
    created_at: datetime
    opened_at: datetime | None
    closed_at: datetime | None
    operator_action_id: str
    operator_reason: str
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class ResearchTrialAttempt:
    trial_id: str
    experiment_id: str
    parent_trial_id: str | None
    ordinal: int
    source_type: ResearchTrialSourceType
    source_identity: str
    source_hash: str
    strategy_model_family: str
    configuration: dict[str, object]
    configuration_hash: str
    feature_set_hash: str
    dataset_hash: str
    simulator_identity: str
    simulator_version: str
    execution_cost_policy_hash: str
    random_seed: int | None
    state: ResearchTrialState
    started_at: datetime
    completed_at: datetime | None
    result_identity: str | None
    result_hash: str | None
    failure_reason: str | None
    selected_for_comparison: bool
    integrity_blocked: bool
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str | None


@dataclass(frozen=True, slots=True)
class ResearchReturnSeriesArtifact:
    artifact_id: str
    trial_id: str
    series_type: ResearchSeriesType
    frequency: ResearchFrequency
    start_period: str
    end_period: str
    observation_count: int
    relative_path: str
    sha256: str
    byte_size: int
    return_policy_hash: str
    missing_period_policy: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class ResearchMetricSnapshot:
    metric_id: str
    trial_id: str
    observation_count: int
    independent_group_count: int
    gross_return: Decimal
    net_return: Decimal
    mean_periodic_return: Decimal
    periodic_volatility: Decimal
    sharpe_ratio: Decimal | None
    sortino_ratio: Decimal | None
    maximum_drawdown: Decimal
    turnover: Decimal
    hit_rate: Decimal
    brier_score: Decimal | None
    log_loss: Decimal | None
    benchmark_differences: dict[str, object]
    skewness: Decimal | None
    excess_kurtosis: Decimal | None
    autocorrelations: tuple[Decimal, ...]
    effective_sample_size: Decimal
    concentration: dict[str, object]
    integrity_warnings: tuple[str, ...]
    calculation_policy_id: str
    calculation_policy_version: str
    canonical_input_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class ResearchSelectionEvent:
    selection_id: str
    experiment_id: str
    selected_trial_id: str
    candidate_trial_ids: tuple[str, ...]
    candidate_set_hash: str
    criterion: str
    direction: str
    selected_at: datetime
    action_identity: str
    reason: str
    canonical_input_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class ResearchCrossValidationPartition:
    partition_id: str
    experiment_id: str
    policy_id: str
    policy_version: str
    ordinal: int
    training_group_ids: tuple[str, ...]
    training_group_hash: str
    validation_group_ids: tuple[str, ...]
    validation_group_hash: str
    test_group_ids: tuple[str, ...]
    test_group_hash: str
    selected_trial_id: str
    validation_result_id: str
    test_result_id: str
    selected_test_rank_percentile: Decimal
    below_median: bool
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class ResearchMultipleTestingEvaluation:
    evaluation_id: str
    experiment_id: str
    cutoff_at: datetime
    included_trial_ids: tuple[str, ...]
    included_trial_set_hash: str
    completed_trial_count: int
    failed_rejected_trial_count: int
    unique_configuration_count: int
    feature_variant_count: int
    model_variant_count: int
    simulator_variant_count: int
    raw_comparable_trial_count: int
    effective_independent_trials: Decimal
    overlap_coverage: Decimal
    best_observed_trial_id: str | None
    best_observed_metric: Decimal | None
    benchmark_metric: Decimal
    probabilistic_sharpe_ratio: Decimal | None
    deflated_sharpe_ratio: Decimal | None
    expected_maximum_sharpe: Decimal | None
    minimum_track_record_length: Decimal | None
    probability_backtest_overfitting: Decimal | None
    rank_stability: dict[str, object]
    best_fold_excluded: dict[str, object]
    best_period_excluded: dict[str, object]
    concentration: dict[str, object]
    state: MultipleTestingState
    recommendation: ResearchRecommendation
    reason_codes: tuple[str, ...]
    calculation_policy_id: str
    calculation_policy_version: str
    canonical_input_hash: str
    canonical_result_hash: str
    created_at: datetime


class ResearchReturnArtifactStore(Protocol):
    def write(
        self,
        *,
        experiment_id: str,
        trial_id: str,
        series_type: ResearchSeriesType,
        frequency: ResearchFrequency,
        observations: Sequence[ReturnObservation],
        return_policy_hash: str,
        missing_period_policy: str,
    ) -> ResearchReturnSeriesArtifact: ...

    def read(self, artifact: ResearchReturnSeriesArtifact) -> tuple[ReturnObservation, ...]: ...


class ResearchIntegrityRepository(Protocol):
    def family_by_idempotency(self, key: str) -> ResearchExperimentFamily | None: ...
    def family(self, experiment_id: str) -> ResearchExperimentFamily | None: ...
    def families(self) -> tuple[ResearchExperimentFamily, ...]: ...
    def save_family(self, family: ResearchExperimentFamily) -> ResearchExperimentFamily: ...
    def update_family_state(
        self,
        experiment_id: str,
        *,
        expected: ResearchFamilyState,
        replacement: ResearchExperimentFamily,
    ) -> ResearchExperimentFamily: ...
    def trial_by_idempotency(self, experiment_id: str, key: str) -> ResearchTrialAttempt | None: ...
    def reserve_trial(self, trial: ResearchTrialAttempt) -> ResearchTrialAttempt: ...
    def finish_trial(self, trial: ResearchTrialAttempt) -> ResearchTrialAttempt: ...
    def trial(self, trial_id: str) -> ResearchTrialAttempt | None: ...
    def trials(self, experiment_id: str) -> tuple[ResearchTrialAttempt, ...]: ...
    def save_artifact(
        self, artifact: ResearchReturnSeriesArtifact
    ) -> ResearchReturnSeriesArtifact: ...
    def artifacts(self, trial_id: str) -> tuple[ResearchReturnSeriesArtifact, ...]: ...
    def save_metric(self, metric: ResearchMetricSnapshot) -> ResearchMetricSnapshot: ...
    def metric(self, trial_id: str) -> ResearchMetricSnapshot | None: ...
    def save_selection(self, event: ResearchSelectionEvent) -> ResearchSelectionEvent: ...
    def selections(self, experiment_id: str) -> tuple[ResearchSelectionEvent, ...]: ...
    def save_partitions(
        self, values: Sequence[ResearchCrossValidationPartition]
    ) -> tuple[ResearchCrossValidationPartition, ...]: ...
    def save_evaluation(
        self, evaluation: ResearchMultipleTestingEvaluation
    ) -> ResearchMultipleTestingEvaluation: ...
    def evaluations(self, experiment_id: str) -> tuple[ResearchMultipleTestingEvaluation, ...]: ...


class InMemoryResearchIntegrityRepository:
    def __init__(self) -> None:
        self._families: dict[str, ResearchExperimentFamily] = {}
        self._trials: dict[str, ResearchTrialAttempt] = {}
        self._artifacts: dict[str, ResearchReturnSeriesArtifact] = {}
        self._metrics: dict[str, ResearchMetricSnapshot] = {}
        self._selections: dict[str, ResearchSelectionEvent] = {}
        self._partitions: dict[str, ResearchCrossValidationPartition] = {}
        self._evaluations: dict[str, ResearchMultipleTestingEvaluation] = {}

    def family_by_idempotency(self, key: str) -> ResearchExperimentFamily | None:
        return next((item for item in self._families.values() if item.idempotency_key == key), None)

    def family(self, experiment_id: str) -> ResearchExperimentFamily | None:
        return self._families.get(experiment_id)

    def families(self) -> tuple[ResearchExperimentFamily, ...]:
        return tuple(sorted(self._families.values(), key=lambda item: item.created_at))

    def save_family(self, family: ResearchExperimentFamily) -> ResearchExperimentFamily:
        prior = self._families.get(family.experiment_id)
        if prior is not None and prior.canonical_input_hash != family.canonical_input_hash:
            raise ResearchIdempotencyConflict("RESEARCH_FAMILY_IDEMPOTENCY_CONFLICT")
        self._families[family.experiment_id] = family
        return family

    def update_family_state(
        self,
        experiment_id: str,
        *,
        expected: ResearchFamilyState,
        replacement: ResearchExperimentFamily,
    ) -> ResearchExperimentFamily:
        prior = self._families.get(experiment_id)
        if prior is None or prior.state is not expected:
            raise ResearchIntegrityApplicationError("RESEARCH_FAMILY_STATE_CONFLICT")
        if prior.canonical_input_hash != replacement.canonical_input_hash:
            raise ResearchIntegrityApplicationError("RESEARCH_FAMILY_SCOPE_IMMUTABLE")
        self._families[experiment_id] = replacement
        return replacement

    def trial_by_idempotency(self, experiment_id: str, key: str) -> ResearchTrialAttempt | None:
        return next(
            (
                item
                for item in self._trials.values()
                if item.experiment_id == experiment_id and item.idempotency_key == key
            ),
            None,
        )

    def reserve_trial(self, trial: ResearchTrialAttempt) -> ResearchTrialAttempt:
        family_trials = self.trials(trial.experiment_id)
        expected = len(family_trials) + 1
        if trial.ordinal != expected:
            raise ResearchIntegrityApplicationError("TRIAL_ORDINAL_CONFLICT")
        self._trials[trial.trial_id] = trial
        return trial

    def finish_trial(self, trial: ResearchTrialAttempt) -> ResearchTrialAttempt:
        prior = self._trials.get(trial.trial_id)
        if prior is None or prior.canonical_input_hash != trial.canonical_input_hash:
            raise ResearchIdempotencyConflict("TRIAL_RESULT_CONFLICT")
        if prior.state is not ResearchTrialState.REQUESTED:
            if prior.canonical_result_hash == trial.canonical_result_hash:
                return prior
            raise ResearchIdempotencyConflict("TRIAL_RESULT_CONFLICT")
        self._trials[trial.trial_id] = trial
        return trial

    def trial(self, trial_id: str) -> ResearchTrialAttempt | None:
        return self._trials.get(trial_id)

    def trials(self, experiment_id: str) -> tuple[ResearchTrialAttempt, ...]:
        return tuple(
            sorted(
                (item for item in self._trials.values() if item.experiment_id == experiment_id),
                key=lambda item: item.ordinal,
            )
        )

    def save_artifact(self, artifact: ResearchReturnSeriesArtifact) -> ResearchReturnSeriesArtifact:
        prior = self._artifacts.get(artifact.artifact_id)
        if prior is not None and prior.canonical_result_hash != artifact.canonical_result_hash:
            raise ResearchIdempotencyConflict("RETURN_ARTIFACT_CONFLICT")
        self._artifacts[artifact.artifact_id] = artifact
        return artifact

    def artifacts(self, trial_id: str) -> tuple[ResearchReturnSeriesArtifact, ...]:
        return tuple(
            sorted(
                (item for item in self._artifacts.values() if item.trial_id == trial_id),
                key=lambda item: item.series_type.value,
            )
        )

    def save_metric(self, metric: ResearchMetricSnapshot) -> ResearchMetricSnapshot:
        prior = self._metrics.get(metric.trial_id)
        if prior is not None and prior.canonical_result_hash != metric.canonical_result_hash:
            raise ResearchIdempotencyConflict("RESEARCH_METRIC_CONFLICT")
        self._metrics[metric.trial_id] = metric
        return metric

    def metric(self, trial_id: str) -> ResearchMetricSnapshot | None:
        return self._metrics.get(trial_id)

    def save_selection(self, event: ResearchSelectionEvent) -> ResearchSelectionEvent:
        prior = self._selections.get(event.selection_id)
        if prior is not None and prior.canonical_result_hash != event.canonical_result_hash:
            raise ResearchIdempotencyConflict("RESEARCH_SELECTION_CONFLICT")
        self._selections[event.selection_id] = event
        return event

    def selections(self, experiment_id: str) -> tuple[ResearchSelectionEvent, ...]:
        return tuple(
            sorted(
                (item for item in self._selections.values() if item.experiment_id == experiment_id),
                key=lambda item: item.selected_at,
            )
        )

    def save_partitions(
        self, values: Sequence[ResearchCrossValidationPartition]
    ) -> tuple[ResearchCrossValidationPartition, ...]:
        for value in values:
            prior = self._partitions.get(value.partition_id)
            if prior is not None and prior.canonical_hash != value.canonical_hash:
                raise ResearchIdempotencyConflict("RESEARCH_PARTITION_CONFLICT")
            self._partitions[value.partition_id] = value
        return tuple(values)

    def save_evaluation(
        self, evaluation: ResearchMultipleTestingEvaluation
    ) -> ResearchMultipleTestingEvaluation:
        prior = self._evaluations.get(evaluation.evaluation_id)
        if prior is not None and prior.canonical_result_hash != evaluation.canonical_result_hash:
            raise ResearchIdempotencyConflict("RESEARCH_EVALUATION_CONFLICT")
        self._evaluations[evaluation.evaluation_id] = evaluation
        return evaluation

    def evaluations(self, experiment_id: str) -> tuple[ResearchMultipleTestingEvaluation, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._evaluations.values()
                    if item.experiment_id == experiment_id
                ),
                key=lambda item: item.created_at,
            )
        )


class OpenResearchExperimentFamily:
    """Freeze all declared research degrees of freedom before execution."""

    def __init__(self, repository: ResearchIntegrityRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        name: str,
        vertical: str,
        research_family: str,
        research_question: str,
        target_label_identity: str,
        benchmark_identities: Sequence[str],
        data_scope_identities: Sequence[str],
        data_scope_hashes: Sequence[str],
        universe_hash: str,
        period_start: str,
        period_end: str,
        cutoff_at: datetime,
        feature_schema_hashes: Sequence[str],
        permitted_families: Sequence[str],
        parameter_space: Mapping[str, object],
        maximum_trials: int,
        maximum_feature_variants: int,
        maximum_model_variants: int,
        maximum_simulator_variants: int,
        evaluation_policy_id: str,
        evaluation_policy_payload: Mapping[str, object],
        cost_execution_assumptions: Mapping[str, object],
        frequency: ResearchFrequency,
        operator_action_id: str,
        operator_reason: str,
        idempotency_key: str,
        created_at: datetime | None = None,
    ) -> ResearchExperimentFamily:
        now = created_at or datetime.now(tz=UTC)
        self._validate(
            name=name,
            vertical=vertical,
            research_question=research_question,
            data_scope_identities=data_scope_identities,
            data_scope_hashes=data_scope_hashes,
            universe_hash=universe_hash,
            period_start=period_start,
            period_end=period_end,
            cutoff_at=cutoff_at,
            permitted_families=permitted_families,
            parameter_space=parameter_space,
            maximum_trials=maximum_trials,
            maximum_feature_variants=maximum_feature_variants,
            maximum_model_variants=maximum_model_variants,
            maximum_simulator_variants=maximum_simulator_variants,
            operator_reason=operator_reason,
            idempotency_key=idempotency_key,
        )
        parameter_hash = content_digest(parameter_space)
        evaluation_hash = content_digest(evaluation_policy_payload)
        cost_hash = content_digest(cost_execution_assumptions)
        payload = {
            "name": name,
            "vertical": vertical,
            "research_family": research_family,
            "research_question": research_question,
            "target_label_identity": target_label_identity,
            "benchmark_identities": tuple(benchmark_identities),
            "data_scope_identities": tuple(data_scope_identities),
            "data_scope_hashes": tuple(data_scope_hashes),
            "universe_hash": universe_hash,
            "period_start": period_start,
            "period_end": period_end,
            "cutoff_at": cutoff_at,
            "feature_schema_hashes": tuple(feature_schema_hashes),
            "permitted_families": tuple(permitted_families),
            "parameter_space_hash": parameter_hash,
            "maximum_trials": maximum_trials,
            "maximum_feature_variants": maximum_feature_variants,
            "maximum_model_variants": maximum_model_variants,
            "maximum_simulator_variants": maximum_simulator_variants,
            "evaluation_policy_id": evaluation_policy_id,
            "evaluation_policy_hash": evaluation_hash,
            "cost_execution_hash": cost_hash,
            "frequency": frequency.value,
            "operator_action_id": operator_action_id,
            "operator_reason": operator_reason,
            "idempotency_key": idempotency_key,
        }
        input_hash = content_digest(payload)
        prior = self._repository.family_by_idempotency(idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != input_hash:
                raise ResearchIdempotencyConflict("RESEARCH_FAMILY_IDEMPOTENCY_CONFLICT")
            return prior
        family = ResearchExperimentFamily(
            experiment_id=deterministic_id("research_experiment_family", payload),
            name=name,
            vertical=vertical,
            research_family=research_family,
            research_question=research_question,
            target_label_identity=target_label_identity,
            benchmark_identities=tuple(benchmark_identities),
            data_scope_identities=tuple(data_scope_identities),
            data_scope_hashes=tuple(data_scope_hashes),
            universe_hash=universe_hash,
            period_start=period_start,
            period_end=period_end,
            cutoff_at=cutoff_at,
            feature_schema_hashes=tuple(feature_schema_hashes),
            permitted_families=tuple(permitted_families),
            parameter_space=dict(parameter_space),
            parameter_space_hash=parameter_hash,
            maximum_trials=maximum_trials,
            maximum_feature_variants=maximum_feature_variants,
            maximum_model_variants=maximum_model_variants,
            maximum_simulator_variants=maximum_simulator_variants,
            evaluation_policy_id=evaluation_policy_id,
            evaluation_policy_hash=evaluation_hash,
            cost_execution_assumptions=dict(cost_execution_assumptions),
            cost_execution_hash=cost_hash,
            frequency=frequency,
            state=ResearchFamilyState.OPEN,
            created_at=now,
            opened_at=now,
            closed_at=None,
            operator_action_id=operator_action_id,
            operator_reason=operator_reason,
            idempotency_key=idempotency_key,
            canonical_input_hash=input_hash,
            canonical_result_hash=content_digest(
                {"canonical_input_hash": input_hash, "state": "OPEN"}
            ),
        )
        return self._repository.save_family(family)

    @staticmethod
    def _validate(
        *,
        name: str,
        vertical: str,
        research_question: str,
        data_scope_identities: Sequence[str],
        data_scope_hashes: Sequence[str],
        universe_hash: str,
        period_start: str,
        period_end: str,
        cutoff_at: datetime,
        permitted_families: Sequence[str],
        parameter_space: Mapping[str, object],
        maximum_trials: int,
        maximum_feature_variants: int,
        maximum_model_variants: int,
        maximum_simulator_variants: int,
        operator_reason: str,
        idempotency_key: str,
    ) -> None:
        if (
            not name
            or not vertical
            or not research_question
            or not operator_reason
            or not idempotency_key
            or not permitted_families
            or not parameter_space
        ):
            raise ResearchIntegrityApplicationError("RESEARCH_FAMILY_INPUT_INVALID")
        if any(
            callable(value)
            or isinstance(value, (bytes, bytearray))
            or (isinstance(value, str) and value.strip().lower().startswith(("lambda ", "exec(")))
            for value in parameter_space.values()
        ):
            raise ResearchIntegrityApplicationError("EXECUTABLE_SEARCH_SPACE_FORBIDDEN")
        if period_start > period_end:
            raise ResearchIntegrityApplicationError("RESEARCH_PERIOD_INVALID")
        if not isinstance(cutoff_at, datetime):
            raise ResearchIntegrityApplicationError("RESEARCH_CUTOFF_INVALID")
        if len(data_scope_identities) != len(data_scope_hashes):
            raise ResearchIntegrityApplicationError("DATA_SCOPE_HASH_ALIGNMENT_INVALID")
        hashes = (*data_scope_hashes, universe_hash)
        if any(not isinstance(item, str) or len(item) != 64 for item in hashes):
            raise ResearchIntegrityApplicationError("RESEARCH_SCOPE_HASH_INVALID")
        if not 3 <= maximum_trials <= 500:
            raise ResearchIntegrityApplicationError("TRIAL_BUDGET_INVALID")
        for value in (
            maximum_feature_variants,
            maximum_model_variants,
            maximum_simulator_variants,
        ):
            if value < 1 or value > maximum_trials:
                raise ResearchIntegrityApplicationError("VARIANT_BUDGET_INVALID")


class ResearchTrialLedger:
    """Reserve immutable trial ordinals before any research computation."""

    def __init__(self, repository: ResearchIntegrityRepository) -> None:
        self._repository = repository

    def reserve(
        self,
        *,
        experiment_id: str,
        source_type: ResearchTrialSourceType,
        source_identity: str,
        source_hash: str,
        strategy_model_family: str,
        configuration: Mapping[str, object],
        feature_set_hash: str,
        dataset_hash: str,
        simulator_identity: str,
        simulator_version: str,
        execution_cost_policy_hash: str,
        random_seed: int | None,
        idempotency_key: str,
        started_at: datetime,
        parent_trial_id: str | None = None,
        selected_for_comparison: bool = True,
    ) -> ResearchTrialAttempt:
        family = self._repository.family(experiment_id)
        if family is None or family.state is not ResearchFamilyState.OPEN:
            raise ResearchIntegrityApplicationError("RESEARCH_FAMILY_NOT_OPEN")
        if strategy_model_family not in family.permitted_families:
            raise ResearchIntegrityApplicationError("TRIAL_FAMILY_OUTSIDE_DECLARED_SCOPE")
        configuration_hash = content_digest(configuration)
        payload = {
            "experiment_id": experiment_id,
            "parent_trial_id": parent_trial_id,
            "source_type": source_type.value,
            "source_identity": source_identity,
            "source_hash": source_hash,
            "strategy_model_family": strategy_model_family,
            "configuration_hash": configuration_hash,
            "feature_set_hash": feature_set_hash,
            "dataset_hash": dataset_hash,
            "simulator_identity": simulator_identity,
            "simulator_version": simulator_version,
            "execution_cost_policy_hash": execution_cost_policy_hash,
            "random_seed": random_seed,
            "idempotency_key": idempotency_key,
        }
        input_hash = content_digest(payload)
        replay = self._repository.trial_by_idempotency(experiment_id, idempotency_key)
        if replay is not None:
            if replay.canonical_input_hash != input_hash:
                raise ResearchIdempotencyConflict("TRIAL_IDEMPOTENCY_CONFLICT")
            return replay
        existing = self._repository.trials(experiment_id)
        if len(existing) >= family.maximum_trials:
            raise ResearchIntegrityApplicationError("TRIAL_BUDGET_EXHAUSTED")
        for item in existing:
            if item.source_identity == source_identity and item.source_hash != source_hash:
                raise ResearchIntegrityApplicationError("SOURCE_LINEAGE_CONFLICT")
        ordinal = len(existing) + 1
        trial = ResearchTrialAttempt(
            trial_id=deterministic_id(
                "research_trial_attempt",
                {"experiment_id": experiment_id, "ordinal": ordinal, **payload},
            ),
            experiment_id=experiment_id,
            parent_trial_id=parent_trial_id,
            ordinal=ordinal,
            source_type=source_type,
            source_identity=source_identity,
            source_hash=source_hash,
            strategy_model_family=strategy_model_family,
            configuration=dict(configuration),
            configuration_hash=configuration_hash,
            feature_set_hash=feature_set_hash,
            dataset_hash=dataset_hash,
            simulator_identity=simulator_identity,
            simulator_version=simulator_version,
            execution_cost_policy_hash=execution_cost_policy_hash,
            random_seed=random_seed,
            state=ResearchTrialState.REQUESTED,
            started_at=started_at,
            completed_at=None,
            result_identity=None,
            result_hash=None,
            failure_reason=None,
            selected_for_comparison=selected_for_comparison,
            integrity_blocked=False,
            idempotency_key=idempotency_key,
            canonical_input_hash=input_hash,
            canonical_result_hash=None,
        )
        return self._repository.reserve_trial(trial)

    def finish(
        self,
        trial_id: str,
        *,
        state: ResearchTrialState,
        completed_at: datetime,
        result_identity: str | None,
        result_hash: str | None,
        failure_reason: str | None = None,
        integrity_blocked: bool = False,
    ) -> ResearchTrialAttempt:
        prior = self._repository.trial(trial_id)
        if prior is None:
            raise ResearchIntegrityApplicationError("TRIAL_NOT_FOUND")
        if state in (ResearchTrialState.REQUESTED, ResearchTrialState.RUNNING):
            raise ResearchIntegrityApplicationError("TRIAL_TERMINAL_STATE_REQUIRED")
        result_payload = {
            "state": state.value,
            "result_identity": result_identity,
            "result_hash": result_hash,
            "failure_reason": failure_reason,
            "integrity_blocked": integrity_blocked,
        }
        completed = replace(
            prior,
            state=state,
            completed_at=completed_at,
            result_identity=result_identity,
            result_hash=result_hash,
            failure_reason=failure_reason,
            integrity_blocked=integrity_blocked,
            canonical_result_hash=content_digest(result_payload),
        )
        return self._repository.finish_trial(completed)


class ResearchExperimentLifecycle:
    """Close or cancel a family without permitting scope or budget edits."""

    def __init__(self, repository: ResearchIntegrityRepository) -> None:
        self._repository = repository

    def transition(
        self,
        experiment_id: str,
        *,
        target: ResearchFamilyState,
        reason: str,
        changed_at: datetime,
    ) -> ResearchExperimentFamily:
        family = self._repository.family(experiment_id)
        if family is None:
            raise ResearchIntegrityApplicationError("RESEARCH_FAMILY_NOT_FOUND")
        allowed = {
            ResearchFamilyState.DRAFT: {
                ResearchFamilyState.OPEN,
                ResearchFamilyState.CANCELED,
            },
            ResearchFamilyState.OPEN: {
                ResearchFamilyState.CLOSED,
                ResearchFamilyState.CANCELED,
                ResearchFamilyState.INVALIDATED,
            },
        }
        if family.state is target:
            return family
        if target not in allowed.get(family.state, set()) or not reason:
            raise ResearchIntegrityApplicationError("RESEARCH_FAMILY_STATE_CONFLICT")
        if target is ResearchFamilyState.CANCELED and any(
            item.state in (ResearchTrialState.REQUESTED, ResearchTrialState.RUNNING)
            for item in self._repository.trials(experiment_id)
        ):
            raise ResearchIntegrityApplicationError("ACTIVE_TRIALS_PREVENT_CANCELLATION")
        replacement = replace(
            family,
            state=target,
            closed_at=changed_at
            if target
            in (
                ResearchFamilyState.CLOSED,
                ResearchFamilyState.CANCELED,
                ResearchFamilyState.INVALIDATED,
            )
            else family.closed_at,
            canonical_result_hash=content_digest(
                {
                    "canonical_input_hash": family.canonical_input_hash,
                    "state": target.value,
                    "reason": reason,
                    "changed_at": changed_at,
                }
            ),
        )
        return self._repository.update_family_state(
            experiment_id,
            expected=family.state,
            replacement=replacement,
        )


class ResearchIntegrityService:
    def __init__(
        self,
        repository: ResearchIntegrityRepository,
        artifacts: ResearchReturnArtifactStore,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts

    def retain_return_evidence(
        self,
        *,
        trial_id: str,
        gross_observations: Sequence[ReturnObservation],
        net_observations: Sequence[ReturnObservation],
        frequency: ResearchFrequency,
        turnover: Decimal = Decimal(),
    ) -> ResearchMetricSnapshot:
        trial = self._repository.trial(trial_id)
        if trial is None or trial.state is not ResearchTrialState.COMPLETED:
            raise ResearchIntegrityApplicationError("COMPLETED_TRIAL_REQUIRED")
        family = self._repository.family(trial.experiment_id)
        if family is None or family.frequency is not frequency:
            raise ResearchIntegrityApplicationError("RESEARCH_FREQUENCY_INCOMPARABLE")
        return_policy_hash = content_digest(
            {"policy": RESEARCH_METRICS_POLICY_ID, "frequency": frequency.value}
        )
        gross_artifact = self._artifacts.write(
            experiment_id=trial.experiment_id,
            trial_id=trial_id,
            series_type=ResearchSeriesType.GROSS_RETURN,
            frequency=frequency,
            observations=gross_observations,
            return_policy_hash=return_policy_hash,
            missing_period_policy="FAIL_CLOSED_EXACT_ALIGNMENT",
        )
        net_artifact = self._artifacts.write(
            experiment_id=trial.experiment_id,
            trial_id=trial_id,
            series_type=ResearchSeriesType.NET_RETURN,
            frequency=frequency,
            observations=net_observations,
            return_policy_hash=return_policy_hash,
            missing_period_policy="FAIL_CLOSED_EXACT_ALIGNMENT",
        )
        self._repository.save_artifact(gross_artifact)
        self._repository.save_artifact(net_artifact)
        normalized = normalize_return_metrics(
            gross_observations=gross_observations,
            net_observations=net_observations,
            frequency=frequency,
            turnover=turnover,
        )
        input_payload = {
            "trial_id": trial_id,
            "gross_artifact": gross_artifact.canonical_result_hash,
            "net_artifact": net_artifact.canonical_result_hash,
            "policy": RESEARCH_METRICS_POLICY_ID,
        }
        result_payload = {
            "observation_count": normalized.observation_count,
            "independent_group_count": normalized.independent_group_count,
            "gross_return": str(normalized.gross_return),
            "net_return": str(normalized.net_return),
            "mean": str(normalized.mean_periodic_return),
            "volatility": str(normalized.periodic_volatility),
            "sharpe": str(normalized.sharpe_ratio) if normalized.sharpe_ratio is not None else None,
            "sortino": str(normalized.sortino_ratio)
            if normalized.sortino_ratio is not None
            else None,
            "drawdown": str(normalized.maximum_drawdown),
            "skewness": str(normalized.skewness) if normalized.skewness is not None else None,
            "excess_kurtosis": str(normalized.excess_kurtosis)
            if normalized.excess_kurtosis is not None
            else None,
            "effective_sample_size": str(normalized.effective_sample_size),
            "warnings": normalized.warnings,
        }
        input_hash = content_digest(input_payload)
        metric = ResearchMetricSnapshot(
            metric_id=deterministic_id("research_metric_snapshot", input_payload),
            trial_id=trial_id,
            observation_count=normalized.observation_count,
            independent_group_count=normalized.independent_group_count,
            gross_return=normalized.gross_return,
            net_return=normalized.net_return,
            mean_periodic_return=normalized.mean_periodic_return,
            periodic_volatility=normalized.periodic_volatility,
            sharpe_ratio=normalized.sharpe_ratio,
            sortino_ratio=normalized.sortino_ratio,
            maximum_drawdown=normalized.maximum_drawdown,
            turnover=normalized.turnover,
            hit_rate=normalized.hit_rate,
            brier_score=None,
            log_loss=None,
            benchmark_differences={},
            skewness=normalized.skewness,
            excess_kurtosis=normalized.excess_kurtosis,
            autocorrelations=normalized.autocorrelations,
            effective_sample_size=normalized.effective_sample_size,
            concentration={
                "period": str(normalized.largest_period_concentration),
                "group": str(normalized.largest_group_concentration),
                "instrument": str(normalized.largest_instrument_concentration),
            },
            integrity_warnings=normalized.warnings,
            calculation_policy_id=RESEARCH_METRICS_POLICY_ID,
            calculation_policy_version="1",
            canonical_input_hash=input_hash,
            canonical_result_hash=content_digest(result_payload),
        )
        return self._repository.save_metric(metric)

    def select(
        self,
        *,
        experiment_id: str,
        selected_trial_id: str,
        criterion: str,
        direction: str,
        action_identity: str,
        reason: str,
        selected_at: datetime,
    ) -> ResearchSelectionEvent:
        trials = self._repository.trials(experiment_id)
        candidate_ids = tuple(item.trial_id for item in trials)
        if selected_trial_id not in candidate_ids or direction not in ("MAXIMIZE", "MINIMIZE"):
            raise ResearchIntegrityApplicationError("RESEARCH_SELECTION_INVALID")
        candidate_set_hash = content_digest(candidate_ids)
        payload = {
            "experiment_id": experiment_id,
            "selected_trial_id": selected_trial_id,
            "candidate_set_hash": candidate_set_hash,
            "criterion": criterion,
            "direction": direction,
            "selected_at": selected_at,
            "action_identity": action_identity,
            "reason": reason,
        }
        input_hash = content_digest(payload)
        return self._repository.save_selection(
            ResearchSelectionEvent(
                selection_id=deterministic_id("research_selection_event", payload),
                experiment_id=experiment_id,
                selected_trial_id=selected_trial_id,
                candidate_trial_ids=candidate_ids,
                candidate_set_hash=candidate_set_hash,
                criterion=criterion,
                direction=direction,
                selected_at=selected_at,
                action_identity=action_identity,
                reason=reason,
                canonical_input_hash=input_hash,
                canonical_result_hash=content_digest(
                    {"canonical_input_hash": input_hash, "retained": True}
                ),
            )
        )

    def evaluate(
        self, *, experiment_id: str, cutoff_at: datetime
    ) -> ResearchMultipleTestingEvaluation:
        family = self._repository.family(experiment_id)
        if family is None:
            raise ResearchIntegrityApplicationError("RESEARCH_FAMILY_NOT_FOUND")
        trials = tuple(
            item for item in self._repository.trials(experiment_id) if item.started_at <= cutoff_at
        )
        completed = tuple(
            item
            for item in trials
            if item.state is ResearchTrialState.COMPLETED
            and item.selected_for_comparison
            and not item.integrity_blocked
        )
        failed_count = len(trials) - len(completed)
        series: dict[str, tuple[ReturnObservation, ...]] = {}
        metrics: dict[str, ResearchMetricSnapshot] = {}
        artifact_failures: list[str] = []
        for trial in completed:
            metric = self._repository.metric(trial.trial_id)
            net_artifact = next(
                (
                    item
                    for item in self._repository.artifacts(trial.trial_id)
                    if item.series_type is ResearchSeriesType.NET_RETURN
                ),
                None,
            )
            if metric is None or net_artifact is None:
                artifact_failures.append(trial.trial_id)
                continue
            try:
                observations = self._artifacts.read(net_artifact)
            except ResearchIntegrityApplicationError:
                artifact_failures.append(trial.trial_id)
                continue
            series[trial.trial_id] = observations
            metrics[trial.trial_id] = metric

        base_input = {
            "experiment_id": experiment_id,
            "cutoff_at": cutoff_at,
            "trial_ids": tuple(item.trial_id for item in trials),
            "family_hash": family.canonical_input_hash,
            "policies": (
                RESEARCH_METRICS_POLICY_ID,
                PSR_POLICY_ID,
                DSR_POLICY_ID,
                EFFECTIVE_TRIAL_POLICY_ID,
                CSCV_POLICY_ID,
                INTEGRITY_POLICY_ID,
            ),
        }
        input_hash = content_digest(base_input)
        evaluation_id = deterministic_id("research_multiple_testing_evaluation", base_input)
        if len(series) < 3:
            reasons = ["COMPARABLE_RETURN_TRIALS_INSUFFICIENT"]
            if artifact_failures:
                reasons.append("RETURN_ARTIFACT_MISSING_OR_HASH_INVALID")
            return self._repository.save_evaluation(
                self._empty_evaluation(
                    evaluation_id=evaluation_id,
                    family=family,
                    cutoff_at=cutoff_at,
                    trials=trials,
                    failed_count=failed_count,
                    input_hash=input_hash,
                    reasons=tuple(reasons),
                )
            )
        try:
            aligned = {key: tuple(item.value for item in values) for key, values in series.items()}
            effective = estimate_effective_trial_count(aligned)
            best_trial = max(
                series,
                key=lambda item: (
                    metrics[item].sharpe_ratio
                    if metrics[item].sharpe_ratio is not None
                    else Decimal("-999999"),
                    item,
                ),
            )
            best_metric = metrics[best_trial]
            sharpes = tuple(
                value.sharpe_ratio for value in metrics.values() if value.sharpe_ratio is not None
            )
            if (
                best_metric.sharpe_ratio is None
                or best_metric.skewness is None
                or best_metric.excess_kurtosis is None
                or len(sharpes) < 2
            ):
                raise ResearchIntegrityError("SHARPE_MOMENTS_INSUFFICIENT")
            deflated = deflated_sharpe_ratio(
                best_sharpe=best_metric.sharpe_ratio,
                observed_trial_sharpes=sharpes,
                effective_trial_count=effective.effective_trial_count,
                effective_sample_size=best_metric.effective_sample_size,
                skewness=best_metric.skewness,
                excess_kurtosis=best_metric.excess_kurtosis,
            )
            cscv = cscv_probability_of_backtest_overfitting(trial_returns=series)
            stability = selection_stability(trial_returns=series)
            partitions = self._persist_partitions(family, cscv.partitions)
            del partitions
            normalized = normalize_return_metrics(
                gross_observations=series[best_trial],
                net_observations=series[best_trial],
                frequency=family.frequency,
                turnover=best_metric.turnover,
            )
            material_mismatch = any(
                item.integrity_blocked
                and item.selected_for_comparison
                and item.failure_reason == "MATERIAL_SIMULATOR_MISMATCH"
                for item in trials
            )
            hard_reasons = research_integrity_reasons(
                predeclared=all(
                    item.started_at >= (family.opened_at or family.created_at) for item in trials
                ),
                budget_respected=len(trials) <= family.maximum_trials,
                all_attempts_retained=tuple(item.ordinal for item in trials)
                == tuple(range(1, len(trials) + 1)),
                aligned_scope=len({item.dataset_hash for item in completed}) == 1,
                aligned_costs=len({item.execution_cost_policy_hash for item in completed}) == 1,
                material_simulator_mismatch=material_mismatch,
                lineage_valid=not any(
                    item.failure_reason == "LEAKAGE_OR_LINEAGE_FAILURE" for item in trials
                ),
                artifacts_reproducible=not artifact_failures,
                metrics=normalized,
                psr=deflated.probabilistic_sharpe_ratio,
                dsr=deflated.deflated_sharpe_ratio,
                pbo=cscv.probability_of_backtest_overfitting,
                stability=stability,
                severe_recent_degradation=False,
            )
            recommendation = (
                ResearchRecommendation.ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW
                if not hard_reasons
                else ResearchRecommendation.REJECT_SELECTION_BIAS
            )
            state = (
                MultipleTestingState.COMPLETED
                if not hard_reasons
                else MultipleTestingState.BLOCKED_INTEGRITY
            )
            reason_codes = tuple(
                (
                    *hard_reasons,
                    *effective.warnings,
                    *(("RETURN_ARTIFACT_MISSING_OR_HASH_INVALID",) if artifact_failures else ()),
                )
            )
            result_payload = {
                "best_trial": best_trial,
                "raw_trials": effective.raw_trial_count,
                "effective_trials": str(effective.effective_trial_count),
                "psr": str(deflated.probabilistic_sharpe_ratio),
                "dsr": str(deflated.deflated_sharpe_ratio),
                "expected_maximum": str(deflated.expected_maximum_sharpe),
                "pbo": str(cscv.probability_of_backtest_overfitting),
                "stability": stability.canonical_hash,
                "state": state.value,
                "recommendation": recommendation.value,
                "reasons": reason_codes,
            }
            evaluation = ResearchMultipleTestingEvaluation(
                evaluation_id=evaluation_id,
                experiment_id=experiment_id,
                cutoff_at=cutoff_at,
                included_trial_ids=tuple(item.trial_id for item in trials),
                included_trial_set_hash=content_digest(tuple(item.trial_id for item in trials)),
                completed_trial_count=len(completed),
                failed_rejected_trial_count=failed_count,
                unique_configuration_count=len({item.configuration_hash for item in trials}),
                feature_variant_count=len({item.feature_set_hash for item in trials}),
                model_variant_count=len({item.strategy_model_family for item in trials}),
                simulator_variant_count=len(
                    {(item.simulator_identity, item.simulator_version) for item in trials}
                ),
                raw_comparable_trial_count=effective.raw_trial_count,
                effective_independent_trials=effective.effective_trial_count,
                overlap_coverage=effective.overlap_coverage,
                best_observed_trial_id=best_trial,
                best_observed_metric=best_metric.sharpe_ratio,
                benchmark_metric=Decimal(),
                probabilistic_sharpe_ratio=deflated.probabilistic_sharpe_ratio,
                deflated_sharpe_ratio=deflated.deflated_sharpe_ratio,
                expected_maximum_sharpe=deflated.expected_maximum_sharpe,
                minimum_track_record_length=deflated.minimum_track_record_length,
                probability_backtest_overfitting=cscv.probability_of_backtest_overfitting,
                rank_stability={
                    "original_winner": stability.original_winner,
                    "top_group_excluded": stability.top_group_excluded,
                    "parameter_neighborhood": stability.parameter_neighborhood,
                },
                best_fold_excluded=stability.best_fold_excluded,
                best_period_excluded=stability.best_period_excluded,
                concentration=best_metric.concentration,
                state=state,
                recommendation=recommendation,
                reason_codes=reason_codes,
                calculation_policy_id=INTEGRITY_POLICY_ID,
                calculation_policy_version="1",
                canonical_input_hash=input_hash,
                canonical_result_hash=content_digest(result_payload),
                created_at=datetime.now(tz=UTC),
            )
            return self._repository.save_evaluation(evaluation)
        except ResearchIntegrityError as error:
            return self._repository.save_evaluation(
                self._empty_evaluation(
                    evaluation_id=evaluation_id,
                    family=family,
                    cutoff_at=cutoff_at,
                    trials=trials,
                    failed_count=failed_count,
                    input_hash=input_hash,
                    reasons=(str(error),),
                )
            )

    def _persist_partitions(
        self,
        family: ResearchExperimentFamily,
        values: Sequence[CscvPartitionResult],
    ) -> tuple[ResearchCrossValidationPartition, ...]:
        partitions = []
        for value in values:
            payload = {
                "experiment_id": family.experiment_id,
                "policy": CSCV_POLICY_ID,
                "ordinal": value.ordinal,
                "training": value.training_period_ids,
                "test": value.test_period_ids,
                "selected": value.selected_trial_id,
                "rank": str(value.selected_test_rank_percentile),
            }
            partitions.append(
                ResearchCrossValidationPartition(
                    partition_id=deterministic_id("research_cross_validation_partition", payload),
                    experiment_id=family.experiment_id,
                    policy_id=CSCV_POLICY_ID,
                    policy_version="1",
                    ordinal=value.ordinal,
                    training_group_ids=value.training_period_ids,
                    training_group_hash=content_digest(value.training_period_ids),
                    validation_group_ids=(),
                    validation_group_hash=content_digest(()),
                    test_group_ids=value.test_period_ids,
                    test_group_hash=content_digest(value.test_period_ids),
                    selected_trial_id=value.selected_trial_id,
                    validation_result_id="NOT_APPLICABLE",
                    test_result_id=value.canonical_hash,
                    selected_test_rank_percentile=value.selected_test_rank_percentile,
                    below_median=value.below_median,
                    canonical_hash=content_digest(payload),
                )
            )
        return self._repository.save_partitions(partitions)

    @staticmethod
    def _empty_evaluation(
        *,
        evaluation_id: str,
        family: ResearchExperimentFamily,
        cutoff_at: datetime,
        trials: Sequence[ResearchTrialAttempt],
        failed_count: int,
        input_hash: str,
        reasons: tuple[str, ...],
    ) -> ResearchMultipleTestingEvaluation:
        result = {
            "state": MultipleTestingState.INSUFFICIENT_EVIDENCE.value,
            "recommendation": ResearchRecommendation.REQUIRE_MORE_INDEPENDENT_EVIDENCE.value,
            "reasons": reasons,
        }
        return ResearchMultipleTestingEvaluation(
            evaluation_id=evaluation_id,
            experiment_id=family.experiment_id,
            cutoff_at=cutoff_at,
            included_trial_ids=tuple(item.trial_id for item in trials),
            included_trial_set_hash=content_digest(tuple(item.trial_id for item in trials)),
            completed_trial_count=sum(
                item.state is ResearchTrialState.COMPLETED for item in trials
            ),
            failed_rejected_trial_count=failed_count,
            unique_configuration_count=len({item.configuration_hash for item in trials}),
            feature_variant_count=len({item.feature_set_hash for item in trials}),
            model_variant_count=len({item.strategy_model_family for item in trials}),
            simulator_variant_count=len(
                {(item.simulator_identity, item.simulator_version) for item in trials}
            ),
            raw_comparable_trial_count=0,
            effective_independent_trials=Decimal(),
            overlap_coverage=Decimal(),
            best_observed_trial_id=None,
            best_observed_metric=None,
            benchmark_metric=Decimal(),
            probabilistic_sharpe_ratio=None,
            deflated_sharpe_ratio=None,
            expected_maximum_sharpe=None,
            minimum_track_record_length=None,
            probability_backtest_overfitting=None,
            rank_stability={},
            best_fold_excluded={},
            best_period_excluded={},
            concentration={},
            state=MultipleTestingState.INSUFFICIENT_EVIDENCE,
            recommendation=ResearchRecommendation.REQUIRE_MORE_INDEPENDENT_EVIDENCE,
            reason_codes=reasons,
            calculation_policy_id=INTEGRITY_POLICY_ID,
            calculation_policy_version="1",
            canonical_input_hash=input_hash,
            canonical_result_hash=content_digest(result),
            created_at=datetime.now(tz=UTC),
        )


def strategy_lab_research_integrity_extension(
    *,
    experiment: ResearchExperimentFamily,
    trial: ResearchTrialAttempt,
    evaluation: ResearchMultipleTestingEvaluation,
) -> dict[str, object]:
    """Non-mutating overlay for new Strategy Lab candidate/challenger reports."""

    return {
        "experiment_family_id": experiment.experiment_id,
        "trial_ordinal": trial.ordinal,
        "raw_search_count": evaluation.raw_comparable_trial_count,
        "effective_search_count": str(evaluation.effective_independent_trials),
        "probabilistic_sharpe_ratio": _optional_decimal(evaluation.probabilistic_sharpe_ratio),
        "deflated_sharpe_ratio": _optional_decimal(evaluation.deflated_sharpe_ratio),
        "probability_backtest_overfitting": _optional_decimal(
            evaluation.probability_backtest_overfitting
        ),
        "winner_stability": evaluation.rank_stability,
        "trial_budget": {
            "used": len(evaluation.included_trial_ids),
            "maximum": experiment.maximum_trials,
        },
        "selection_integrity_state": evaluation.state.value,
        "eligible_for_shadow_review": evaluation.recommendation
        is ResearchRecommendation.ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW,
        "authority_granted": False,
    }


def _optional_decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
