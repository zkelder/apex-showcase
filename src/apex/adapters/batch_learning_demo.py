"""Deterministic product demonstration for automated batch learning."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import sqlalchemy as sa

from apex.adapters.batch_learning_operations import (
    BATCH_LEARNING_WARNINGS,
    MANAGED_SEARCH_TEMPLATE,
    MANAGED_SYNTHETIC_SOURCE_SCOPE,
    BatchLearningRuntime,
    ProductionBatchForwardExecutor,
    SyntheticResolvedEvidenceSource,
)
from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.adapters.strategy_lab_shadow_models import StrategyLabManagedModelLoader
from apex.adapters.strategy_lab_shadow_synthetic import DeterministicForwardAdapter
from apex.adapters.strategy_lab_synthetic import DeterministicStrategyLabDemoAdapter
from apex.application.batch_learning import (
    AttachApprovedBatchLearningChallenger,
    BatchLearningConflict,
    BatchLearningCycle,
    BatchLearningCycleExecutor,
    BatchLearningCycleState,
    BatchLearningError,
    BatchLearningEvidenceWindow,
    BatchLearningLifecycle,
    BatchLearningPlan,
    BatchLearningTriggerType,
    CancelBatchLearningCycle,
    CreateBatchLearningPlan,
    CycleExecutionResult,
    EvaluateBatchForwardWindow,
    EvaluateBatchLearningTrigger,
    InMemoryBatchLearningRepository,
    RecoverBatchLearningCycle,
    ResolveBatchForwardOutcomes,
    RunBatchForwardInference,
    RunBatchLearningCycle,
)
from apex.application.candidate_generation import (
    AutomatedCandidateTrialBinding,
    CandidateGenerationRecommendation,
)
from apex.application.strategy_lab import (
    CalibrationMethod,
    InMemoryStrategyLabRepository,
    ModelFamily,
    StrategyLabModelCandidate,
    StrategyLabService,
)
from apex.application.strategy_lab_shadow import (
    DeploymentRole,
    InMemoryStrategyLabShadowRepository,
    StrategyLabModelDeployment,
    StrategyLabShadowService,
)
from apex.domain.values import content_digest

DEMO_AT = datetime(2026, 7, 27, 18, tzinfo=UTC)


class _EligibleFixtureCycleExecutor(BatchLearningCycleExecutor):
    def __init__(self, candidate: StrategyLabModelCandidate) -> None:
        self._candidate = candidate

    def execute(
        self,
        *,
        plan: BatchLearningPlan,
        cycle: BatchLearningCycle,
        window: BatchLearningEvidenceWindow,
    ) -> CycleExecutionResult:
        del plan, cycle, window
        return CycleExecutionResult(
            dataset_id="fixture-batch-dataset",
            dataset_hash=self._candidate.dataset_hash,
            experiment_id="fixture-cycle-family",
            experiment_hash=content_digest({"fixture": "cycle-family"}),
            search_plan_id="fixture-search-plan",
            search_plan_hash=content_digest({"fixture": "search-plan"}),
            search_run_id="fixture-search-run",
            candidate_decision_id="fixture-existing-gates-decision",
            candidate_recommendation=(
                CandidateGenerationRecommendation.ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW
            ),
            eligible_candidate_id=self._candidate.candidate_id,
            research_trial_ids=("fixture-retained-trial",),
            candidate_ids=(self._candidate.candidate_id,),
            mlflow_record_ids=("fixture-local-mirror",),
        )

    def reconcile(self, cycle: BatchLearningCycle) -> tuple[str, ...]:
        del cycle
        return ("LEASE_EXPIRED", "NO_DUPLICATE_TRIALS")


def _financial_counts(runtime: BatchLearningRuntime) -> dict[str, int]:
    tables = (
        "opportunity_proposal",
        "risk_reservation",
        "order",
        "position",
        "settlement_fact",
        "ledger_journal",
        "securities_paper_order",
        "securities_position_state",
    )
    with runtime.engine.connect() as connection:
        return {
            table: int(connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) or 0)
            for table in tables
        }


def _approve(
    *,
    service: StrategyLabShadowService,
    candidate: StrategyLabModelCandidate,
    research: InMemoryStrategyLabRepository,
    suffix: str,
    role: DeploymentRole,
) -> StrategyLabModelDeployment:
    training = research.training(candidate.training_run_id)
    if training is None or training.model_artifact is None:
        raise RuntimeError("fixture training artifact missing")
    dataset = research.dataset(training.dataset_id)
    if dataset is None:
        raise RuntimeError("fixture dataset missing")
    review = service.request_review(
        candidate_id=candidate.candidate_id,
        reason=f"Exact-confirmed fixture {suffix} SHADOW review",
        idempotency_key=f"batch-learning-fixture-review-{suffix}",
    )
    review = service.start_review(
        review_id=review.review_id,
        reason="Lineage and research-only artifact verified",
        idempotency_key=f"batch-learning-fixture-review-start-{suffix}",
    )
    return service.approve_review(
        review_id=review.review_id,
        role=role,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        artifact_sha256=training.model_artifact.sha256,
        evaluation_id=candidate.evaluation_id,
        reason="Explicit exact-confirmed SHADOW approval",
        idempotency_key=f"batch-learning-fixture-review-approve-{suffix}",
    )


def _forward_fixture(local_root: Path) -> dict[str, object]:
    artifacts = LocalStrategyLabArtifactStore(local_root / "batch-learning" / "fixture-models")
    research = InMemoryStrategyLabRepository()
    shadow = InMemoryStrategyLabShadowRepository()
    foundation = StrategyLabService(research, artifacts)
    dataset = foundation.build_dataset(
        adapter=DeterministicStrategyLabDemoAdapter(),
        name="Automated batch learning forward fixture",
        cutoff_at=datetime(2026, 3, 1, tzinfo=UTC),
        idempotency_key="batch-learning-forward-fixture-dataset",
    )
    champion_training = foundation.train_model(
        dataset_id=dataset.dataset_id,
        family=ModelFamily.LOGISTIC_REGRESSION,
        hyperparameters={"C": 1.0},
        calibration=CalibrationMethod.PLATT,
        seed=42,
        idempotency_key="batch-learning-forward-champion-training",
    )
    _, champion_candidate = foundation.evaluate_candidate(
        training_run_id=champion_training.training_run_id
    )
    challenger_training = foundation.train_model(
        dataset_id=dataset.dataset_id,
        family=ModelFamily.LOGISTIC_REGRESSION,
        hyperparameters={"C": 1.0},
        calibration=CalibrationMethod.PLATT,
        seed=43,
        idempotency_key="batch-learning-forward-challenger-training",
    )
    _, challenger_candidate = foundation.evaluate_candidate(
        training_run_id=challenger_training.training_run_id
    )
    shadow_service = StrategyLabShadowService(
        research, shadow, artifacts, StrategyLabManagedModelLoader()
    )
    champion = _approve(
        service=shadow_service,
        candidate=champion_candidate,
        research=research,
        suffix="champion",
        role=DeploymentRole.CHAMPION,
    )
    for ordinal in range(3):
        shadow_service.run_inference(
            deployment_id=champion.deployment_id,
            adapter=DeterministicForwardAdapter(ordinal),
            cutoff_at=datetime(2026, 4, 5, tzinfo=UTC),
            maximum_rows=40,
            idempotency_key=f"batch-learning-champion-proof-{ordinal}",
        )
    shadow_service.resolve_predictions(
        deployment_id=champion.deployment_id,
        adapter=DeterministicForwardAdapter(0),
        cutoff_at=datetime(2026, 4, 20, tzinfo=UTC),
    )
    champion_evaluation = shadow_service.evaluate_challenger(
        deployment_id=champion.deployment_id,
        cutoff_at=datetime(2026, 4, 20, tzinfo=UTC),
    )
    assignment = shadow_service.assign_champion(
        deployment_id=champion.deployment_id,
        reason="Explicit fixture research champion assignment",
        confirm_deployment=champion.deployment_id,
        idempotency_key="batch-learning-fixture-champion-assignment",
    )
    challenger = _approve(
        service=shadow_service,
        candidate=challenger_candidate,
        research=research,
        suffix="challenger",
        role=DeploymentRole.CHALLENGER,
    )

    repository = InMemoryBatchLearningRepository()
    draft = _fixture_plan(repository, dataset.schema.schema_id, dataset.schema.canonical_hash)
    plan = BatchLearningLifecycle(repository).activate(
        plan_id=draft.plan_id,
        confirm_plan_id=draft.plan_id,
        confirm_plan_hash=draft.canonical_input_hash,
        opened_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    window, requested, trigger_decision = EvaluateBatchLearningTrigger(repository).execute(
        plan_id=plan.plan_id,
        source=SyntheticResolvedEvidenceSource(observations=160, watermark="forward-fixture-ready"),
        cutoff_at=datetime(2026, 6, 2, tzinfo=UTC),
        trigger_type=BatchLearningTriggerType.NEW_RESOLVED_EVIDENCE,
        trigger_evidence={"fixture": "existing-strategy-lab-gates"},
        idempotency_key="batch-learning-forward-fixture-trigger",
    )
    cycle, _initial_binding, candidate_decision = RunBatchLearningCycle(
        repository, _EligibleFixtureCycleExecutor(challenger_candidate)
    ).execute(
        cycle_id=requested.cycle_id,
        lease_owner="fixture-worker",
        lease_token="fixture-lease",
        lease_seconds=60,
        now=datetime(2026, 6, 2, 0, 1, tzinfo=UTC),
    )
    cycle, _attached_binding, forward_window = AttachApprovedBatchLearningChallenger(
        repository
    ).execute(
        cycle_id=cycle.cycle_id,
        deployment=challenger,
        champion=champion,
        operator_action_id="fixture-explicit-shadow-approval",
        idempotency_key="batch-learning-forward-fixture-attach",
        opened_at=datetime(2026, 6, 3, tzinfo=UTC),
    )
    forward_executor = ProductionBatchForwardExecutor(shadow_service, shadow)
    for day in range(3):
        forward_window = RunBatchForwardInference(repository, forward_executor).execute(
            window_id=forward_window.window_id,
            cutoff_at=datetime(2026, 6, 5 + day, 23, 59, tzinfo=UTC),
        )
    pre_outcome_counts = (
        forward_window.champion_prediction_count,
        forward_window.challenger_prediction_count,
    )
    resolved = ResolveBatchForwardOutcomes(repository, forward_executor).execute(
        window_id=forward_window.window_id,
        cutoff_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    evaluated_window, completed_cycle, forward_decision = EvaluateBatchForwardWindow(
        repository, forward_executor
    ).execute(
        window_id=forward_window.window_id,
        cutoff_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    final_binding = repository.binding(completed_cycle.cycle_id)
    if final_binding is None:
        raise RuntimeError("completed forward binding is missing")
    champion_after = shadow.active_champion(champion.vertical, champion.research_family)

    replay = EvaluateBatchLearningTrigger(repository).execute(
        plan_id=plan.plan_id,
        source=SyntheticResolvedEvidenceSource(observations=160, watermark="forward-fixture-ready"),
        cutoff_at=datetime(2026, 6, 2, tzinfo=UTC),
        trigger_type=BatchLearningTriggerType.NEW_RESOLVED_EVIDENCE,
        trigger_evidence={"fixture": "existing-strategy-lab-gates"},
        idempotency_key="batch-learning-forward-fixture-trigger",
    )
    replay_conflict = False
    try:
        EvaluateBatchLearningTrigger(repository).execute(
            plan_id=plan.plan_id,
            source=SyntheticResolvedEvidenceSource(
                observations=159, watermark="forward-fixture-conflict"
            ),
            cutoff_at=datetime(2026, 6, 2, tzinfo=UTC),
            trigger_type=BatchLearningTriggerType.NEW_RESOLVED_EVIDENCE,
            trigger_evidence={"fixture": "conflict"},
            idempotency_key="batch-learning-forward-fixture-trigger",
        )
    except BatchLearningConflict:
        replay_conflict = True
    cooldown_blocked = False
    try:
        EvaluateBatchLearningTrigger(repository).execute(
            plan_id=plan.plan_id,
            source=SyntheticResolvedEvidenceSource(
                observations=160, watermark="forward-fixture-next"
            ),
            cutoff_at=datetime(2026, 7, 20, 0, 0, 1, tzinfo=UTC),
            trigger_type=BatchLearningTriggerType.NEW_RESOLVED_EVIDENCE,
            trigger_evidence={"fixture": "cooldown"},
            idempotency_key="batch-learning-forward-fixture-cooldown",
        )
    except BatchLearningError as error:
        cooldown_blocked = str(error) == "BATCH_LEARNING_COOLDOWN_ACTIVE"
    paused = BatchLearningLifecycle(repository).control(
        plan_id=plan.plan_id,
        action="pause",
        reason="fixture pause",
        at=datetime(2026, 7, 20, 1, tzinfo=UTC),
    )
    pause_blocked = False
    try:
        EvaluateBatchLearningTrigger(repository).execute(
            plan_id=plan.plan_id,
            source=SyntheticResolvedEvidenceSource(observations=160, watermark="paused"),
            cutoff_at=datetime(2026, 7, 22, tzinfo=UTC),
            trigger_type=BatchLearningTriggerType.NEW_RESOLVED_EVIDENCE,
            trigger_evidence={"fixture": "paused"},
            idempotency_key="batch-learning-forward-paused",
        )
    except BatchLearningError:
        pause_blocked = True
    resumed = BatchLearningLifecycle(repository).control(
        plan_id=paused.plan_id,
        action="resume",
        reason="fixture resume",
        at=datetime(2026, 7, 20, 2, tzinfo=UTC),
    )
    revoked = BatchLearningLifecycle(repository).control(
        plan_id=resumed.plan_id,
        action="revoke",
        reason="fixture revoke",
        at=datetime(2026, 7, 20, 3, tzinfo=UTC),
    )
    revoke_blocked = False
    try:
        RunBatchForwardInference(repository, forward_executor).execute(
            window_id=evaluated_window.window_id,
            cutoff_at=datetime(2026, 7, 21, tzinfo=UTC),
        )
    except BatchLearningError:
        revoke_blocked = True

    recovery_repository = InMemoryBatchLearningRepository()
    recovery_draft = _fixture_plan(
        recovery_repository,
        dataset.schema.schema_id,
        dataset.schema.canonical_hash,
        suffix="recovery",
    )
    recovery_plan = BatchLearningLifecycle(recovery_repository).activate(
        plan_id=recovery_draft.plan_id,
        confirm_plan_id=recovery_draft.plan_id,
        confirm_plan_hash=recovery_draft.canonical_input_hash,
        opened_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    _, recovery_cycle, _ = EvaluateBatchLearningTrigger(recovery_repository).execute(
        plan_id=recovery_plan.plan_id,
        source=SyntheticResolvedEvidenceSource(observations=160, watermark="recovery-ready"),
        cutoff_at=datetime(2026, 6, 2, tzinfo=UTC),
        trigger_type=BatchLearningTriggerType.NEW_RESOLVED_EVIDENCE,
        trigger_evidence={"fixture": "recovery"},
        idempotency_key="batch-learning-recovery-trigger",
    )
    recovery_cycle = recovery_repository.update_cycle(
        replace(
            recovery_cycle,
            state=BatchLearningCycleState.SEARCHING,
            lease_owner="interrupted-worker",
            lease_token="expired-lease",
            lease_expires_at=datetime(2026, 6, 2, 0, 1, tzinfo=UTC),
        )
    )
    recovered = RecoverBatchLearningCycle(
        recovery_repository, _EligibleFixtureCycleExecutor(challenger_candidate)
    ).execute(
        cycle_id=recovery_cycle.cycle_id,
        now=datetime(2026, 6, 2, 0, 2, tzinfo=UTC),
    )
    canceled = CancelBatchLearningCycle(recovery_repository).execute(
        cycle_id=recovered.cycle_id,
        reason="fixture bounded cancellation",
        canceled_at=datetime(2026, 6, 2, 0, 3, tzinfo=UTC),
    )

    return {
        "trigger_decision": trigger_decision,
        "candidate_decision": candidate_decision,
        "evidence_window": window,
        "binding": final_binding,
        "pre_outcome_prediction_counts": pre_outcome_counts,
        "resolved_prediction_counts": resolved,
        "forward_window": evaluated_window,
        "forward_decision": forward_decision,
        "completed_cycle": completed_cycle,
        "challenger_evaluation": shadow.challenger_evaluation(challenger.deployment_id),
        "champion_initial_evaluation": champion_evaluation,
        "champion_assignment": assignment,
        "champion_unchanged": (
            champion_after is not None and champion_after.deployment_id == champion.deployment_id
        ),
        "exact_replay": replay[1].cycle_id == cycle.cycle_id,
        "conflicting_replay_rejected": replay_conflict,
        "cooldown_blocked_duplicate_cycle": cooldown_blocked,
        "pause_blocked_new_cycle": pause_blocked,
        "revoked_state": revoked.state,
        "revoke_blocked_new_prediction": revoke_blocked,
        "recovered_state": recovered.state,
        "canceled_state": canceled.state,
        "automatic_deployment": False,
        "automatic_champion_replacement": False,
    }


def _fixture_plan(
    repository: InMemoryBatchLearningRepository,
    schema_id: str,
    schema_hash: str,
    *,
    suffix: str = "forward",
) -> BatchLearningPlan:
    return CreateBatchLearningPlan(repository).execute(
        name=f"Fixture batch learning {suffix}",
        vertical="SECURITIES",
        research_family="STRATEGY_LAB_CANDIDATE_GENERATION",
        source_adapter_id=MANAGED_SYNTHETIC_SOURCE_SCOPE,
        source_adapter_version="1",
        source_scope={"managed_specification": MANAGED_SYNTHETIC_SOURCE_SCOPE},
        feature_schema_id=schema_id,
        feature_schema_hash=schema_hash,
        dataset_policy_id="RESOLVED_GROUPED_SNAPSHOT_V1",
        dataset_policy={"resolved_only": True},
        experiment_template={"maximum_trials": 20},
        permitted_model_families=tuple(ModelFamily),
        search_template={"maximum_trials": 20, "seed": 17},
        objective_policy_id="BRIER_CLASSIFICATION_V1",
        objective_policy={"metric": "BRIER"},
        fold_policy_id="GROUPED_CHRONOLOGICAL_WALK_FORWARD_V1",
        fold_policy={"grouped": True},
        minimum_new_resolved_observations=120,
        minimum_new_independent_groups=30,
        maximum_observations_per_cycle=1_000,
        maximum_trials_per_cycle=20,
        maximum_failed_trials_per_cycle=4,
        maximum_cycles=3,
        minimum_cooldown_seconds=86_400,
        drift_policy={"identity": "STRATEGY_LAB_DRIFT_V1"},
        forward_policy={
            "minimum_predictions": 120,
            "minimum_resolved_outcomes": 120,
            "maximum_duration_seconds": 2_592_000,
        },
        champion_comparison_policy={"matched_rows_only": True},
        mlflow_mirror_policy_id="LOCAL_MLFLOW_MIRROR_V1",
        mlflow_mirror_policy={"host": "127.0.0.1"},
        operator_action_id=f"fixture-plan-{suffix}",
        operator_reason="Deterministic synthetic forward proof",
        idempotency_key=f"fixture-plan-{suffix}",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def automated_batch_learning_demo(*, local_root: Path) -> dict[str, object]:
    runtime = BatchLearningRuntime.open(local_root=local_root)
    try:
        before = _financial_counts(runtime)
        seed_dataset = runtime.candidate_runtime.strategy_service.build_dataset(
            adapter=DeterministicStrategyLabDemoAdapter(),
            name="Automated batch learning cycle seed",
            cutoff_at=DEMO_AT,
            idempotency_key="automated-batch-learning-seed-dataset",
        )
        plan = runtime.create_plan(
            name="Automated batch learning synthetic product demo",
            source_scope=MANAGED_SYNTHETIC_SOURCE_SCOPE,
            feature_schema_id=seed_dataset.schema.schema_id,
            search_template=MANAGED_SEARCH_TEMPLATE,
            minimum_new_groups=30,
            minimum_new_observations=120,
            maximum_trials_per_cycle=16,
            maximum_cycles=3,
            reason="Bounded synthetic-first automated learning demonstration",
            idempotency_key="automated-batch-learning-demo-plan",
        )
        if plan.state.value == "DRAFT":
            plan = runtime.activate(
                plan_id=plan.plan_id,
                confirm_plan_id=plan.plan_id,
                confirm_plan_hash=plan.canonical_input_hash,
            )
        retained_cycles = runtime.repository.cycles(plan.plan_id)
        retained_search_cycle = next(
            (item for item in retained_cycles if item.search_run_id is not None),
            None,
        )
        if retained_search_cycle is None:
            worker_result = runtime.worker_once(plan_id=plan.plan_id)
            insufficient = cast(
                tuple[BatchLearningEvidenceWindow, BatchLearningCycle, object],
                worker_result["result"],
            )
            ready = runtime.trigger(
                plan_id=plan.plan_id,
                idempotency_key="automated-batch-learning-demo-ready",
                source=SyntheticResolvedEvidenceSource(observations=160, watermark="demo-ready"),
                cutoff_at=DEMO_AT + timedelta(days=1),
            )
            cycle = ready[1]
            if cycle.state is BatchLearningCycleState.REQUESTED:
                completed, binding, decision = runtime.run_cycle(cycle_id=cycle.cycle_id)
            else:
                completed = cycle
                retained_binding = runtime.repository.binding(cycle.cycle_id)
                retained_decision = next(
                    (
                        item
                        for item in runtime.repository.decisions(cycle.cycle_id)
                        if item.decision_stage.value == "CANDIDATE_GENERATION"
                    ),
                    None,
                )
                if retained_binding is None or retained_decision is None:
                    raise RuntimeError("terminal batch cycle linkage is incomplete")
                binding, decision = retained_binding, retained_decision
        else:
            completed = retained_search_cycle
            waiting = next(
                (
                    item
                    for item in retained_cycles
                    if item.state is BatchLearningCycleState.WAITING_FOR_EVIDENCE
                ),
                None,
            )
            retained_binding = runtime.repository.binding(completed.cycle_id)
            retained_decision = next(
                (
                    item
                    for item in runtime.repository.decisions(completed.cycle_id)
                    if item.decision_stage.value == "CANDIDATE_GENERATION"
                ),
                None,
            )
            if waiting is None or retained_binding is None or retained_decision is None:
                raise RuntimeError("retained batch demo reconciliation is incomplete")
            waiting_window = runtime.repository.evidence_window(waiting.evidence_window_id)
            waiting_decision = next(iter(runtime.repository.decisions(waiting.cycle_id)), None)
            if waiting_window is None or waiting_decision is None:
                raise RuntimeError("retained insufficient-evidence proof is incomplete")
            insufficient = (waiting_window, waiting, waiting_decision)
            worker_result = {
                "status": "RETAINED_CYCLE_RECONCILED",
                "processed": 0,
                "result": insufficient,
            }
            binding, decision = retained_binding, retained_decision
        if completed.search_run_id is None:
            raise RuntimeError("batch learning demo search was not retained")
        search_status = runtime.candidate_runtime.status(completed.search_run_id)
        search_bindings = cast(Sequence[AutomatedCandidateTrialBinding], search_status["bindings"])
        search_decision = search_status["decision"]
        mlflow = (
            runtime.candidate_runtime.reconcile_mlflow(completed.experiment_id)
            if completed.experiment_id is not None
            else {}
        )
        forward = _forward_fixture(local_root)
        after = _financial_counts(runtime)
        readiness = runtime.readiness()
        return {
            "warnings": BATCH_LEARNING_WARNINGS,
            "plan": plan,
            "insufficient_worker_result": worker_result,
            "insufficient_evidence_cycle": insufficient[1],
            "learning_cycle": completed,
            "cycle_binding": binding,
            "cycle_decision": decision,
            "search_trial_count": len(search_bindings),
            "search_bindings": search_bindings,
            "search_decision": search_decision,
            "mlflow_reconciliation": mlflow,
            "forward_fixture": forward,
            "retained_data_readiness": readiness,
            "financial_counts_before": before,
            "financial_counts_after": after,
            "financial_state_unchanged": before == after,
            "provider_calls": 0,
            "broker_calls": 0,
            "normal_strategy_authority": False,
            "paper_authority": False,
            "live_authority": False,
            "worker_or_daemon_left_running": False,
        }
    finally:
        runtime.close()
