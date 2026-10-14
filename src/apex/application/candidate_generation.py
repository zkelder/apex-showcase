"""Apex-owned bounded automated Strategy Lab candidate generation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from time import monotonic
from typing import Protocol

from apex.application.research_integrity import (
    ResearchExperimentFamily,
    ResearchFamilyState,
    ResearchIntegrityService,
    ResearchRecommendation,
    ResearchTrialAttempt,
    ResearchTrialLedger,
    ResearchTrialSourceType,
    ResearchTrialState,
)
from apex.application.strategy_lab import (
    CalibrationMethod,
    CandidateState,
    DatasetState,
    ModelFamily,
    StrategyLabDatasetSnapshot,
    StrategyLabModelCandidate,
    StrategyLabService,
    StrategyLabTrainingRun,
    StrategyLabWalkForwardEvaluation,
)
from apex.domain.candidate_generation import (
    MLFLOW_MIRROR_POLICY_ID,
    OBJECTIVE_POLICY_ID,
    SELECTION_POLICY_ID,
    BoundedSearchSpace,
    CandidateGenerationError,
    ObjectiveDirection,
    ObjectiveIdentity,
    objective_direction,
)
from apex.domain.research_integrity import ReturnObservation
from apex.domain.values import canonical_json_bytes, content_digest, deterministic_id


class CandidateGenerationApplicationError(RuntimeError):
    """The bounded research orchestration boundary rejected an operation."""


class CandidateGenerationConflict(CandidateGenerationApplicationError):
    """An idempotency key was replayed with incompatible input."""


class SearchPlanState(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"


class SearchRunState(StrEnum):
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"


class ObjectiveState(StrEnum):
    PROPOSED = "PROPOSED"
    TRAINING = "TRAINING"
    COMPLETED = "COMPLETED"
    PRUNED = "PRUNED"
    FAILED = "FAILED"
    REJECTED_INTEGRITY = "REJECTED_INTEGRITY"
    CANCELED = "CANCELED"


class MirrorWorkspaceState(StrEnum):
    READY = "READY"
    DISABLED = "DISABLED"


class MirrorRecordState(StrEnum):
    PENDING = "PENDING"
    MIRRORED = "MIRRORED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    SKIPPED_DISABLED = "SKIPPED_DISABLED"


class CandidateGenerationRecommendation(StrEnum):
    NO_ELIGIBLE_CANDIDATE = "NO_ELIGIBLE_CANDIDATE"
    CONTINUE_RESEARCH = "CONTINUE_RESEARCH"
    ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW = "ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW"
    REJECT_SEARCH_BIAS = "REJECT_SEARCH_BIAS"
    BLOCKED_SIMULATOR_MISMATCH = "BLOCKED_SIMULATOR_MISMATCH"
    REQUIRE_MORE_EVIDENCE = "REQUIRE_MORE_EVIDENCE"


class MirrorReconciliationState(StrEnum):
    MIRROR_CONFORMANT = "MIRROR_CONFORMANT"
    MIRROR_MISSING = "MIRROR_MISSING"
    MIRROR_STALE = "MIRROR_STALE"
    MIRROR_DIVERGED = "MIRROR_DIVERGED"
    MLFLOW_UNAVAILABLE = "MLFLOW_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class AutomatedCandidateSearchPlan:
    search_plan_id: str
    experiment_id: str
    experiment_hash: str
    display_name: str
    objective_identity: ObjectiveIdentity
    objective_version: str
    objective_direction: ObjectiveDirection
    permitted_model_families: tuple[str, ...]
    search_space_payload: tuple[dict[str, object], ...]
    search_space_hash: str
    theoretical_combinations: int
    calibration_choices: tuple[str, ...]
    feature_schema_id: str
    feature_schema_hash: str
    dataset_id: str
    dataset_hash: str
    fold_policy_id: str
    fold_policy_hash: str
    random_seed: int
    sampler_identity: str
    sampler_version: str
    sampler_configuration: dict[str, object]
    sampler_hash: str
    pruner_identity: str
    pruner_version: str
    pruner_configuration: dict[str, object]
    pruner_hash: str
    maximum_trials: int
    maximum_failed_trials: int
    wall_clock_budget_seconds: int
    per_trial_budget_seconds: int
    artifact_byte_budget: int
    concurrency_limit: int
    candidate_selection_policy_id: str
    candidate_selection_policy_hash: str
    mlflow_mirror_policy_id: str
    mlflow_mirror_policy_hash: str
    state: SearchPlanState
    created_at: datetime
    opened_at: datetime
    completed_at: datetime | None
    canceled_at: datetime | None
    operator_action_id: str
    operator_reason: str
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class AutomatedCandidateSearchRun:
    search_run_id: str
    search_plan_id: str
    search_plan_hash: str
    experiment_id: str
    experiment_hash: str
    optuna_study_identity: str
    optuna_version: str
    mlflow_experiment_identity: str | None
    requested_trial_count: int
    reserved_trial_count: int
    completed_trial_count: int
    pruned_trial_count: int
    failed_trial_count: int
    integrity_rejected_trial_count: int
    elapsed_seconds: Decimal
    artifact_bytes_written: int
    state: SearchRunState
    best_raw_trial_id: str | None
    best_integrity_eligible_trial_id: str | None
    multiple_testing_evaluation_id: str | None
    final_recommendation: CandidateGenerationRecommendation | None
    reason_codes: tuple[str, ...]
    started_at: datetime
    completed_at: datetime | None
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str | None


@dataclass(frozen=True, slots=True)
class AutomatedCandidateTrialBinding:
    binding_id: str
    search_run_id: str
    optuna_trial_number: int
    optimizer_token: str
    research_trial_id: str
    training_run_id: str | None
    evaluation_id: str | None
    candidate_id: str | None
    dataset_id: str
    feature_schema_id: str
    parameter_payload: dict[str, object]
    parameter_hash: str
    sampler_metadata: dict[str, object]
    objective_value: Decimal | None
    objective_state: ObjectiveState
    artifact_identities: tuple[str, ...]
    artifact_hashes: tuple[str, ...]
    mlflow_mirror_record_id: str | None
    failure_reason: str | None
    canonical_input_hash: str
    canonical_result_hash: str | None


@dataclass(frozen=True, slots=True)
class MLflowMirrorWorkspace:
    workspace_id: str
    workspace_version: str
    tracking_mode: str
    loopback_bind_host: str
    backend_relative_path: str
    artifact_relative_root: str
    experiment_naming_policy: str
    mlflow_version: str
    state: MirrorWorkspaceState
    canonical_configuration_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MLflowMirrorRecord:
    mirror_record_id: str
    source_type: str
    source_identity: str
    source_hash: str
    workspace_id: str
    mlflow_experiment_id: str | None
    mlflow_experiment_name: str
    mlflow_run_id: str | None
    mirrored_parameter_hash: str
    mirrored_metric_hash: str
    mirrored_artifact_set_hash: str
    state: MirrorRecordState
    attempted_at: datetime
    completed_at: datetime | None
    failure_code: str | None
    failure_message: str | None
    retry_count: int
    canonical_input_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class CandidateGenerationDecision:
    decision_id: str
    search_run_id: str
    candidate_trial_ids: tuple[str, ...]
    candidate_trial_set_hash: str
    raw_best_trial_id: str | None
    integrity_eligible_trial_ids: tuple[str, ...]
    selected_candidate_trial_id: str | None
    selection_metric: str
    multiple_testing_evaluation_id: str | None
    conformance_state: str
    integrity_policy_state: str
    recommendation: CandidateGenerationRecommendation
    hard_gate_reason_codes: tuple[str, ...]
    operator_action_id: str | None
    canonical_input_hash: str
    canonical_result_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OptimizerProposal:
    trial_number: int
    optimizer_token: str
    parameters: dict[str, object]
    sampler_metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class TrialExecutionResult:
    training_run: StrategyLabTrainingRun
    evaluation: StrategyLabWalkForwardEvaluation
    candidate: StrategyLabModelCandidate
    objective_value: Decimal
    artifact_identities: tuple[str, ...]
    artifact_hashes: tuple[str, ...]
    artifact_bytes: int
    gross_returns: tuple[ReturnObservation, ...] = ()
    net_returns: tuple[ReturnObservation, ...] = ()
    turnover: Decimal = Decimal()


@dataclass(frozen=True, slots=True)
class MirrorReconciliation:
    mirror_record_id: str
    state: MirrorReconciliationState
    reason_codes: tuple[str, ...]


class CandidateSearchOptimizerPort(Protocol):
    runtime_version: str
    study_identity: str

    def ask(self, search_space: BoundedSearchSpace) -> OptimizerProposal: ...

    def tell(
        self,
        proposal: OptimizerProposal,
        *,
        state: ObjectiveState,
        objective_value: Decimal | None,
    ) -> None: ...

    def reconcile(self, bindings: Sequence[AutomatedCandidateTrialBinding]) -> tuple[str, ...]: ...


class MLflowMirrorPort(Protocol):
    runtime_version: str

    def mirror(
        self,
        *,
        workspace: MLflowMirrorWorkspace,
        experiment: ResearchExperimentFamily,
        plan: AutomatedCandidateSearchPlan,
        trial: ResearchTrialAttempt,
        binding: AutomatedCandidateTrialBinding,
        metrics: Mapping[str, Decimal | str | int | None],
        artifacts: Mapping[str, bytes],
        prior: MLflowMirrorRecord | None = None,
    ) -> MLflowMirrorRecord: ...

    def reconcile(
        self, workspace: MLflowMirrorWorkspace, record: MLflowMirrorRecord
    ) -> MirrorReconciliation: ...


class CandidateTrialExecutorPort(Protocol):
    def execute(
        self,
        *,
        plan: AutomatedCandidateSearchPlan,
        proposal: dict[str, str],
        research_trial: ResearchTrialAttempt,
    ) -> TrialExecutionResult: ...


class CandidateGenerationRepository(Protocol):
    def plans(
        self, experiment_id: str | None = None
    ) -> tuple[AutomatedCandidateSearchPlan, ...]: ...

    def plan_by_idempotency(self, key: str) -> AutomatedCandidateSearchPlan | None: ...

    def save_plan(self, value: AutomatedCandidateSearchPlan) -> AutomatedCandidateSearchPlan: ...

    def plan(self, search_plan_id: str) -> AutomatedCandidateSearchPlan | None: ...

    def update_plan(self, value: AutomatedCandidateSearchPlan) -> AutomatedCandidateSearchPlan: ...

    def run_by_idempotency(
        self, search_plan_id: str, key: str
    ) -> AutomatedCandidateSearchRun | None: ...

    def runs(
        self, search_plan_id: str | None = None
    ) -> tuple[AutomatedCandidateSearchRun, ...]: ...

    def save_run(self, value: AutomatedCandidateSearchRun) -> AutomatedCandidateSearchRun: ...

    def run(self, search_run_id: str) -> AutomatedCandidateSearchRun | None: ...

    def update_run(self, value: AutomatedCandidateSearchRun) -> AutomatedCandidateSearchRun: ...

    def save_binding(
        self, value: AutomatedCandidateTrialBinding
    ) -> AutomatedCandidateTrialBinding: ...

    def update_binding(
        self, value: AutomatedCandidateTrialBinding
    ) -> AutomatedCandidateTrialBinding: ...

    def bindings(self, search_run_id: str) -> tuple[AutomatedCandidateTrialBinding, ...]: ...

    def save_workspace(self, value: MLflowMirrorWorkspace) -> MLflowMirrorWorkspace: ...

    def workspace(self, workspace_id: str) -> MLflowMirrorWorkspace | None: ...

    def save_mirror_record(self, value: MLflowMirrorRecord) -> MLflowMirrorRecord: ...

    def mirror_records(self, experiment_id: str) -> tuple[MLflowMirrorRecord, ...]: ...

    def save_decision(self, value: CandidateGenerationDecision) -> CandidateGenerationDecision: ...

    def decision(self, search_run_id: str) -> CandidateGenerationDecision | None: ...


class InMemoryCandidateGenerationRepository:
    def __init__(self) -> None:
        self._plans: dict[str, AutomatedCandidateSearchPlan] = {}
        self._runs: dict[str, AutomatedCandidateSearchRun] = {}
        self._bindings: dict[str, AutomatedCandidateTrialBinding] = {}
        self._workspaces: dict[str, MLflowMirrorWorkspace] = {}
        self._mirrors: dict[str, MLflowMirrorRecord] = {}
        self._decisions: dict[str, CandidateGenerationDecision] = {}

    def plan_by_idempotency(self, key: str) -> AutomatedCandidateSearchPlan | None:
        return next((item for item in self._plans.values() if item.idempotency_key == key), None)

    def plans(self, experiment_id: str | None = None) -> tuple[AutomatedCandidateSearchPlan, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._plans.values()
                    if experiment_id is None or item.experiment_id == experiment_id
                ),
                key=lambda item: (item.created_at, item.search_plan_id),
            )
        )

    def save_plan(self, value: AutomatedCandidateSearchPlan) -> AutomatedCandidateSearchPlan:
        prior = self.plan_by_idempotency(value.idempotency_key)
        if prior is not None and prior.canonical_input_hash != value.canonical_input_hash:
            raise CandidateGenerationConflict("SEARCH_PLAN_IDEMPOTENCY_CONFLICT")
        self._plans[value.search_plan_id] = prior or value
        return prior or value

    def plan(self, search_plan_id: str) -> AutomatedCandidateSearchPlan | None:
        return self._plans.get(search_plan_id)

    def update_plan(self, value: AutomatedCandidateSearchPlan) -> AutomatedCandidateSearchPlan:
        prior = self._plans.get(value.search_plan_id)
        if prior is None or prior.canonical_input_hash != value.canonical_input_hash:
            raise CandidateGenerationConflict("SEARCH_PLAN_STATE_CONFLICT")
        self._plans[value.search_plan_id] = value
        return value

    def run_by_idempotency(
        self, search_plan_id: str, key: str
    ) -> AutomatedCandidateSearchRun | None:
        return next(
            (
                item
                for item in self._runs.values()
                if item.search_plan_id == search_plan_id and item.idempotency_key == key
            ),
            None,
        )

    def runs(self, search_plan_id: str | None = None) -> tuple[AutomatedCandidateSearchRun, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._runs.values()
                    if search_plan_id is None or item.search_plan_id == search_plan_id
                ),
                key=lambda item: (item.started_at, item.search_run_id),
            )
        )

    def save_run(self, value: AutomatedCandidateSearchRun) -> AutomatedCandidateSearchRun:
        prior = self.run_by_idempotency(value.search_plan_id, value.idempotency_key)
        if prior is not None and prior.canonical_input_hash != value.canonical_input_hash:
            raise CandidateGenerationConflict("SEARCH_RUN_IDEMPOTENCY_CONFLICT")
        self._runs[value.search_run_id] = prior or value
        return prior or value

    def run(self, search_run_id: str) -> AutomatedCandidateSearchRun | None:
        return self._runs.get(search_run_id)

    def update_run(self, value: AutomatedCandidateSearchRun) -> AutomatedCandidateSearchRun:
        self._runs[value.search_run_id] = value
        return value

    def save_binding(self, value: AutomatedCandidateTrialBinding) -> AutomatedCandidateTrialBinding:
        prior = self._bindings.get(value.binding_id)
        if prior is not None and prior.canonical_input_hash != value.canonical_input_hash:
            raise CandidateGenerationConflict("SEARCH_BINDING_CONFLICT")
        self._bindings[value.binding_id] = prior or value
        return prior or value

    def update_binding(
        self, value: AutomatedCandidateTrialBinding
    ) -> AutomatedCandidateTrialBinding:
        self._bindings[value.binding_id] = value
        return value

    def bindings(self, search_run_id: str) -> tuple[AutomatedCandidateTrialBinding, ...]:
        return tuple(
            sorted(
                (item for item in self._bindings.values() if item.search_run_id == search_run_id),
                key=lambda item: item.optuna_trial_number,
            )
        )

    def save_workspace(self, value: MLflowMirrorWorkspace) -> MLflowMirrorWorkspace:
        prior = self._workspaces.get(value.workspace_id)
        if (
            prior is not None
            and prior.canonical_configuration_hash != value.canonical_configuration_hash
        ):
            raise CandidateGenerationConflict("MLFLOW_WORKSPACE_IMMUTABLE")
        self._workspaces[value.workspace_id] = prior or value
        return prior or value

    def workspace(self, workspace_id: str) -> MLflowMirrorWorkspace | None:
        return self._workspaces.get(workspace_id)

    def save_mirror_record(self, value: MLflowMirrorRecord) -> MLflowMirrorRecord:
        self._mirrors[value.mirror_record_id] = value
        return value

    def mirror_records(self, experiment_id: str) -> tuple[MLflowMirrorRecord, ...]:
        prefix = f"apex-{experiment_id}"
        return tuple(
            item for item in self._mirrors.values() if item.mlflow_experiment_name == prefix
        )

    def save_decision(self, value: CandidateGenerationDecision) -> CandidateGenerationDecision:
        prior = self._decisions.get(value.search_run_id)
        if prior is not None and prior.canonical_result_hash != value.canonical_result_hash:
            raise CandidateGenerationConflict("CANDIDATE_DECISION_IMMUTABLE")
        self._decisions[value.search_run_id] = prior or value
        return prior or value

    def decision(self, search_run_id: str) -> CandidateGenerationDecision | None:
        return self._decisions.get(search_run_id)


class CreateAutomatedCandidateSearchPlan:
    def __init__(self, repository: CandidateGenerationRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        experiment: ResearchExperimentFamily,
        existing_experiment_trial_count: int,
        dataset: StrategyLabDatasetSnapshot,
        search_space: BoundedSearchSpace,
        display_name: str,
        permitted_model_families: Sequence[ModelFamily],
        objective_identity: ObjectiveIdentity,
        maximum_trials: int,
        maximum_failed_trials: int,
        wall_clock_budget_seconds: int,
        per_trial_budget_seconds: int,
        artifact_byte_budget: int,
        random_seed: int,
        operator_action_id: str,
        operator_reason: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> AutomatedCandidateSearchPlan:
        if experiment.state is not ResearchFamilyState.OPEN:
            raise CandidateGenerationApplicationError("RESEARCH_FAMILY_NOT_OPEN")
        if dataset.state is not DatasetState.READY or dataset.canonical_result_hash is None:
            raise CandidateGenerationApplicationError("STRATEGY_LAB_DATASET_NOT_READY")
        if dataset.schema.canonical_hash not in experiment.feature_schema_hashes:
            raise CandidateGenerationApplicationError("FEATURE_SCHEMA_OUTSIDE_FAMILY")
        if dataset.canonical_result_hash not in experiment.data_scope_hashes:
            raise CandidateGenerationApplicationError("DATASET_OUTSIDE_FAMILY")
        families = tuple(item.value for item in permitted_model_families)
        if not families or not set(families).issubset(set(experiment.permitted_families)):
            raise CandidateGenerationApplicationError("MODEL_FAMILY_OUTSIDE_FAMILY")
        if not 1 <= maximum_trials <= 500:
            raise CandidateGenerationApplicationError("SEARCH_TRIAL_BUDGET_INVALID")
        if existing_experiment_trial_count + maximum_trials > experiment.maximum_trials:
            raise CandidateGenerationApplicationError("EXPERIMENT_TRIAL_BUDGET_EXCEEDED")
        if not 0 <= maximum_failed_trials < maximum_trials:
            raise CandidateGenerationApplicationError("FAILED_TRIAL_BUDGET_INVALID")
        if not 1 <= wall_clock_budget_seconds <= 86_400:
            raise CandidateGenerationApplicationError("WALL_CLOCK_BUDGET_INVALID")
        if not 1 <= per_trial_budget_seconds <= wall_clock_budget_seconds:
            raise CandidateGenerationApplicationError("PER_TRIAL_BUDGET_INVALID")
        if not 1 <= artifact_byte_budget <= 2_000_000_000:
            raise CandidateGenerationApplicationError("ARTIFACT_BUDGET_INVALID")
        if objective_identity is ObjectiveIdentity.COST_ADJUSTED_EXCESS_RETURN:
            raise CandidateGenerationApplicationError(
                "COST_ADJUSTED_OBJECTIVE_REQUIRES_SECURITIES_RETURN_EVIDENCE"
            )
        if random_seed < 0 or not display_name or not operator_reason or not idempotency_key:
            raise CandidateGenerationApplicationError("SEARCH_PLAN_INPUT_INVALID")
        family_space = content_digest(experiment.parameter_space.get("parameters"))
        if family_space != search_space.canonical_hash:
            raise CandidateGenerationApplicationError("SEARCH_SPACE_OUTSIDE_FAMILY")
        payload = {
            "experiment": experiment.experiment_id,
            "experiment_hash": experiment.canonical_input_hash,
            "display_name": display_name,
            "objective": objective_identity.value,
            "families": families,
            "space": search_space.payload,
            "dataset": dataset.dataset_id,
            "dataset_hash": dataset.canonical_result_hash,
            "schema": dataset.schema.schema_id,
            "schema_hash": dataset.schema.canonical_hash,
            "fold_policy": "GROUPED_CHRONOLOGICAL_WALK_FORWARD_V1",
            "seed": random_seed,
            "sampler": {"identity": "TPESampler", "seed": random_seed},
            "pruner": {"identity": "NopPruner"},
            "budgets": {
                "trials": maximum_trials,
                "failed": maximum_failed_trials,
                "wall_seconds": wall_clock_budget_seconds,
                "trial_seconds": per_trial_budget_seconds,
                "artifact_bytes": artifact_byte_budget,
                "concurrency": 1,
            },
            "operator": operator_action_id,
            "reason": operator_reason,
            "key": idempotency_key,
        }
        input_hash = content_digest(payload)
        prior = self._repository.plan_by_idempotency(idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != input_hash:
                raise CandidateGenerationConflict("SEARCH_PLAN_IDEMPOTENCY_CONFLICT")
            return prior
        selection_hash = content_digest({"policy": SELECTION_POLICY_ID, "version": "1"})
        mirror_hash = content_digest({"policy": MLFLOW_MIRROR_POLICY_ID, "version": "1"})
        plan = AutomatedCandidateSearchPlan(
            search_plan_id=deterministic_id("automated_candidate_search_plan", payload),
            experiment_id=experiment.experiment_id,
            experiment_hash=experiment.canonical_input_hash,
            display_name=display_name,
            objective_identity=objective_identity,
            objective_version="1",
            objective_direction=objective_direction(objective_identity),
            permitted_model_families=families,
            search_space_payload=search_space.payload,
            search_space_hash=search_space.canonical_hash,
            theoretical_combinations=search_space.theoretical_combinations,
            calibration_choices=("NONE", "PLATT", "ISOTONIC"),
            feature_schema_id=dataset.schema.schema_id,
            feature_schema_hash=dataset.schema.canonical_hash,
            dataset_id=dataset.dataset_id,
            dataset_hash=dataset.canonical_result_hash,
            fold_policy_id="GROUPED_CHRONOLOGICAL_WALK_FORWARD_V1",
            fold_policy_hash=content_digest({"policy": "GROUPED_CHRONOLOGICAL_WALK_FORWARD_V1"}),
            random_seed=random_seed,
            sampler_identity="TPESampler",
            sampler_version="4.9.0",
            sampler_configuration={"seed": random_seed},
            sampler_hash=content_digest({"sampler": "TPESampler", "seed": random_seed}),
            pruner_identity="NopPruner",
            pruner_version="4.9.0",
            pruner_configuration={},
            pruner_hash=content_digest({"pruner": "NopPruner"}),
            maximum_trials=maximum_trials,
            maximum_failed_trials=maximum_failed_trials,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
            per_trial_budget_seconds=per_trial_budget_seconds,
            artifact_byte_budget=artifact_byte_budget,
            concurrency_limit=1,
            candidate_selection_policy_id=SELECTION_POLICY_ID,
            candidate_selection_policy_hash=selection_hash,
            mlflow_mirror_policy_id=MLFLOW_MIRROR_POLICY_ID,
            mlflow_mirror_policy_hash=mirror_hash,
            state=SearchPlanState.READY,
            created_at=created_at,
            opened_at=created_at,
            completed_at=None,
            canceled_at=None,
            operator_action_id=operator_action_id,
            operator_reason=operator_reason,
            idempotency_key=idempotency_key,
            canonical_input_hash=input_hash,
            canonical_result_hash=content_digest({"input": input_hash, "state": "READY"}),
        )
        return self._repository.save_plan(plan)


def create_local_mlflow_workspace(
    *, mlflow_version: str, created_at: datetime
) -> MLflowMirrorWorkspace:
    payload = {
        "version": "1",
        "tracking_mode": "LOCAL_SQLITE_TRACKING",
        "bind": "127.0.0.1",
        "backend": "backend/mlflow.db",
        "artifacts": "artifacts",
        "naming": "APEX_EXPERIMENT_ID_V1",
        "mlflow_version": mlflow_version,
    }
    configuration_hash = content_digest(payload)
    return MLflowMirrorWorkspace(
        workspace_id=deterministic_id("mlflow_mirror_workspace", payload),
        workspace_version="1",
        tracking_mode="LOCAL_SQLITE_TRACKING",
        loopback_bind_host="127.0.0.1",
        backend_relative_path="backend/mlflow.db",
        artifact_relative_root="artifacts",
        experiment_naming_policy="APEX_EXPERIMENT_ID_V1",
        mlflow_version=mlflow_version,
        state=MirrorWorkspaceState.READY,
        canonical_configuration_hash=configuration_hash,
        created_at=created_at,
    )


class StrategyLabCandidateTrialExecutor:
    """Maps a frozen proposal into the existing production Strategy Lab service."""

    def __init__(self, service: StrategyLabService) -> None:
        self._service = service

    def execute(
        self,
        *,
        plan: AutomatedCandidateSearchPlan,
        proposal: dict[str, str],
        research_trial: ResearchTrialAttempt,
    ) -> TrialExecutionResult:
        family = ModelFamily(proposal["model_family"])
        calibration = CalibrationMethod[proposal["calibration"]]
        if family is ModelFamily.LOGISTIC_REGRESSION:
            parameters: dict[str, object] = {
                "C": proposal["regularization_c"],
                "penalty": proposal["penalty"],
                "class_weight": proposal["class_weight"],
                "max_iter": proposal["logistic_max_iter"],
            }
        else:
            parameters = {
                "learning_rate": proposal["learning_rate"],
                "max_iter": proposal["histogram_max_iter"],
                "max_leaf_nodes": proposal["max_leaf_nodes"],
                "max_bins": proposal["max_bins"],
                "min_samples_leaf": proposal["min_samples_leaf"],
                "l2_regularization": proposal["l2_regularization"],
            }
        training = self._service.train_model(
            dataset_id=plan.dataset_id,
            family=family,
            hyperparameters=parameters,
            calibration=calibration,
            seed=plan.random_seed + research_trial.ordinal,
            idempotency_key=f"candidate-search-training-{research_trial.trial_id}",
        )
        evaluation, candidate = self._service.evaluate_candidate(
            training_run_id=training.training_run_id
        )
        if plan.objective_identity is ObjectiveIdentity.BRIER:
            objective = evaluation.brier_score
        elif plan.objective_identity is ObjectiveIdentity.LOG_LOSS:
            objective = evaluation.log_loss
        else:
            raise CandidateGenerationApplicationError(
                "COST_ADJUSTED_OBJECTIVE_REQUIRES_SECURITIES_RETURN_EVIDENCE"
            )
        if objective is None:
            raise CandidateGenerationApplicationError("OBJECTIVE_UNAVAILABLE")
        artifact = training.model_artifact
        if artifact is None:
            raise CandidateGenerationApplicationError("MODEL_ARTIFACT_UNAVAILABLE")
        return TrialExecutionResult(
            training_run=training,
            evaluation=evaluation,
            candidate=candidate,
            objective_value=objective,
            artifact_identities=(artifact.relative_location,),
            artifact_hashes=(artifact.sha256,),
            artifact_bytes=artifact.byte_size,
        )


class RunAutomatedCandidateSearch:
    def __init__(
        self,
        *,
        repository: CandidateGenerationRepository,
        research_ledger: ResearchTrialLedger,
        research_integrity: ResearchIntegrityService,
        optimizer: CandidateSearchOptimizerPort,
        executor: CandidateTrialExecutorPort,
        mirror: MLflowMirrorPort,
    ) -> None:
        self._repository = repository
        self._research_ledger = research_ledger
        self._research_integrity = research_integrity
        self._optimizer = optimizer
        self._executor = executor
        self._mirror = mirror

    def execute(
        self,
        *,
        plan: AutomatedCandidateSearchPlan,
        experiment: ResearchExperimentFamily,
        search_space: BoundedSearchSpace,
        workspace: MLflowMirrorWorkspace,
        requested_trials: int,
        idempotency_key: str,
        started_at: datetime,
    ) -> tuple[
        AutomatedCandidateSearchRun,
        CandidateGenerationDecision,
        tuple[AutomatedCandidateTrialBinding, ...],
    ]:
        if experiment.experiment_id != plan.experiment_id:
            raise CandidateGenerationApplicationError("SEARCH_FAMILY_MISMATCH")
        if search_space.canonical_hash != plan.search_space_hash:
            raise CandidateGenerationApplicationError("SEARCH_SPACE_HASH_MISMATCH")
        if not 1 <= requested_trials <= plan.maximum_trials:
            raise CandidateGenerationApplicationError("REQUESTED_TRIAL_COUNT_INVALID")
        input_payload = {
            "plan": plan.search_plan_id,
            "plan_hash": plan.canonical_input_hash,
            "experiment": experiment.experiment_id,
            "requested": requested_trials,
            "study": self._optimizer.study_identity,
            "key": idempotency_key,
        }
        input_hash = content_digest(input_payload)
        prior = self._repository.run_by_idempotency(plan.search_plan_id, idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != input_hash:
                raise CandidateGenerationConflict("SEARCH_RUN_IDEMPOTENCY_CONFLICT")
            decision = self._repository.decision(prior.search_run_id)
            if decision is not None:
                return prior, decision, self._repository.bindings(prior.search_run_id)
            if prior.state is not SearchRunState.RUNNING:
                raise CandidateGenerationApplicationError("SEARCH_RUN_RECOVERY_REQUIRED")
            run = self._resume_interrupted_run(prior)
        else:
            if plan.state is not SearchPlanState.READY:
                raise CandidateGenerationApplicationError("SEARCH_PLAN_NOT_READY")
            run = AutomatedCandidateSearchRun(
                search_run_id=deterministic_id("automated_candidate_search_run", input_payload),
                search_plan_id=plan.search_plan_id,
                search_plan_hash=plan.canonical_input_hash,
                experiment_id=experiment.experiment_id,
                experiment_hash=experiment.canonical_input_hash,
                optuna_study_identity=self._optimizer.study_identity,
                optuna_version=self._optimizer.runtime_version,
                mlflow_experiment_identity=f"apex-{experiment.experiment_id}",
                requested_trial_count=requested_trials,
                reserved_trial_count=0,
                completed_trial_count=0,
                pruned_trial_count=0,
                failed_trial_count=0,
                integrity_rejected_trial_count=0,
                elapsed_seconds=Decimal(),
                artifact_bytes_written=0,
                state=SearchRunState.REQUESTED,
                best_raw_trial_id=None,
                best_integrity_eligible_trial_id=None,
                multiple_testing_evaluation_id=None,
                final_recommendation=None,
                reason_codes=(),
                started_at=started_at,
                completed_at=None,
                idempotency_key=idempotency_key,
                canonical_input_hash=input_hash,
                canonical_result_hash=None,
            )
            run = self._repository.save_run(run)
            run = self._repository.update_run(replace(run, state=SearchRunState.RUNNING))
            self._repository.update_plan(replace(plan, state=SearchPlanState.RUNNING))
        start_clock = monotonic()
        completed: list[tuple[ResearchTrialAttempt, TrialExecutionResult]] = []
        for _ in range(requested_trials - run.reserved_trial_count):
            elapsed = Decimal(str(monotonic() - start_clock))
            if elapsed >= plan.wall_clock_budget_seconds:
                run = replace(run, state=SearchRunState.PARTIAL)
                break
            if run.failed_trial_count > 0 and run.failed_trial_count >= plan.maximum_failed_trials:
                run = replace(run, state=SearchRunState.PARTIAL)
                break
            if run.artifact_bytes_written >= plan.artifact_byte_budget:
                run = replace(run, state=SearchRunState.PARTIAL)
                break
            proposal = self._optimizer.ask(search_space)
            raw_hash = content_digest(proposal.parameters)
            research_trial = self._research_ledger.reserve(
                experiment_id=experiment.experiment_id,
                source_type=ResearchTrialSourceType.STRATEGY_LAB_TRAINING,
                source_identity=f"{run.search_run_id}:optuna:{proposal.trial_number}",
                source_hash=content_digest(
                    {"study": self._optimizer.study_identity, "trial": proposal.trial_number}
                ),
                strategy_model_family=str(
                    proposal.parameters.get("model_family", "INVALID_PROPOSAL")
                ),
                configuration=proposal.parameters,
                feature_set_hash=plan.feature_schema_hash,
                dataset_hash=plan.dataset_hash,
                simulator_identity="APEX_STRATEGY_LAB",
                simulator_version="1",
                execution_cost_policy_hash=content_digest({"objective": OBJECTIVE_POLICY_ID}),
                random_seed=plan.random_seed + proposal.trial_number,
                idempotency_key=f"{run.search_run_id}-trial-{proposal.trial_number}",
                started_at=datetime.now(tz=UTC),
                selected_for_comparison=True,
            )
            binding_payload = {
                "run": run.search_run_id,
                "trial_number": proposal.trial_number,
                "research_trial": research_trial.trial_id,
                "parameters": proposal.parameters,
            }
            binding = self._repository.save_binding(
                AutomatedCandidateTrialBinding(
                    binding_id=deterministic_id(
                        "automated_candidate_trial_binding", binding_payload
                    ),
                    search_run_id=run.search_run_id,
                    optuna_trial_number=proposal.trial_number,
                    optimizer_token=proposal.optimizer_token,
                    research_trial_id=research_trial.trial_id,
                    training_run_id=None,
                    evaluation_id=None,
                    candidate_id=None,
                    dataset_id=plan.dataset_id,
                    feature_schema_id=plan.feature_schema_id,
                    parameter_payload=proposal.parameters,
                    parameter_hash=raw_hash,
                    sampler_metadata=proposal.sampler_metadata,
                    objective_value=None,
                    objective_state=ObjectiveState.PROPOSED,
                    artifact_identities=(),
                    artifact_hashes=(),
                    mlflow_mirror_record_id=None,
                    failure_reason=None,
                    canonical_input_hash=content_digest(binding_payload),
                    canonical_result_hash=None,
                )
            )
            run = self._repository.update_run(
                replace(run, reserved_trial_count=run.reserved_trial_count + 1)
            )
            proposal_mirror = self._mirror.mirror(
                workspace=workspace,
                experiment=experiment,
                plan=plan,
                trial=research_trial,
                binding=binding,
                metrics={"trial_budget_used": research_trial.ordinal},
                artifacts={"parameters.json": canonical_json_bytes(proposal.parameters)},
            )
            proposal_mirror = self._repository.save_mirror_record(proposal_mirror)
            binding = self._repository.update_binding(
                replace(
                    binding,
                    mlflow_mirror_record_id=proposal_mirror.mirror_record_id,
                    objective_state=ObjectiveState.TRAINING,
                )
            )
            terminal_state = ObjectiveState.FAILED
            value: Decimal | None = None
            execution: TrialExecutionResult | None = None
            failure_reason: str | None = None
            stop_after_trial = False
            trial_started = monotonic()
            try:
                normalized = search_space.validate_proposal(proposal.parameters)
                execution = self._executor.execute(
                    plan=plan, proposal=normalized, research_trial=research_trial
                )
                value = execution.objective_value
                run = replace(
                    run,
                    artifact_bytes_written=(run.artifact_bytes_written + execution.artifact_bytes),
                )
                if run.artifact_bytes_written > plan.artifact_byte_budget:
                    terminal_state = ObjectiveState.CANCELED
                    value = None
                    failure_reason = "ARTIFACT_BYTE_BUDGET_EXCEEDED"
                    stop_after_trial = True
                    run = replace(
                        run,
                        state=SearchRunState.PARTIAL,
                        reason_codes=tuple(
                            sorted(
                                set(
                                    (
                                        *run.reason_codes,
                                        "ARTIFACT_BYTE_BUDGET_EXCEEDED",
                                    )
                                )
                            )
                        ),
                    )
                    research_trial = self._research_ledger.finish(
                        research_trial.trial_id,
                        state=ResearchTrialState.CANCELED,
                        completed_at=datetime.now(tz=UTC),
                        result_identity=None,
                        result_hash=None,
                        failure_reason=failure_reason,
                    )
                elif monotonic() - trial_started > plan.per_trial_budget_seconds:
                    terminal_state = ObjectiveState.CANCELED
                    value = None
                    failure_reason = "PER_TRIAL_TIME_BUDGET_EXCEEDED"
                    stop_after_trial = True
                    run = replace(
                        run,
                        state=SearchRunState.PARTIAL,
                        reason_codes=tuple(
                            sorted(
                                set(
                                    (
                                        *run.reason_codes,
                                        "PER_TRIAL_TIME_BUDGET_EXCEEDED",
                                    )
                                )
                            )
                        ),
                    )
                    research_trial = self._research_ledger.finish(
                        research_trial.trial_id,
                        state=ResearchTrialState.CANCELED,
                        completed_at=datetime.now(tz=UTC),
                        result_identity=None,
                        result_hash=None,
                        failure_reason=failure_reason,
                    )
                else:
                    terminal_state = ObjectiveState.COMPLETED
                    research_trial = self._research_ledger.finish(
                        research_trial.trial_id,
                        state=ResearchTrialState.COMPLETED,
                        completed_at=datetime.now(tz=UTC),
                        result_identity=execution.candidate.candidate_id,
                        result_hash=execution.candidate.canonical_candidate_hash,
                    )
                    if execution.gross_returns and execution.net_returns:
                        self._research_integrity.retain_return_evidence(
                            trial_id=research_trial.trial_id,
                            gross_observations=execution.gross_returns,
                            net_observations=execution.net_returns,
                            frequency=experiment.frequency,
                            turnover=execution.turnover,
                        )
                    completed.append((research_trial, execution))
                    run = replace(
                        run,
                        completed_trial_count=run.completed_trial_count + 1,
                    )
            except CandidateGenerationError as error:
                failure_reason = str(error)
                terminal_state = ObjectiveState.REJECTED_INTEGRITY
                research_trial = self._research_ledger.finish(
                    research_trial.trial_id,
                    state=ResearchTrialState.REJECTED_INTEGRITY,
                    completed_at=datetime.now(tz=UTC),
                    result_identity=None,
                    result_hash=None,
                    failure_reason=failure_reason,
                    integrity_blocked=True,
                )
                run = replace(
                    run,
                    integrity_rejected_trial_count=(run.integrity_rejected_trial_count + 1),
                )
            except Exception as error:
                failure_reason = f"{type(error).__name__}:{error}"[:500]
                research_trial = self._research_ledger.finish(
                    research_trial.trial_id,
                    state=ResearchTrialState.FAILED,
                    completed_at=datetime.now(tz=UTC),
                    result_identity=None,
                    result_hash=None,
                    failure_reason=failure_reason,
                )
                run = replace(run, failed_trial_count=run.failed_trial_count + 1)
                if run.failed_trial_count >= plan.maximum_failed_trials:
                    stop_after_trial = True
                    run = replace(
                        run,
                        state=SearchRunState.PARTIAL,
                        reason_codes=tuple(
                            sorted(set((*run.reason_codes, "FAILED_TRIAL_BUDGET_REACHED")))
                        ),
                    )
            terminal_binding = replace(
                binding,
                training_run_id=execution.training_run.training_run_id if execution else None,
                evaluation_id=execution.evaluation.evaluation_id if execution else None,
                candidate_id=execution.candidate.candidate_id if execution else None,
                objective_value=value,
                objective_state=terminal_state,
                artifact_identities=execution.artifact_identities if execution else (),
                artifact_hashes=execution.artifact_hashes if execution else (),
                failure_reason=failure_reason,
                canonical_result_hash=content_digest(
                    {
                        "state": terminal_state.value,
                        "value": str(value) if value is not None else None,
                        "failure": failure_reason,
                    }
                ),
            )
            terminal_binding = self._repository.update_binding(terminal_binding)
            try:
                self._optimizer.tell(proposal, state=terminal_state, objective_value=value)
            except Exception as error:
                reason = f"OPTUNA_TELL_FAILED:{type(error).__name__}"
                run = self._repository.update_run(
                    replace(
                        run,
                        state=SearchRunState.BLOCKED_INTEGRITY,
                        reason_codes=tuple(sorted(set((*run.reason_codes, reason)))),
                        elapsed_seconds=Decimal(str(monotonic() - start_clock)),
                    )
                )
                self._repository.update_plan(
                    replace(
                        plan,
                        state=SearchPlanState.BLOCKED_INTEGRITY,
                        canonical_result_hash=content_digest(
                            {"run": run.search_run_id, "reason": reason}
                        ),
                    )
                )
                raise CandidateGenerationApplicationError(reason) from error
            terminal_mirror = self._mirror.mirror(
                workspace=workspace,
                experiment=experiment,
                plan=plan,
                trial=research_trial,
                binding=terminal_binding,
                metrics={
                    "primary_objective": value,
                    "brier": execution.evaluation.brier_score if execution else None,
                    "log_loss": execution.evaluation.log_loss if execution else None,
                    "baseline_brier": (
                        execution.evaluation.baseline_brier_score if execution else None
                    ),
                    "baseline_log_loss": (
                        execution.evaluation.baseline_log_loss if execution else None
                    ),
                    "brier_difference": (
                        execution.evaluation.brier_difference if execution else None
                    ),
                    "log_loss_difference": (
                        execution.evaluation.log_loss_difference if execution else None
                    ),
                    "completed_fold_count": (
                        execution.evaluation.completed_fold_count if execution else None
                    ),
                    "prediction_count": (
                        execution.evaluation.prediction_count if execution else None
                    ),
                    "distinct_group_count": (
                        execution.evaluation.distinct_group_count if execution else None
                    ),
                    "trial_budget_used": research_trial.ordinal,
                },
                artifacts={
                    "terminal-evidence.json": canonical_json_bytes(
                        {
                            "state": terminal_state.value,
                            "candidate": terminal_binding.candidate_id,
                            "hashes": terminal_binding.artifact_hashes,
                        }
                    ),
                    "evaluation-report.json": canonical_json_bytes(
                        {
                            "evaluation_id": (
                                execution.evaluation.evaluation_id if execution else None
                            ),
                            "fold_metrics": (
                                execution.evaluation.full_fold_metrics if execution else {}
                            ),
                            "recent_fold_metrics": (
                                execution.evaluation.recent_fold_metrics if execution else {}
                            ),
                            "calibration": (
                                execution.evaluation.calibration_summary if execution else {}
                            ),
                            "integrity_warnings": (
                                execution.evaluation.integrity_warnings if execution else ()
                            ),
                        }
                    ),
                    "artifact-reference.json": canonical_json_bytes(
                        {
                            "identities": terminal_binding.artifact_identities,
                            "sha256": terminal_binding.artifact_hashes,
                            "apex_authoritative": True,
                        }
                    ),
                },
                prior=proposal_mirror,
            )
            self._repository.save_mirror_record(terminal_mirror)
            run = self._repository.update_run(run)
            if Decimal(str(monotonic() - start_clock)) >= plan.wall_clock_budget_seconds:
                stop_after_trial = True
                run = self._repository.update_run(
                    replace(
                        run,
                        state=SearchRunState.PARTIAL,
                        reason_codes=tuple(
                            sorted(set((*run.reason_codes, "WALL_CLOCK_BUDGET_REACHED")))
                        ),
                    )
                )
            if stop_after_trial:
                break
        integrity = self._research_integrity.evaluate(
            experiment_id=experiment.experiment_id,
            cutoff_at=datetime.now(tz=UTC),
        )
        raw_best: tuple[ResearchTrialAttempt, TrialExecutionResult] | None = None
        if completed:
            reverse = plan.objective_direction is ObjectiveDirection.MAXIMIZE
            raw_best = sorted(completed, key=lambda item: item[1].objective_value, reverse=reverse)[
                0
            ]
        eligible = tuple(
            item
            for item in completed
            if item[1].candidate.state is CandidateState.ELIGIBLE_FOR_SHADOW_REVIEW
        )
        selected: tuple[ResearchTrialAttempt, TrialExecutionResult] | None = None
        if (
            integrity.recommendation is ResearchRecommendation.ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW
            and eligible
        ):
            reverse = plan.objective_direction is ObjectiveDirection.MAXIMIZE
            selected = sorted(eligible, key=lambda item: item[1].objective_value, reverse=reverse)[
                0
            ]
        recommendation = _decision_recommendation(integrity.recommendation, selected)
        reasons = integrity.reason_codes
        decision_payload = {
            "run": run.search_run_id,
            "candidates": tuple(item[0].trial_id for item in completed),
            "raw": raw_best[0].trial_id if raw_best else None,
            "eligible": tuple(item[0].trial_id for item in eligible),
            "selected": selected[0].trial_id if selected else None,
            "integrity": integrity.evaluation_id,
            "recommendation": recommendation.value,
            "reasons": reasons,
        }
        decision = self._repository.save_decision(
            CandidateGenerationDecision(
                decision_id=deterministic_id("candidate_generation_decision", decision_payload),
                search_run_id=run.search_run_id,
                candidate_trial_ids=tuple(item[0].trial_id for item in completed),
                candidate_trial_set_hash=content_digest(
                    tuple(item[0].trial_id for item in completed)
                ),
                raw_best_trial_id=raw_best[0].trial_id if raw_best else None,
                integrity_eligible_trial_ids=tuple(item[0].trial_id for item in eligible),
                selected_candidate_trial_id=selected[0].trial_id if selected else None,
                selection_metric=plan.objective_identity.value,
                multiple_testing_evaluation_id=integrity.evaluation_id,
                conformance_state="APEX_STRATEGY_LAB_AUTHORITATIVE",
                integrity_policy_state=integrity.state.value,
                recommendation=recommendation,
                hard_gate_reason_codes=reasons,
                operator_action_id=None,
                canonical_input_hash=content_digest(
                    {"run": run.search_run_id, "integrity": integrity.canonical_result_hash}
                ),
                canonical_result_hash=content_digest(decision_payload),
                created_at=datetime.now(tz=UTC),
            )
        )
        mirror_targets = {
            trial_id
            for trial_id in (
                decision.raw_best_trial_id,
                decision.selected_candidate_trial_id,
            )
            if trial_id is not None
        }
        retained_bindings = self._repository.bindings(run.search_run_id)
        bindings_by_trial = {item.research_trial_id: item for item in retained_bindings}
        mirrors_by_trial = {
            item.source_identity: item
            for item in self._repository.mirror_records(experiment.experiment_id)
        }
        completed_by_trial = {trial.trial_id: trial for trial, _result in completed}
        for trial_id in sorted(mirror_targets):
            prior_mirror = mirrors_by_trial.get(trial_id)
            target_binding = bindings_by_trial.get(trial_id)
            target_trial = completed_by_trial.get(trial_id)
            if prior_mirror is None or target_binding is None or target_trial is None:
                continue
            enriched = self._mirror.mirror(
                workspace=workspace,
                experiment=experiment,
                plan=plan,
                trial=target_trial,
                binding=target_binding,
                metrics={
                    "effective_independent_trials": (integrity.effective_independent_trials),
                    "probabilistic_sharpe_ratio": (integrity.probabilistic_sharpe_ratio),
                    "deflated_sharpe_ratio": integrity.deflated_sharpe_ratio,
                    "probability_backtest_overfitting": (
                        integrity.probability_backtest_overfitting
                    ),
                },
                artifacts={
                    "candidate-decision.json": canonical_json_bytes(
                        {
                            "decision_id": decision.decision_id,
                            "raw_best_trial_id": decision.raw_best_trial_id,
                            "integrity_eligible_trial_ids": (decision.integrity_eligible_trial_ids),
                            "selected_candidate_trial_id": (decision.selected_candidate_trial_id),
                            "recommendation": decision.recommendation.value,
                            "hard_gate_reason_codes": (decision.hard_gate_reason_codes),
                            "authority": "NONE",
                        }
                    )
                },
                prior=prior_mirror,
            )
            self._repository.save_mirror_record(enriched)
        final_state = (
            SearchRunState.PARTIAL
            if run.state is SearchRunState.PARTIAL
            else SearchRunState.COMPLETED
            if run.reserved_trial_count == requested_trials
            else SearchRunState.PARTIAL
        )
        if recommendation in (
            CandidateGenerationRecommendation.REJECT_SEARCH_BIAS,
            CandidateGenerationRecommendation.BLOCKED_SIMULATOR_MISMATCH,
        ):
            final_state = SearchRunState.BLOCKED_INTEGRITY
        result_payload = {
            "decision": decision.canonical_result_hash,
            "counts": (
                run.reserved_trial_count,
                run.completed_trial_count,
                run.failed_trial_count,
                run.integrity_rejected_trial_count,
            ),
            "state": final_state.value,
        }
        run = self._repository.update_run(
            replace(
                run,
                elapsed_seconds=Decimal(str(monotonic() - start_clock)),
                state=final_state,
                best_raw_trial_id=decision.raw_best_trial_id,
                best_integrity_eligible_trial_id=decision.selected_candidate_trial_id,
                multiple_testing_evaluation_id=integrity.evaluation_id,
                final_recommendation=recommendation,
                reason_codes=reasons,
                completed_at=datetime.now(tz=UTC),
                canonical_result_hash=content_digest(result_payload),
            )
        )
        self._repository.update_plan(
            replace(
                plan,
                state=SearchPlanState(final_state.value),
                completed_at=run.completed_at,
                canonical_result_hash=content_digest(
                    {"run": run.canonical_result_hash, "state": final_state.value}
                ),
            )
        )
        return run, decision, self._repository.bindings(run.search_run_id)

    def _resume_interrupted_run(
        self, run: AutomatedCandidateSearchRun
    ) -> AutomatedCandidateSearchRun:
        """Seal an interrupted foreground attempt before consuming remaining budget."""

        ledger = self._research_ledger
        changed = False
        for binding in self._repository.bindings(run.search_run_id):
            if binding.objective_state not in (ObjectiveState.PROPOSED, ObjectiveState.TRAINING):
                continue
            trial = ledger.finish(
                binding.research_trial_id,
                state=ResearchTrialState.CANCELED,
                completed_at=datetime.now(tz=UTC),
                result_identity=None,
                result_hash=None,
                failure_reason="FOREGROUND_WORKER_INTERRUPTED",
            )
            del trial
            self._repository.update_binding(
                replace(
                    binding,
                    objective_state=ObjectiveState.CANCELED,
                    failure_reason="FOREGROUND_WORKER_INTERRUPTED",
                    canonical_result_hash=content_digest(
                        {"state": "CANCELED", "failure": "FOREGROUND_WORKER_INTERRUPTED"}
                    ),
                )
            )
            changed = True
        reasons = self._optimizer.reconcile(self._repository.bindings(run.search_run_id))
        if reasons:
            raise CandidateGenerationApplicationError("OPTUNA_RESUME_RECONCILIATION_FAILED")
        if not changed:
            return run
        return self._repository.update_run(
            replace(
                run,
                reason_codes=tuple(
                    sorted(set((*run.reason_codes, "FOREGROUND_WORKER_INTERRUPTED")))
                ),
            )
        )


class RecoverAutomatedCandidateSearch:
    def __init__(
        self,
        repository: CandidateGenerationRepository,
        optimizer: CandidateSearchOptimizerPort,
    ) -> None:
        self._repository = repository
        self._optimizer = optimizer

    def execute(self, search_run_id: str) -> tuple[str, ...]:
        run = self._repository.run(search_run_id)
        if run is None:
            raise CandidateGenerationApplicationError("SEARCH_RUN_NOT_FOUND")
        reasons = self._optimizer.reconcile(self._repository.bindings(search_run_id))
        if reasons:
            self._repository.update_run(
                replace(
                    run,
                    state=SearchRunState.BLOCKED_INTEGRITY,
                    reason_codes=tuple(sorted(set((*run.reason_codes, *reasons)))),
                )
            )
        return reasons


class CancelAutomatedCandidateSearch:
    def __init__(self, repository: CandidateGenerationRepository) -> None:
        self._repository = repository

    def execute(
        self, *, search_run_id: str, reason: str, canceled_at: datetime
    ) -> AutomatedCandidateSearchRun:
        run = self._repository.run(search_run_id)
        if run is None:
            raise CandidateGenerationApplicationError("SEARCH_RUN_NOT_FOUND")
        if run.state not in (SearchRunState.REQUESTED, SearchRunState.RUNNING):
            raise CandidateGenerationApplicationError("SEARCH_RUN_NOT_CANCELABLE")
        if not reason:
            raise CandidateGenerationApplicationError("CANCELLATION_REASON_REQUIRED")
        updated = replace(
            run,
            state=SearchRunState.CANCELED,
            reason_codes=tuple(sorted(set((*run.reason_codes, f"CANCELED:{reason}")))),
            completed_at=canceled_at,
            canonical_result_hash=content_digest(
                {"run": run.search_run_id, "state": "CANCELED", "reason": reason}
            ),
        )
        updated = self._repository.update_run(updated)
        plan = self._repository.plan(run.search_plan_id)
        if plan is not None and plan.state in (
            SearchPlanState.READY,
            SearchPlanState.RUNNING,
        ):
            self._repository.update_plan(
                replace(
                    plan,
                    state=SearchPlanState.CANCELED,
                    canceled_at=canceled_at,
                    canonical_result_hash=content_digest(
                        {
                            "plan": plan.search_plan_id,
                            "run": run.search_run_id,
                            "state": "CANCELED",
                            "reason": reason,
                        }
                    ),
                )
            )
        return updated


def reconcile_mlflow_mirror(
    *,
    repository: CandidateGenerationRepository,
    mirror: MLflowMirrorPort,
    workspace: MLflowMirrorWorkspace,
    experiment: ResearchExperimentFamily,
    research_trials: Sequence[ResearchTrialAttempt],
) -> tuple[MirrorReconciliation, ...]:
    trials_by_id = {item.trial_id: item for item in research_trials}
    bindings_by_trial: dict[str, AutomatedCandidateTrialBinding] = {}
    plans_by_run: dict[str, AutomatedCandidateSearchPlan] = {}
    for run in repository.runs():
        plan = repository.plan(run.search_plan_id)
        if plan is None or plan.experiment_id != experiment.experiment_id:
            continue
        plans_by_run[run.search_run_id] = plan
        for binding in repository.bindings(run.search_run_id):
            bindings_by_trial[binding.research_trial_id] = binding
    reconciliations: list[MirrorReconciliation] = []
    for retained in repository.mirror_records(experiment.experiment_id):
        record = retained
        initial_reconciliation = mirror.reconcile(workspace, record)
        should_retry = (
            record.state is MirrorRecordState.FAILED_RETRYABLE
            or initial_reconciliation.state is MirrorReconciliationState.MIRROR_MISSING
        )
        if should_retry:
            trial = trials_by_id.get(record.source_identity)
            retry_binding = bindings_by_trial.get(record.source_identity)
            plan = plans_by_run.get(retry_binding.search_run_id) if retry_binding else None
            if trial is not None and retry_binding is not None and plan is not None:
                retry_prior = (
                    record
                    if record.state is MirrorRecordState.FAILED_RETRYABLE
                    else replace(record, state=MirrorRecordState.FAILED_RETRYABLE)
                )
                record = mirror.mirror(
                    workspace=workspace,
                    experiment=experiment,
                    plan=plan,
                    trial=trial,
                    binding=retry_binding,
                    metrics={
                        "primary_objective": retry_binding.objective_value,
                        "trial_budget_used": trial.ordinal,
                    },
                    artifacts={
                        "reconciliation-retry.json": canonical_json_bytes(
                            {
                                "objective_state": (retry_binding.objective_state.value),
                                "artifact_identities": (retry_binding.artifact_identities),
                                "artifact_hashes": retry_binding.artifact_hashes,
                                "candidate_id": retry_binding.candidate_id,
                                "apex_authority": "NONE",
                            }
                        )
                    },
                    prior=retry_prior,
                )
                record = repository.save_mirror_record(record)
                reconciliations.append(mirror.reconcile(workspace, record))
                continue
        reconciliations.append(initial_reconciliation)
    return tuple(reconciliations)


def _decision_recommendation(
    recommendation: ResearchRecommendation,
    selected: tuple[ResearchTrialAttempt, TrialExecutionResult] | None,
) -> CandidateGenerationRecommendation:
    if selected is not None:
        return CandidateGenerationRecommendation.ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW
    if recommendation is ResearchRecommendation.REJECT_SELECTION_BIAS:
        return CandidateGenerationRecommendation.REJECT_SEARCH_BIAS
    if recommendation is ResearchRecommendation.REQUIRE_MORE_INDEPENDENT_EVIDENCE:
        return CandidateGenerationRecommendation.REQUIRE_MORE_EVIDENCE
    if recommendation is ResearchRecommendation.CONTINUE_RESEARCH:
        return CandidateGenerationRecommendation.CONTINUE_RESEARCH
    return CandidateGenerationRecommendation.NO_ELIGIBLE_CANDIDATE
