"""Bounded automated learning-cycle orchestration.

This module links retained research evidence to the existing Strategy Lab,
candidate-search, and SHADOW evaluation services.  It does not own datasets,
trials, models, deployments, predictions, outcomes, or financial state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from apex.application.candidate_generation import CandidateGenerationRecommendation
from apex.application.strategy_lab import ModelFamily
from apex.application.strategy_lab_shadow import (
    ChallengerRecommendation,
    DeploymentState,
    StrategyLabChallengerEvaluation,
    StrategyLabModelDeployment,
)
from apex.domain.values import content_digest, deterministic_id

AUTOMATED_LEARNING_WARNING = (
    "AUTOMATED LEARNING CREATES RESEARCH EVIDENCE — NOT DEPLOYMENT AUTHORITY"
)
CHAMPION_WARNING = "CHALLENGER RECOMMENDATIONS NEVER REPLACE THE CHAMPION AUTOMATICALLY"


class BatchLearningError(RuntimeError):
    """A bounded batch-learning invariant rejected an operation."""


class BatchLearningConflict(BatchLearningError):
    """An idempotent operation was replayed with incompatible input."""


class BatchLearningPlanState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    REVOKED = "REVOKED"
    COMPLETED = "COMPLETED"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"


class EvidenceWindowState(StrEnum):
    READY = "READY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    REJECTED_INTEGRITY = "REJECTED_INTEGRITY"


class BatchLearningTriggerType(StrEnum):
    MANUAL = "MANUAL"
    NEW_RESOLVED_EVIDENCE = "NEW_RESOLVED_EVIDENCE"
    CHAMPION_DRIFT = "CHAMPION_DRIFT"
    CHAMPION_DEGRADATION = "CHAMPION_DEGRADATION"


class BatchLearningCycleState(StrEnum):
    REQUESTED = "REQUESTED"
    WAITING_FOR_EVIDENCE = "WAITING_FOR_EVIDENCE"
    BUILDING_DATASET = "BUILDING_DATASET"
    SEARCHING = "SEARCHING"
    SEARCH_COMPLETED = "SEARCH_COMPLETED"
    NO_ELIGIBLE_CANDIDATE = "NO_ELIGIBLE_CANDIDATE"
    ELIGIBLE_FOR_SHADOW_REVIEW = "ELIGIBLE_FOR_SHADOW_REVIEW"
    WAITING_FOR_SHADOW_APPROVAL = "WAITING_FOR_SHADOW_APPROVAL"
    FORWARD_EVALUATING = "FORWARD_EVALUATING"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"


class ForwardWindowState(StrEnum):
    OPEN = "OPEN"
    COLLECTING = "COLLECTING"
    READY_TO_EVALUATE = "READY_TO_EVALUATE"
    COMPLETED = "COMPLETED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"
    CANCELED = "CANCELED"


class BatchLearningDecisionStage(StrEnum):
    CYCLE_TRIGGER = "CYCLE_TRIGGER"
    CANDIDATE_GENERATION = "CANDIDATE_GENERATION"
    FORWARD_EVALUATION = "FORWARD_EVALUATION"


class BatchLearningRecommendation(StrEnum):
    WAIT_FOR_MORE_EVIDENCE = "WAIT_FOR_MORE_EVIDENCE"
    NO_SEARCH_REQUIRED = "NO_SEARCH_REQUIRED"
    NO_ELIGIBLE_CANDIDATE = "NO_ELIGIBLE_CANDIDATE"
    ELIGIBLE_FOR_SHADOW_REVIEW = "ELIGIBLE_FOR_SHADOW_REVIEW"
    CONTINUE_FORWARD_EVALUATION = "CONTINUE_FORWARD_EVALUATION"
    CONTINUE_CURRENT_CHAMPION = "CONTINUE_CURRENT_CHAMPION"
    RECOMMEND_CHAMPION_REVIEW = "RECOMMEND_CHAMPION_REVIEW"
    PAUSE_LEARNING_FOR_DRIFT = "PAUSE_LEARNING_FOR_DRIFT"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"


@dataclass(frozen=True, slots=True)
class BatchLearningPlan:
    plan_id: str
    name: str
    vertical: str
    research_family: str
    source_adapter_id: str
    source_adapter_version: str
    source_scope: Mapping[str, object]
    source_scope_hash: str
    feature_schema_id: str
    feature_schema_hash: str
    dataset_policy_id: str
    dataset_policy_hash: str
    experiment_template: Mapping[str, object]
    experiment_template_hash: str
    permitted_model_families: tuple[str, ...]
    search_template: Mapping[str, object]
    search_template_hash: str
    objective_policy_id: str
    objective_policy_hash: str
    fold_policy_id: str
    fold_policy_hash: str
    minimum_new_resolved_observations: int
    minimum_new_independent_groups: int
    maximum_observations_per_cycle: int
    maximum_trials_per_cycle: int
    maximum_failed_trials_per_cycle: int
    maximum_cycles: int
    minimum_cooldown_seconds: int
    drift_policy: Mapping[str, object]
    drift_policy_hash: str
    forward_policy: Mapping[str, object]
    forward_policy_hash: str
    champion_comparison_policy: Mapping[str, object]
    champion_comparison_policy_hash: str
    mlflow_mirror_policy_id: str
    mlflow_mirror_policy_hash: str
    concurrency_limit: int
    state: BatchLearningPlanState
    created_at: datetime
    opened_at: datetime | None
    paused_at: datetime | None
    revoked_at: datetime | None
    completed_at: datetime | None
    operator_action_id: str
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class BatchLearningEvidenceWindow:
    window_id: str
    plan_id: str
    prior_watermark: str | None
    new_watermark: str
    cutoff_at: datetime
    source_artifact_id: str
    source_artifact_hash: str
    included_observation_count: int
    included_independent_group_count: int
    excluded_unresolved_count: int
    excluded_duplicate_count: int
    excluded_unavailable_count: int
    excluded_integrity_count: int
    earliest_evidence_at: datetime | None
    latest_evidence_at: datetime | None
    group_set_hash: str
    observation_set_hash: str
    state: EvidenceWindowState
    reason_codes: tuple[str, ...]
    canonical_input_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class BatchLearningCycle:
    cycle_id: str
    plan_id: str
    ordinal: int
    trigger_type: BatchLearningTriggerType
    trigger_evidence: Mapping[str, object]
    trigger_evidence_hash: str
    evidence_window_id: str
    evidence_window_hash: str
    dataset_id: str | None
    dataset_hash: str | None
    experiment_id: str | None
    experiment_hash: str | None
    search_plan_id: str | None
    search_plan_hash: str | None
    search_run_id: str | None
    candidate_decision_id: str | None
    eligible_candidate_id: str | None
    approved_challenger_deployment_id: str | None
    forward_window_id: str | None
    state: BatchLearningCycleState
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    failure_code: str | None
    reason_codes: tuple[str, ...]
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str | None


@dataclass(frozen=True, slots=True)
class BatchLearningCycleBinding:
    binding_id: str
    cycle_id: str
    evidence_window_id: str
    dataset_id: str
    experiment_id: str
    research_trial_ids: tuple[str, ...]
    search_plan_id: str
    search_run_id: str
    candidate_ids: tuple[str, ...]
    candidate_decision_id: str
    shadow_review_id: str | None
    approved_deployment_id: str | None
    inference_batch_ids: tuple[str, ...]
    forward_evaluation_ids: tuple[str, ...]
    challenger_evaluation_id: str | None
    mlflow_record_ids: tuple[str, ...]
    canonical_binding_hash: str


@dataclass(frozen=True, slots=True)
class ForwardShadowEvaluationWindow:
    window_id: str
    cycle_id: str
    champion_deployment_id: str | None
    champion_deployment_hash: str | None
    challenger_deployment_id: str
    challenger_deployment_hash: str
    source_scope: Mapping[str, object]
    source_scope_hash: str
    window_start_at: datetime
    cutoff_at: datetime
    window_end_at: datetime
    minimum_predictions: int
    minimum_resolved_outcomes: int
    maximum_duration_seconds: int
    champion_prediction_count: int
    challenger_prediction_count: int
    champion_resolved_count: int
    challenger_resolved_count: int
    unmatched_count: int
    excluded_count: int
    outcome_set_hash: str | None
    state: ForwardWindowState
    evaluation_ids: tuple[str, ...]
    drift_diagnostic_ids: tuple[str, ...]
    challenger_evaluation_id: str | None
    canonical_input_hash: str
    canonical_result_hash: str | None
    opened_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class BatchLearningDecision:
    decision_id: str
    cycle_id: str
    decision_stage: BatchLearningDecisionStage
    evidence_ids: tuple[str, ...]
    evidence_set_hash: str
    candidate_generation_decision_id: str | None
    challenger_evaluation_id: str | None
    champion_deployment_id: str | None
    challenger_deployment_id: str | None
    research_integrity_state: str
    drift_state: str | None
    recommendation: BatchLearningRecommendation
    reason_codes: tuple[str, ...]
    operator_action_id: str | None
    canonical_input_hash: str
    canonical_result_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    cutoff_at: datetime
    source_artifact_id: str
    source_artifact_hash: str
    observation_ids: tuple[str, ...]
    group_ids: tuple[str, ...]
    new_watermark: str
    excluded_unresolved_count: int = 0
    excluded_duplicate_count: int = 0
    excluded_unavailable_count: int = 0
    excluded_integrity_count: int = 0
    earliest_evidence_at: datetime | None = None
    latest_evidence_at: datetime | None = None
    integrity_valid: bool = True


@dataclass(frozen=True, slots=True)
class CycleExecutionResult:
    dataset_id: str
    dataset_hash: str
    experiment_id: str
    experiment_hash: str
    search_plan_id: str
    search_plan_hash: str
    search_run_id: str
    candidate_decision_id: str
    candidate_recommendation: CandidateGenerationRecommendation
    eligible_candidate_id: str | None
    research_trial_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    mlflow_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ForwardInferenceResult:
    champion_batch_id: str | None
    challenger_batch_id: str | None
    champion_prediction_count: int
    challenger_prediction_count: int
    excluded_count: int


class BatchLearningEvidenceSource(Protocol):
    adapter_id: str
    adapter_version: str

    def snapshot(
        self,
        *,
        source_scope: Mapping[str, object],
        prior_watermark: str | None,
        cutoff_at: datetime,
        maximum_observations: int,
    ) -> EvidenceSnapshot: ...


class BatchLearningCycleExecutor(Protocol):
    def execute(
        self,
        *,
        plan: BatchLearningPlan,
        cycle: BatchLearningCycle,
        window: BatchLearningEvidenceWindow,
    ) -> CycleExecutionResult: ...

    def reconcile(self, cycle: BatchLearningCycle) -> tuple[str, ...]: ...


class BatchForwardInferenceExecutor(Protocol):
    def execute(
        self,
        *,
        window: ForwardShadowEvaluationWindow,
        cutoff_at: datetime,
    ) -> ForwardInferenceResult: ...


class BatchForwardOutcomeExecutor(Protocol):
    def resolve(
        self,
        *,
        deployment_id: str,
        inference_batch_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> int: ...

    def evaluate(
        self,
        *,
        deployment_id: str,
        inference_batch_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> StrategyLabChallengerEvaluation: ...

    def matched_counts(
        self,
        *,
        champion_deployment_id: str | None,
        challenger_deployment_id: str,
        inference_batch_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> tuple[int, int, int, str, tuple[str, ...]]: ...


class BatchLearningRepository(Protocol):
    def plans(self) -> tuple[BatchLearningPlan, ...]: ...
    def plan(self, plan_id: str) -> BatchLearningPlan | None: ...
    def plan_by_idempotency(self, key: str) -> BatchLearningPlan | None: ...
    def save_plan(self, value: BatchLearningPlan) -> BatchLearningPlan: ...
    def update_plan(self, value: BatchLearningPlan) -> BatchLearningPlan: ...
    def save_evidence_window(
        self, value: BatchLearningEvidenceWindow
    ) -> BatchLearningEvidenceWindow: ...
    def evidence_window(self, window_id: str) -> BatchLearningEvidenceWindow | None: ...
    def evidence_windows(self, plan_id: str) -> tuple[BatchLearningEvidenceWindow, ...]: ...
    def cycle_by_idempotency(self, plan_id: str, key: str) -> BatchLearningCycle | None: ...
    def save_cycle(self, value: BatchLearningCycle) -> BatchLearningCycle: ...
    def update_cycle(self, value: BatchLearningCycle) -> BatchLearningCycle: ...
    def cycle(self, cycle_id: str) -> BatchLearningCycle | None: ...
    def cycles(self, plan_id: str) -> tuple[BatchLearningCycle, ...]: ...
    def save_binding(self, value: BatchLearningCycleBinding) -> BatchLearningCycleBinding: ...
    def binding(self, cycle_id: str) -> BatchLearningCycleBinding | None: ...
    def save_forward_window(
        self, value: ForwardShadowEvaluationWindow
    ) -> ForwardShadowEvaluationWindow: ...
    def update_forward_window(
        self, value: ForwardShadowEvaluationWindow
    ) -> ForwardShadowEvaluationWindow: ...
    def forward_window(self, window_id: str) -> ForwardShadowEvaluationWindow | None: ...
    def save_decision(self, value: BatchLearningDecision) -> BatchLearningDecision: ...
    def decisions(self, cycle_id: str) -> tuple[BatchLearningDecision, ...]: ...


class InMemoryBatchLearningRepository:
    def __init__(self) -> None:
        self._plans: dict[str, BatchLearningPlan] = {}
        self._windows: dict[str, BatchLearningEvidenceWindow] = {}
        self._cycles: dict[str, BatchLearningCycle] = {}
        self._bindings: dict[str, BatchLearningCycleBinding] = {}
        self._forward: dict[str, ForwardShadowEvaluationWindow] = {}
        self._decisions: dict[str, BatchLearningDecision] = {}

    def plans(self) -> tuple[BatchLearningPlan, ...]:
        return tuple(sorted(self._plans.values(), key=lambda item: (item.created_at, item.plan_id)))

    def plan(self, plan_id: str) -> BatchLearningPlan | None:
        return self._plans.get(plan_id)

    def plan_by_idempotency(self, key: str) -> BatchLearningPlan | None:
        return next((item for item in self._plans.values() if item.idempotency_key == key), None)

    def save_plan(self, value: BatchLearningPlan) -> BatchLearningPlan:
        prior = self.plan_by_idempotency(value.idempotency_key)
        if prior is not None and prior.canonical_input_hash != value.canonical_input_hash:
            raise BatchLearningConflict("BATCH_LEARNING_PLAN_IDEMPOTENCY_CONFLICT")
        self._plans[value.plan_id] = prior or value
        return prior or value

    def update_plan(self, value: BatchLearningPlan) -> BatchLearningPlan:
        if value.plan_id not in self._plans:
            raise BatchLearningConflict("BATCH_LEARNING_PLAN_NOT_FOUND")
        self._plans[value.plan_id] = value
        return value

    def save_evidence_window(
        self, value: BatchLearningEvidenceWindow
    ) -> BatchLearningEvidenceWindow:
        prior = self._windows.get(value.window_id)
        if prior is not None and prior.canonical_input_hash != value.canonical_input_hash:
            raise BatchLearningConflict("BATCH_EVIDENCE_WINDOW_CONFLICT")
        self._windows[value.window_id] = prior or value
        return prior or value

    def evidence_window(self, window_id: str) -> BatchLearningEvidenceWindow | None:
        return self._windows.get(window_id)

    def evidence_windows(self, plan_id: str) -> tuple[BatchLearningEvidenceWindow, ...]:
        return tuple(
            sorted(
                (item for item in self._windows.values() if item.plan_id == plan_id),
                key=lambda item: (item.cutoff_at, item.window_id),
            )
        )

    def cycle_by_idempotency(self, plan_id: str, key: str) -> BatchLearningCycle | None:
        return next(
            (
                item
                for item in self._cycles.values()
                if item.plan_id == plan_id and item.idempotency_key == key
            ),
            None,
        )

    def save_cycle(self, value: BatchLearningCycle) -> BatchLearningCycle:
        prior = self.cycle_by_idempotency(value.plan_id, value.idempotency_key)
        if prior is not None and prior.canonical_input_hash != value.canonical_input_hash:
            raise BatchLearningConflict("BATCH_LEARNING_CYCLE_IDEMPOTENCY_CONFLICT")
        self._cycles[value.cycle_id] = prior or value
        return prior or value

    def update_cycle(self, value: BatchLearningCycle) -> BatchLearningCycle:
        if value.cycle_id not in self._cycles:
            raise BatchLearningConflict("BATCH_LEARNING_CYCLE_NOT_FOUND")
        self._cycles[value.cycle_id] = value
        return value

    def cycle(self, cycle_id: str) -> BatchLearningCycle | None:
        return self._cycles.get(cycle_id)

    def cycles(self, plan_id: str) -> tuple[BatchLearningCycle, ...]:
        return tuple(
            sorted(
                (item for item in self._cycles.values() if item.plan_id == plan_id),
                key=lambda item: item.ordinal,
            )
        )

    def save_binding(self, value: BatchLearningCycleBinding) -> BatchLearningCycleBinding:
        prior = self._bindings.get(value.cycle_id)
        if prior is not None and (
            prior.binding_id != value.binding_id
            or prior.evidence_window_id != value.evidence_window_id
            or prior.dataset_id != value.dataset_id
            or prior.experiment_id != value.experiment_id
            or prior.research_trial_ids != value.research_trial_ids
            or prior.search_plan_id != value.search_plan_id
            or prior.search_run_id != value.search_run_id
            or prior.candidate_ids != value.candidate_ids
            or prior.candidate_decision_id != value.candidate_decision_id
        ):
            raise BatchLearningConflict("BATCH_LEARNING_BINDING_IMMUTABLE")
        self._bindings[value.cycle_id] = value
        return value

    def binding(self, cycle_id: str) -> BatchLearningCycleBinding | None:
        return self._bindings.get(cycle_id)

    def save_forward_window(
        self, value: ForwardShadowEvaluationWindow
    ) -> ForwardShadowEvaluationWindow:
        prior = self._forward.get(value.window_id)
        if prior is not None and prior.canonical_input_hash != value.canonical_input_hash:
            raise BatchLearningConflict("FORWARD_WINDOW_CONFLICT")
        self._forward[value.window_id] = prior or value
        return prior or value

    def update_forward_window(
        self, value: ForwardShadowEvaluationWindow
    ) -> ForwardShadowEvaluationWindow:
        if value.window_id not in self._forward:
            raise BatchLearningConflict("FORWARD_WINDOW_NOT_FOUND")
        self._forward[value.window_id] = value
        return value

    def forward_window(self, window_id: str) -> ForwardShadowEvaluationWindow | None:
        return self._forward.get(window_id)

    def save_decision(self, value: BatchLearningDecision) -> BatchLearningDecision:
        prior = self._decisions.get(value.decision_id)
        if prior is not None and prior.canonical_result_hash != value.canonical_result_hash:
            raise BatchLearningConflict("BATCH_LEARNING_DECISION_IMMUTABLE")
        self._decisions[value.decision_id] = prior or value
        return prior or value

    def decisions(self, cycle_id: str) -> tuple[BatchLearningDecision, ...]:
        return tuple(
            sorted(
                (item for item in self._decisions.values() if item.cycle_id == cycle_id),
                key=lambda item: (item.created_at, item.decision_id),
            )
        )


class CreateBatchLearningPlan:
    def __init__(self, repository: BatchLearningRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        name: str,
        vertical: str,
        research_family: str,
        source_adapter_id: str,
        source_adapter_version: str,
        source_scope: Mapping[str, object],
        feature_schema_id: str,
        feature_schema_hash: str,
        dataset_policy_id: str,
        dataset_policy: Mapping[str, object],
        experiment_template: Mapping[str, object],
        permitted_model_families: Sequence[ModelFamily],
        search_template: Mapping[str, object],
        objective_policy_id: str,
        objective_policy: Mapping[str, object],
        fold_policy_id: str,
        fold_policy: Mapping[str, object],
        minimum_new_resolved_observations: int,
        minimum_new_independent_groups: int,
        maximum_observations_per_cycle: int,
        maximum_trials_per_cycle: int,
        maximum_failed_trials_per_cycle: int,
        maximum_cycles: int,
        minimum_cooldown_seconds: int,
        drift_policy: Mapping[str, object],
        forward_policy: Mapping[str, object],
        champion_comparison_policy: Mapping[str, object],
        mlflow_mirror_policy_id: str,
        mlflow_mirror_policy: Mapping[str, object],
        operator_action_id: str,
        operator_reason: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> BatchLearningPlan:
        if (
            not name
            or not vertical
            or not research_family
            or not source_adapter_id
            or not source_adapter_version
            or not feature_schema_id
            or len(feature_schema_hash) != 64
            or not dataset_policy_id
            or not objective_policy_id
            or not fold_policy_id
            or not mlflow_mirror_policy_id
            or not operator_action_id
            or not operator_reason
            or not idempotency_key
            or created_at.tzinfo is None
        ):
            raise BatchLearningError("BATCH_LEARNING_PLAN_INPUT_INVALID")
        families = tuple(item.value for item in permitted_model_families)
        if not families or not set(families).issubset({item.value for item in ModelFamily}):
            raise BatchLearningError("BATCH_LEARNING_MODEL_FAMILY_INVALID")
        bounds = (
            1 <= minimum_new_resolved_observations <= maximum_observations_per_cycle <= 100_000
            and 1 <= minimum_new_independent_groups <= minimum_new_resolved_observations
            and 1 <= maximum_trials_per_cycle <= 500
            and 0 <= maximum_failed_trials_per_cycle < maximum_trials_per_cycle
            and 1 <= maximum_cycles <= 1_000
            and 0 <= minimum_cooldown_seconds <= 31_536_000
        )
        template_budget = search_template.get("maximum_trials")
        if not bounds or (
            template_budget is not None
            and (not isinstance(template_budget, int) or maximum_trials_per_cycle > template_budget)
        ):
            raise BatchLearningError("BATCH_LEARNING_PLAN_BUDGET_INVALID")
        payload = {
            "name": name,
            "vertical": vertical,
            "family": research_family,
            "source": (source_adapter_id, source_adapter_version),
            "scope": source_scope,
            "schema": (feature_schema_id, feature_schema_hash),
            "dataset_policy": (dataset_policy_id, dataset_policy),
            "experiment_template": experiment_template,
            "families": families,
            "search_template": search_template,
            "objective": (objective_policy_id, objective_policy),
            "fold": (fold_policy_id, fold_policy),
            "thresholds": (
                minimum_new_resolved_observations,
                minimum_new_independent_groups,
            ),
            "budgets": (
                maximum_observations_per_cycle,
                maximum_trials_per_cycle,
                maximum_failed_trials_per_cycle,
                maximum_cycles,
                minimum_cooldown_seconds,
            ),
            "drift": drift_policy,
            "forward": forward_policy,
            "comparison": champion_comparison_policy,
            "mirror": (mlflow_mirror_policy_id, mlflow_mirror_policy),
            "operator_action": operator_action_id,
            "operator_reason": operator_reason,
            "key": idempotency_key,
        }
        input_hash = content_digest(payload)
        prior = self._repository.plan_by_idempotency(idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != input_hash:
                raise BatchLearningConflict("BATCH_LEARNING_PLAN_IDEMPOTENCY_CONFLICT")
            return prior
        plan = BatchLearningPlan(
            deterministic_id("batch_learning_plan", payload),
            name,
            vertical,
            research_family,
            source_adapter_id,
            source_adapter_version,
            dict(source_scope),
            content_digest(source_scope),
            feature_schema_id,
            feature_schema_hash,
            dataset_policy_id,
            content_digest(dataset_policy),
            dict(experiment_template),
            content_digest(experiment_template),
            families,
            dict(search_template),
            content_digest(search_template),
            objective_policy_id,
            content_digest(objective_policy),
            fold_policy_id,
            content_digest(fold_policy),
            minimum_new_resolved_observations,
            minimum_new_independent_groups,
            maximum_observations_per_cycle,
            maximum_trials_per_cycle,
            maximum_failed_trials_per_cycle,
            maximum_cycles,
            minimum_cooldown_seconds,
            dict(drift_policy),
            content_digest(drift_policy),
            dict(forward_policy),
            content_digest(forward_policy),
            dict(champion_comparison_policy),
            content_digest(champion_comparison_policy),
            mlflow_mirror_policy_id,
            content_digest(mlflow_mirror_policy),
            1,
            BatchLearningPlanState.DRAFT,
            created_at,
            None,
            None,
            None,
            None,
            operator_action_id,
            idempotency_key,
            input_hash,
            content_digest({"input": input_hash, "state": "DRAFT"}),
        )
        return self._repository.save_plan(plan)


class BatchLearningLifecycle:
    def __init__(self, repository: BatchLearningRepository) -> None:
        self._repository = repository

    def activate(
        self,
        *,
        plan_id: str,
        confirm_plan_id: str,
        confirm_plan_hash: str,
        opened_at: datetime,
    ) -> BatchLearningPlan:
        plan = self._require_plan(plan_id)
        if (
            plan.state is not BatchLearningPlanState.DRAFT
            or confirm_plan_id != plan.plan_id
            or confirm_plan_hash != plan.canonical_input_hash
            or opened_at.tzinfo is None
        ):
            raise BatchLearningError("BATCH_LEARNING_ACTIVATION_CONFIRMATION_MISMATCH")
        return self._repository.update_plan(
            replace(
                plan,
                state=BatchLearningPlanState.ACTIVE,
                opened_at=opened_at,
                canonical_result_hash=content_digest(
                    {"input": plan.canonical_input_hash, "state": "ACTIVE"}
                ),
            )
        )

    def control(self, *, plan_id: str, action: str, reason: str, at: datetime) -> BatchLearningPlan:
        plan = self._require_plan(plan_id)
        allowed = {
            "pause": (BatchLearningPlanState.ACTIVE, BatchLearningPlanState.PAUSED),
            "resume": (BatchLearningPlanState.PAUSED, BatchLearningPlanState.ACTIVE),
            "revoke": (BatchLearningPlanState.ACTIVE, BatchLearningPlanState.REVOKED),
        }
        if action not in allowed or plan.state is not allowed[action][0] or not reason:
            raise BatchLearningError("BATCH_LEARNING_PLAN_TRANSITION_INVALID")
        target = allowed[action][1]
        return self._repository.update_plan(
            replace(
                plan,
                state=target,
                paused_at=at if target is BatchLearningPlanState.PAUSED else plan.paused_at,
                revoked_at=at if target is BatchLearningPlanState.REVOKED else plan.revoked_at,
                canonical_result_hash=content_digest(
                    {"input": plan.canonical_input_hash, "state": target.value, "reason": reason}
                ),
            )
        )

    def _require_plan(self, plan_id: str) -> BatchLearningPlan:
        plan = self._repository.plan(plan_id)
        if plan is None:
            raise BatchLearningError("BATCH_LEARNING_PLAN_NOT_FOUND")
        return plan


def _terminal_cycle(value: BatchLearningCycle) -> bool:
    return value.state in {
        BatchLearningCycleState.NO_ELIGIBLE_CANDIDATE,
        BatchLearningCycleState.ELIGIBLE_FOR_SHADOW_REVIEW,
        BatchLearningCycleState.WAITING_FOR_SHADOW_APPROVAL,
        BatchLearningCycleState.FORWARD_EVALUATING,
        BatchLearningCycleState.COMPLETED,
    }


class EvaluateBatchLearningTrigger:
    def __init__(self, repository: BatchLearningRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        plan_id: str,
        source: BatchLearningEvidenceSource,
        cutoff_at: datetime,
        trigger_type: BatchLearningTriggerType,
        trigger_evidence: Mapping[str, object],
        idempotency_key: str,
    ) -> tuple[BatchLearningEvidenceWindow, BatchLearningCycle, BatchLearningDecision]:
        plan = self._repository.plan(plan_id)
        if plan is None:
            raise BatchLearningError("BATCH_LEARNING_PLAN_NOT_FOUND")
        if plan.state is not BatchLearningPlanState.ACTIVE:
            raise BatchLearningError("BATCH_LEARNING_PLAN_NOT_ACTIVE")
        if (
            source.adapter_id != plan.source_adapter_id
            or source.adapter_version != plan.source_adapter_version
        ):
            raise BatchLearningError("BATCH_LEARNING_SOURCE_ADAPTER_MISMATCH")
        prior_cycle = self._repository.cycle_by_idempotency(plan_id, idempotency_key)
        if prior_cycle is not None:
            prior_window = self._repository.evidence_window(prior_cycle.evidence_window_id)
            if prior_window is None:
                raise BatchLearningError("BATCH_LEARNING_EVIDENCE_WINDOW_NOT_FOUND")
            replay_snapshot = source.snapshot(
                source_scope=plan.source_scope,
                prior_watermark=prior_window.prior_watermark,
                cutoff_at=cutoff_at,
                maximum_observations=plan.maximum_observations_per_cycle,
            )
            replay_payload = {
                "plan": plan_id,
                "prior": prior_window.prior_watermark,
                "watermark": replay_snapshot.new_watermark,
                "cutoff": cutoff_at,
                "source": replay_snapshot.source_artifact_hash,
                "observations": replay_snapshot.observation_ids,
                "groups": replay_snapshot.group_ids,
            }
            if (
                prior_window.canonical_input_hash != content_digest(replay_payload)
                or prior_cycle.trigger_type is not trigger_type
                or prior_cycle.trigger_evidence_hash != content_digest(trigger_evidence)
            ):
                raise BatchLearningConflict("BATCH_LEARNING_CYCLE_IDEMPOTENCY_CONFLICT")
            trigger_decision = next(
                (
                    item
                    for item in self._repository.decisions(prior_cycle.cycle_id)
                    if item.decision_stage is BatchLearningDecisionStage.CYCLE_TRIGGER
                ),
                None,
            )
            if trigger_decision is None:
                raise BatchLearningError("BATCH_LEARNING_TRIGGER_DECISION_MISSING")
            return prior_window, prior_cycle, trigger_decision
        cycles = self._repository.cycles(plan_id)
        actual = tuple(item for item in cycles if _terminal_cycle(item))
        if len(actual) >= plan.maximum_cycles:
            raise BatchLearningError("BATCH_LEARNING_CYCLE_BUDGET_EXHAUSTED")
        active = tuple(
            item
            for item in cycles
            if item.state
            in {
                BatchLearningCycleState.BUILDING_DATASET,
                BatchLearningCycleState.SEARCHING,
            }
            and item.lease_expires_at is not None
            and item.lease_expires_at > cutoff_at
        )
        if active:
            raise BatchLearningError("BATCH_LEARNING_CYCLE_ALREADY_RUNNING")
        successful_windows = tuple(
            self._repository.evidence_window(item.evidence_window_id) for item in actual
        )
        retained_windows = tuple(item for item in successful_windows if item is not None)
        prior_watermark = retained_windows[-1].new_watermark if retained_windows else None
        if actual and actual[-1].completed_at is not None:
            next_at = actual[-1].completed_at + timedelta(seconds=plan.minimum_cooldown_seconds)
            if cutoff_at < next_at:
                raise BatchLearningError("BATCH_LEARNING_COOLDOWN_ACTIVE")
        snapshot = source.snapshot(
            source_scope=plan.source_scope,
            prior_watermark=prior_watermark,
            cutoff_at=cutoff_at,
            maximum_observations=plan.maximum_observations_per_cycle,
        )
        payload = {
            "plan": plan_id,
            "prior": prior_watermark,
            "watermark": snapshot.new_watermark,
            "cutoff": cutoff_at,
            "source": snapshot.source_artifact_hash,
            "observations": snapshot.observation_ids,
            "groups": snapshot.group_ids,
        }
        valid_counts = (
            len(snapshot.observation_ids) >= plan.minimum_new_resolved_observations
            and len(set(snapshot.group_ids)) >= plan.minimum_new_independent_groups
        )
        state = (
            EvidenceWindowState.REJECTED_INTEGRITY
            if not snapshot.integrity_valid
            else EvidenceWindowState.READY
            if valid_counts
            else EvidenceWindowState.INSUFFICIENT_EVIDENCE
        )
        reasons = (
            ("SOURCE_LINEAGE_OR_INTEGRITY_INVALID",)
            if state is EvidenceWindowState.REJECTED_INTEGRITY
            else ("MINIMUM_NEW_EVIDENCE_NOT_MET",)
            if state is EvidenceWindowState.INSUFFICIENT_EVIDENCE
            else ()
        )
        window = self._repository.save_evidence_window(
            BatchLearningEvidenceWindow(
                deterministic_id("batch_learning_evidence_window", payload),
                plan_id,
                prior_watermark,
                snapshot.new_watermark,
                cutoff_at,
                snapshot.source_artifact_id,
                snapshot.source_artifact_hash,
                len(snapshot.observation_ids),
                len(set(snapshot.group_ids)),
                snapshot.excluded_unresolved_count,
                snapshot.excluded_duplicate_count,
                snapshot.excluded_unavailable_count,
                snapshot.excluded_integrity_count,
                snapshot.earliest_evidence_at,
                snapshot.latest_evidence_at,
                content_digest(tuple(sorted(set(snapshot.group_ids)))),
                content_digest(tuple(sorted(snapshot.observation_ids))),
                state,
                reasons,
                content_digest(payload),
                content_digest({"state": state.value, "reasons": reasons}),
            )
        )
        cycle_payload = {
            "plan": plan_id,
            "ordinal": len(cycles) + 1,
            "window": window.window_id,
            "window_hash": window.canonical_result_hash,
            "trigger": trigger_type.value,
            "trigger_evidence": trigger_evidence,
            "key": idempotency_key,
        }
        cycle_state = (
            BatchLearningCycleState.REQUESTED
            if state is EvidenceWindowState.READY
            else BatchLearningCycleState.BLOCKED_INTEGRITY
            if state is EvidenceWindowState.REJECTED_INTEGRITY
            else BatchLearningCycleState.WAITING_FOR_EVIDENCE
        )
        cycle = self._repository.save_cycle(
            BatchLearningCycle(
                deterministic_id("batch_learning_cycle", cycle_payload),
                plan_id,
                len(cycles) + 1,
                trigger_type,
                dict(trigger_evidence),
                content_digest(trigger_evidence),
                window.window_id,
                window.canonical_result_hash,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                cycle_state,
                None,
                None,
                None,
                None,
                cutoff_at
                if cycle_state
                in {
                    BatchLearningCycleState.WAITING_FOR_EVIDENCE,
                    BatchLearningCycleState.BLOCKED_INTEGRITY,
                }
                else None,
                None,
                None,
                reasons,
                idempotency_key,
                content_digest(cycle_payload),
                content_digest({"state": cycle_state.value, "reasons": reasons}),
            )
        )
        recommendation = (
            BatchLearningRecommendation.NO_SEARCH_REQUIRED
            if cycle_state is BatchLearningCycleState.REQUESTED
            else BatchLearningRecommendation.BLOCKED_INTEGRITY
            if cycle_state is BatchLearningCycleState.BLOCKED_INTEGRITY
            else BatchLearningRecommendation.WAIT_FOR_MORE_EVIDENCE
        )
        decision = _decision(
            cycle=cycle,
            stage=BatchLearningDecisionStage.CYCLE_TRIGGER,
            evidence_ids=(window.window_id,),
            recommendation=recommendation,
            reasons=reasons,
            created_at=cutoff_at,
        )
        return window, cycle, self._repository.save_decision(decision)


class RunBatchLearningCycle:
    def __init__(
        self,
        repository: BatchLearningRepository,
        executor: BatchLearningCycleExecutor,
    ) -> None:
        self._repository = repository
        self._executor = executor

    def execute(
        self,
        *,
        cycle_id: str,
        lease_owner: str,
        lease_token: str,
        lease_seconds: int,
        now: datetime,
    ) -> tuple[BatchLearningCycle, BatchLearningCycleBinding, BatchLearningDecision]:
        cycle = self._require_cycle(cycle_id)
        plan = self._repository.plan(cycle.plan_id)
        window = self._repository.evidence_window(cycle.evidence_window_id)
        if (
            plan is None
            or plan.state is not BatchLearningPlanState.ACTIVE
            or window is None
            or window.state is not EvidenceWindowState.READY
            or cycle.state is not BatchLearningCycleState.REQUESTED
            or not lease_owner
            or not lease_token
            or not 1 <= lease_seconds <= 3_600
        ):
            raise BatchLearningError("BATCH_LEARNING_CYCLE_NOT_RUNNABLE")
        cycle = self._repository.update_cycle(
            replace(
                cycle,
                state=BatchLearningCycleState.BUILDING_DATASET,
                lease_owner=lease_owner,
                lease_token=lease_token,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                started_at=now,
                canonical_result_hash=content_digest({"state": "BUILDING_DATASET"}),
            )
        )
        cycle = self._repository.update_cycle(
            replace(
                cycle,
                state=BatchLearningCycleState.SEARCHING,
                canonical_result_hash=content_digest({"state": "SEARCHING"}),
            )
        )
        try:
            result = self._executor.execute(plan=plan, cycle=cycle, window=window)
        except BatchLearningError as error:
            self._repository.update_cycle(
                replace(
                    cycle,
                    state=BatchLearningCycleState.BLOCKED_INTEGRITY,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    completed_at=now,
                    failure_code=str(error),
                    reason_codes=("CYCLE_EXECUTION_INTEGRITY_FAILURE",),
                    canonical_result_hash=content_digest(
                        {"state": "BLOCKED_INTEGRITY", "failure": str(error)}
                    ),
                )
            )
            raise
        eligible = (
            result.candidate_recommendation
            is CandidateGenerationRecommendation.ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW
            and result.eligible_candidate_id is not None
        )
        target = (
            BatchLearningCycleState.ELIGIBLE_FOR_SHADOW_REVIEW
            if eligible
            else BatchLearningCycleState.NO_ELIGIBLE_CANDIDATE
        )
        completed = replace(
            cycle,
            dataset_id=result.dataset_id,
            dataset_hash=result.dataset_hash,
            experiment_id=result.experiment_id,
            experiment_hash=result.experiment_hash,
            search_plan_id=result.search_plan_id,
            search_plan_hash=result.search_plan_hash,
            search_run_id=result.search_run_id,
            candidate_decision_id=result.candidate_decision_id,
            eligible_candidate_id=result.eligible_candidate_id if eligible else None,
            state=target,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            completed_at=now,
            reason_codes=(result.candidate_recommendation.value,),
            canonical_result_hash=content_digest(
                {
                    "state": target.value,
                    "dataset": result.dataset_hash,
                    "experiment": result.experiment_hash,
                    "search": result.search_run_id,
                    "decision": result.candidate_decision_id,
                }
            ),
        )
        completed = self._repository.update_cycle(completed)
        binding_payload = {
            "cycle": cycle_id,
            "window": window.window_id,
            "dataset": result.dataset_id,
            "experiment": result.experiment_id,
            "trials": result.research_trial_ids,
            "search_plan": result.search_plan_id,
            "search_run": result.search_run_id,
            "candidates": result.candidate_ids,
            "decision": result.candidate_decision_id,
            "mlflow": result.mlflow_record_ids,
        }
        binding = self._repository.save_binding(
            BatchLearningCycleBinding(
                deterministic_id("batch_learning_cycle_binding", binding_payload),
                cycle_id,
                window.window_id,
                result.dataset_id,
                result.experiment_id,
                result.research_trial_ids,
                result.search_plan_id,
                result.search_run_id,
                result.candidate_ids,
                result.candidate_decision_id,
                None,
                None,
                (),
                (),
                None,
                result.mlflow_record_ids,
                content_digest(binding_payload),
            )
        )
        recommendation = (
            BatchLearningRecommendation.ELIGIBLE_FOR_SHADOW_REVIEW
            if eligible
            else BatchLearningRecommendation.NO_ELIGIBLE_CANDIDATE
        )
        decision = self._repository.save_decision(
            _decision(
                cycle=completed,
                stage=BatchLearningDecisionStage.CANDIDATE_GENERATION,
                evidence_ids=(window.window_id, result.candidate_decision_id),
                recommendation=recommendation,
                reasons=completed.reason_codes,
                created_at=now,
                candidate_generation_decision_id=result.candidate_decision_id,
            )
        )
        return completed, binding, decision

    def _require_cycle(self, cycle_id: str) -> BatchLearningCycle:
        cycle = self._repository.cycle(cycle_id)
        if cycle is None:
            raise BatchLearningError("BATCH_LEARNING_CYCLE_NOT_FOUND")
        return cycle


class AttachApprovedBatchLearningChallenger:
    def __init__(self, repository: BatchLearningRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        cycle_id: str,
        deployment: StrategyLabModelDeployment,
        champion: StrategyLabModelDeployment | None,
        operator_action_id: str,
        idempotency_key: str,
        opened_at: datetime,
    ) -> tuple[BatchLearningCycle, BatchLearningCycleBinding, ForwardShadowEvaluationWindow]:
        cycle = self._repository.cycle(cycle_id)
        binding = self._repository.binding(cycle_id)
        plan = self._repository.plan(cycle.plan_id) if cycle is not None else None
        if cycle is not None and cycle.approved_challenger_deployment_id is not None:
            window = (
                self._repository.forward_window(cycle.forward_window_id)
                if cycle.forward_window_id is not None
                else None
            )
            if (
                cycle.approved_challenger_deployment_id == deployment.deployment_id
                and window is not None
                and binding is not None
            ):
                return cycle, binding, window
            raise BatchLearningConflict("BATCH_CHALLENGER_ATTACHMENT_CONFLICT")
        if (
            cycle is None
            or binding is None
            or plan is None
            or cycle.state is not BatchLearningCycleState.ELIGIBLE_FOR_SHADOW_REVIEW
            or cycle.eligible_candidate_id != deployment.candidate_id
            or deployment.state is not DeploymentState.ACTIVE
            or not deployment.review_id
            or not operator_action_id
            or not idempotency_key
        ):
            raise BatchLearningError("APPROVED_BATCH_CHALLENGER_REQUIRED")
        policy = plan.forward_policy
        minimum_predictions = _policy_integer(policy, "minimum_predictions", 100)
        minimum_resolved = _policy_integer(policy, "minimum_resolved_outcomes", 100)
        maximum_duration = _policy_integer(policy, "maximum_duration_seconds", 2_592_000)
        payload = {
            "cycle": cycle_id,
            "challenger": deployment.deployment_id,
            "challenger_hash": deployment.canonical_hash,
            "champion": champion.deployment_id if champion else None,
            "champion_hash": champion.canonical_hash if champion else None,
            "scope": plan.source_scope_hash,
            "key": idempotency_key,
        }
        window_id = deterministic_id("forward_shadow_evaluation_window", payload)
        window = self._repository.save_forward_window(
            ForwardShadowEvaluationWindow(
                window_id,
                cycle_id,
                champion.deployment_id if champion else None,
                champion.canonical_hash if champion else None,
                deployment.deployment_id,
                deployment.canonical_hash,
                dict(plan.source_scope),
                plan.source_scope_hash,
                opened_at,
                opened_at,
                opened_at + timedelta(seconds=maximum_duration),
                minimum_predictions,
                minimum_resolved,
                maximum_duration,
                0,
                0,
                0,
                0,
                0,
                0,
                None,
                ForwardWindowState.OPEN,
                (),
                (),
                None,
                content_digest(payload),
                None,
                opened_at,
                None,
            )
        )
        updated_binding = self._repository.save_binding(
            replace(
                binding,
                shadow_review_id=deployment.review_id,
                approved_deployment_id=deployment.deployment_id,
                canonical_binding_hash=content_digest(
                    {
                        "prior": binding.canonical_binding_hash,
                        "review": deployment.review_id,
                        "deployment": deployment.deployment_id,
                        "operator": operator_action_id,
                    }
                ),
            )
        )
        updated_cycle = self._repository.update_cycle(
            replace(
                cycle,
                approved_challenger_deployment_id=deployment.deployment_id,
                forward_window_id=window_id,
                state=BatchLearningCycleState.FORWARD_EVALUATING,
                completed_at=None,
                canonical_result_hash=content_digest(
                    {"state": "FORWARD_EVALUATING", "window": window_id}
                ),
            )
        )
        return updated_cycle, updated_binding, window


class RunBatchForwardInference:
    def __init__(
        self,
        repository: BatchLearningRepository,
        executor: BatchForwardInferenceExecutor,
    ) -> None:
        self._repository = repository
        self._executor = executor

    def execute(self, *, window_id: str, cutoff_at: datetime) -> ForwardShadowEvaluationWindow:
        window = self._require_window(window_id)
        cycle = self._repository.cycle(window.cycle_id)
        plan = self._repository.plan(cycle.plan_id) if cycle is not None else None
        if (
            cycle is None
            or plan is None
            or plan.state is BatchLearningPlanState.REVOKED
            or cycle.state is not BatchLearningCycleState.FORWARD_EVALUATING
            or window.state
            not in {
                ForwardWindowState.OPEN,
                ForwardWindowState.COLLECTING,
                ForwardWindowState.INSUFFICIENT_EVIDENCE,
            }
        ):
            raise BatchLearningError("BATCH_FORWARD_INFERENCE_NOT_ALLOWED")
        result = self._executor.execute(window=window, cutoff_at=cutoff_at)
        batches = tuple(
            item
            for item in (result.champion_batch_id, result.challenger_batch_id)
            if item is not None
        )
        binding = self._repository.binding(cycle.cycle_id)
        retained_batches = set(binding.inference_batch_ids) if binding is not None else set()
        champion_is_new = (
            result.champion_batch_id is not None
            and result.champion_batch_id not in retained_batches
        )
        challenger_is_new = (
            result.challenger_batch_id is not None
            and result.challenger_batch_id not in retained_batches
        )
        if not champion_is_new and not challenger_is_new:
            return window
        if binding is not None:
            self._repository.save_binding(
                replace(
                    binding,
                    inference_batch_ids=tuple(
                        dict.fromkeys((*binding.inference_batch_ids, *batches))
                    ),
                    canonical_binding_hash=content_digest(
                        {"prior": binding.canonical_binding_hash, "batches": batches}
                    ),
                )
            )
        return self._repository.update_forward_window(
            replace(
                window,
                cutoff_at=cutoff_at,
                state=ForwardWindowState.COLLECTING,
                champion_prediction_count=(
                    window.champion_prediction_count
                    + (result.champion_prediction_count if champion_is_new else 0)
                ),
                challenger_prediction_count=(
                    window.challenger_prediction_count
                    + (result.challenger_prediction_count if challenger_is_new else 0)
                ),
                excluded_count=window.excluded_count
                + (result.excluded_count if champion_is_new or challenger_is_new else 0),
                canonical_result_hash=content_digest(
                    {
                        "state": "COLLECTING",
                        "champion": (
                            window.champion_prediction_count
                            + (result.champion_prediction_count if champion_is_new else 0)
                        ),
                        "challenger": (
                            window.challenger_prediction_count
                            + (result.challenger_prediction_count if challenger_is_new else 0)
                        ),
                    }
                ),
            )
        )

    def _require_window(self, window_id: str) -> ForwardShadowEvaluationWindow:
        window = self._repository.forward_window(window_id)
        if window is None:
            raise BatchLearningError("BATCH_FORWARD_WINDOW_NOT_FOUND")
        return window


class ResolveBatchForwardOutcomes:
    def __init__(
        self,
        repository: BatchLearningRepository,
        executor: BatchForwardOutcomeExecutor,
    ) -> None:
        self._repository = repository
        self._executor = executor

    def execute(self, *, window_id: str, cutoff_at: datetime) -> tuple[int, int]:
        window = self._repository.forward_window(window_id)
        if window is None or window.state is ForwardWindowState.CANCELED:
            raise BatchLearningError("BATCH_FORWARD_WINDOW_NOT_RESOLVABLE")
        if window.state is ForwardWindowState.COMPLETED:
            return window.champion_resolved_count, window.challenger_resolved_count
        binding = self._repository.binding(window.cycle_id)
        if binding is None:
            raise BatchLearningError("BATCH_FORWARD_BINDING_NOT_FOUND")
        champion = (
            self._executor.resolve(
                deployment_id=window.champion_deployment_id,
                inference_batch_ids=binding.inference_batch_ids,
                cutoff_at=cutoff_at,
            )
            if window.champion_deployment_id is not None
            else 0
        )
        challenger = self._executor.resolve(
            deployment_id=window.challenger_deployment_id,
            inference_batch_ids=binding.inference_batch_ids,
            cutoff_at=cutoff_at,
        )
        return champion, challenger


class EvaluateBatchForwardWindow:
    def __init__(
        self,
        repository: BatchLearningRepository,
        executor: BatchForwardOutcomeExecutor,
    ) -> None:
        self._repository = repository
        self._executor = executor

    def execute(
        self, *, window_id: str, cutoff_at: datetime
    ) -> tuple[ForwardShadowEvaluationWindow, BatchLearningCycle, BatchLearningDecision]:
        window = self._repository.forward_window(window_id)
        if window is None or window.state is ForwardWindowState.CANCELED:
            raise BatchLearningError("BATCH_FORWARD_WINDOW_NOT_EVALUABLE")
        cycle = self._repository.cycle(window.cycle_id)
        if cycle is None:
            raise BatchLearningError("BATCH_LEARNING_CYCLE_NOT_FOUND")
        if window.state is ForwardWindowState.COMPLETED:
            retained = next(
                (
                    item
                    for item in self._repository.decisions(cycle.cycle_id)
                    if item.decision_stage is BatchLearningDecisionStage.FORWARD_EVALUATION
                    and item.challenger_evaluation_id == window.challenger_evaluation_id
                ),
                None,
            )
            if retained is None:
                raise BatchLearningError("BATCH_FORWARD_TERMINAL_DECISION_MISSING")
            return window, cycle, retained
        binding = self._repository.binding(cycle.cycle_id)
        if binding is None:
            raise BatchLearningError("BATCH_FORWARD_BINDING_NOT_FOUND")
        champion_count, challenger_count, unmatched, outcome_hash, evaluation_ids = (
            self._executor.matched_counts(
                champion_deployment_id=window.champion_deployment_id,
                challenger_deployment_id=window.challenger_deployment_id,
                inference_batch_ids=binding.inference_batch_ids,
                cutoff_at=cutoff_at,
            )
        )
        if challenger_count < window.minimum_resolved_outcomes or (
            window.champion_deployment_id is not None
            and champion_count < window.minimum_resolved_outcomes
        ):
            updated = self._repository.update_forward_window(
                replace(
                    window,
                    cutoff_at=cutoff_at,
                    champion_resolved_count=champion_count,
                    challenger_resolved_count=challenger_count,
                    unmatched_count=unmatched,
                    outcome_set_hash=outcome_hash,
                    state=ForwardWindowState.INSUFFICIENT_EVIDENCE,
                    evaluation_ids=evaluation_ids,
                    canonical_result_hash=content_digest(
                        {"state": "INSUFFICIENT_EVIDENCE", "outcomes": outcome_hash}
                    ),
                )
            )
            decision = self._repository.save_decision(
                _decision(
                    cycle=cycle,
                    stage=BatchLearningDecisionStage.FORWARD_EVALUATION,
                    evidence_ids=evaluation_ids,
                    recommendation=BatchLearningRecommendation.CONTINUE_FORWARD_EVALUATION,
                    reasons=("MINIMUM_MATCHED_FORWARD_OUTCOMES_NOT_MET",),
                    created_at=cutoff_at,
                    challenger_deployment_id=window.challenger_deployment_id,
                    champion_deployment_id=window.champion_deployment_id,
                )
            )
            return updated, cycle, decision
        challenger = self._executor.evaluate(
            deployment_id=window.challenger_deployment_id,
            inference_batch_ids=binding.inference_batch_ids,
            cutoff_at=cutoff_at,
        )
        recommendation = _forward_recommendation(challenger)
        drift_state = str(challenger.drift_metrics.get("state", "UNKNOWN"))
        updated_window = self._repository.update_forward_window(
            replace(
                window,
                cutoff_at=cutoff_at,
                champion_resolved_count=champion_count,
                challenger_resolved_count=challenger_count,
                unmatched_count=unmatched,
                outcome_set_hash=outcome_hash,
                state=ForwardWindowState.COMPLETED,
                evaluation_ids=evaluation_ids,
                drift_diagnostic_ids=(challenger.challenger_evaluation_id,),
                challenger_evaluation_id=challenger.challenger_evaluation_id,
                canonical_result_hash=content_digest(
                    {
                        "state": "COMPLETED",
                        "outcomes": outcome_hash,
                        "challenger": challenger.canonical_result_hash,
                    }
                ),
                completed_at=cutoff_at,
            )
        )
        updated_cycle = self._repository.update_cycle(
            replace(
                cycle,
                state=BatchLearningCycleState.COMPLETED,
                completed_at=cutoff_at,
                canonical_result_hash=content_digest(
                    {
                        "state": "COMPLETED",
                        "forward_window": window_id,
                        "challenger_evaluation": challenger.challenger_evaluation_id,
                    }
                ),
            )
        )
        self._repository.save_binding(
            replace(
                binding,
                forward_evaluation_ids=evaluation_ids,
                challenger_evaluation_id=challenger.challenger_evaluation_id,
                canonical_binding_hash=content_digest(
                    {
                        "prior": binding.canonical_binding_hash,
                        "evaluations": evaluation_ids,
                        "challenger": challenger.challenger_evaluation_id,
                    }
                ),
            )
        )
        decision = self._repository.save_decision(
            _decision(
                cycle=updated_cycle,
                stage=BatchLearningDecisionStage.FORWARD_EVALUATION,
                evidence_ids=evaluation_ids,
                recommendation=recommendation,
                reasons=challenger.reason_codes,
                created_at=cutoff_at,
                challenger_evaluation_id=challenger.challenger_evaluation_id,
                challenger_deployment_id=window.challenger_deployment_id,
                champion_deployment_id=window.champion_deployment_id,
                drift_state=drift_state,
            )
        )
        return updated_window, updated_cycle, decision


class RecoverBatchLearningCycle:
    def __init__(
        self,
        repository: BatchLearningRepository,
        executor: BatchLearningCycleExecutor,
    ) -> None:
        self._repository = repository
        self._executor = executor

    def execute(self, *, cycle_id: str, now: datetime) -> BatchLearningCycle:
        cycle = self._repository.cycle(cycle_id)
        if cycle is None:
            raise BatchLearningError("BATCH_LEARNING_CYCLE_NOT_FOUND")
        if cycle.state not in {
            BatchLearningCycleState.BUILDING_DATASET,
            BatchLearningCycleState.SEARCHING,
        }:
            return cycle
        if cycle.lease_expires_at is not None and cycle.lease_expires_at > now:
            raise BatchLearningError("BATCH_LEARNING_LEASE_STILL_ACTIVE")
        reasons = self._executor.reconcile(cycle)
        if "IRRECONCILABLE" in reasons:
            return self._repository.update_cycle(
                replace(
                    cycle,
                    state=BatchLearningCycleState.BLOCKED_INTEGRITY,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    completed_at=now,
                    failure_code="BATCH_LEARNING_RECOVERY_IRRECONCILABLE",
                    reason_codes=reasons,
                    canonical_result_hash=content_digest(
                        {"state": "BLOCKED_INTEGRITY", "reasons": reasons}
                    ),
                )
            )
        return self._repository.update_cycle(
            replace(
                cycle,
                state=BatchLearningCycleState.REQUESTED,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                reason_codes=tuple(dict.fromkeys((*cycle.reason_codes, *reasons))),
                canonical_result_hash=content_digest({"state": "REQUESTED", "reconciled": reasons}),
            )
        )


class CancelBatchLearningCycle:
    def __init__(self, repository: BatchLearningRepository) -> None:
        self._repository = repository

    def execute(self, *, cycle_id: str, reason: str, canceled_at: datetime) -> BatchLearningCycle:
        cycle = self._repository.cycle(cycle_id)
        if cycle is None or cycle.state in {
            BatchLearningCycleState.COMPLETED,
            BatchLearningCycleState.CANCELED,
        }:
            raise BatchLearningError("BATCH_LEARNING_CYCLE_NOT_CANCELABLE")
        return self._repository.update_cycle(
            replace(
                cycle,
                state=BatchLearningCycleState.CANCELED,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                canceled_at=canceled_at,
                completed_at=canceled_at,
                reason_codes=tuple(dict.fromkeys((*cycle.reason_codes, reason))),
                canonical_result_hash=content_digest({"state": "CANCELED", "reason": reason}),
            )
        )


def _forward_recommendation(
    evaluation: StrategyLabChallengerEvaluation,
) -> BatchLearningRecommendation:
    if evaluation.recommendation is ChallengerRecommendation.ELIGIBLE_FOR_CHAMPION_REVIEW:
        return BatchLearningRecommendation.RECOMMEND_CHAMPION_REVIEW
    if evaluation.recommendation is ChallengerRecommendation.PAUSE_FOR_DEGRADATION:
        return BatchLearningRecommendation.PAUSE_LEARNING_FOR_DRIFT
    if evaluation.recommendation is ChallengerRecommendation.INSUFFICIENT_EVIDENCE:
        return BatchLearningRecommendation.CONTINUE_FORWARD_EVALUATION
    return BatchLearningRecommendation.CONTINUE_CURRENT_CHAMPION


def _policy_integer(policy: Mapping[str, object], name: str, default: int) -> int:
    value = policy.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BatchLearningError("BATCH_FORWARD_POLICY_INVALID")
    return value


def _decision(
    *,
    cycle: BatchLearningCycle,
    stage: BatchLearningDecisionStage,
    evidence_ids: Sequence[str],
    recommendation: BatchLearningRecommendation,
    reasons: Sequence[str],
    created_at: datetime,
    candidate_generation_decision_id: str | None = None,
    challenger_evaluation_id: str | None = None,
    champion_deployment_id: str | None = None,
    challenger_deployment_id: str | None = None,
    drift_state: str | None = None,
) -> BatchLearningDecision:
    evidence = tuple(evidence_ids)
    payload = {
        "cycle": cycle.cycle_id,
        "stage": stage.value,
        "evidence": evidence,
        "candidate_decision": candidate_generation_decision_id,
        "challenger_evaluation": challenger_evaluation_id,
        "champion": champion_deployment_id,
        "challenger": challenger_deployment_id,
        "recommendation": recommendation.value,
        "reasons": tuple(reasons),
    }
    return BatchLearningDecision(
        deterministic_id("batch_learning_decision", payload),
        cycle.cycle_id,
        stage,
        evidence,
        content_digest(evidence),
        candidate_generation_decision_id,
        challenger_evaluation_id,
        champion_deployment_id,
        challenger_deployment_id,
        "BLOCKED" if recommendation is BatchLearningRecommendation.BLOCKED_INTEGRITY else "PASSED",
        drift_state,
        recommendation,
        tuple(reasons),
        None,
        content_digest(payload),
        content_digest({"recommendation": recommendation.value, "reasons": tuple(reasons)}),
        created_at,
    )
