from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.batch_learning import (
    AttachApprovedBatchLearningChallenger,
    BatchLearningConflict,
    BatchLearningCycle,
    BatchLearningCycleState,
    BatchLearningDecision,
    BatchLearningError,
    BatchLearningEvidenceWindow,
    BatchLearningLifecycle,
    BatchLearningPlan,
    BatchLearningPlanState,
    BatchLearningRecommendation,
    BatchLearningTriggerType,
    CancelBatchLearningCycle,
    CreateBatchLearningPlan,
    CycleExecutionResult,
    EvaluateBatchForwardWindow,
    EvaluateBatchLearningTrigger,
    EvidenceSnapshot,
    EvidenceWindowState,
    ForwardInferenceResult,
    ForwardShadowEvaluationWindow,
    ForwardWindowState,
    InMemoryBatchLearningRepository,
    RecoverBatchLearningCycle,
    ResolveBatchForwardOutcomes,
    RunBatchForwardInference,
    RunBatchLearningCycle,
)
from apex.application.candidate_generation import CandidateGenerationRecommendation
from apex.application.strategy_lab import ModelFamily, StrategyLabArtifact
from apex.application.strategy_lab_shadow import (
    ChallengerEvaluationState,
    ChallengerRecommendation,
    DeploymentRole,
    DeploymentState,
    StrategyLabChallengerEvaluation,
    StrategyLabModelDeployment,
)
from apex.domain.values import content_digest

NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
HASH = "a" * 64


class _Evidence:
    adapter_id = "fixture-resolved-evidence"
    adapter_version = "1"

    def __init__(self, count: int, groups: int, watermark: str = "wm-1") -> None:
        self.count = count
        self.groups = groups
        self.watermark = watermark

    def snapshot(
        self,
        *,
        source_scope: Mapping[str, object],
        prior_watermark: str | None,
        cutoff_at: datetime,
        maximum_observations: int,
    ) -> EvidenceSnapshot:
        del source_scope, prior_watermark, maximum_observations
        return EvidenceSnapshot(
            cutoff_at=cutoff_at,
            source_artifact_id=f"artifact-{self.watermark}",
            source_artifact_hash=content_digest({"watermark": self.watermark}),
            observation_ids=tuple(f"observation-{index}" for index in range(self.count)),
            group_ids=tuple(f"group-{index % self.groups}" for index in range(self.count)),
            new_watermark=self.watermark,
            earliest_evidence_at=cutoff_at - timedelta(days=1),
            latest_evidence_at=cutoff_at,
        )


class _CycleExecutor:
    def __init__(self, *, eligible: bool = False, irreconcilable: bool = False) -> None:
        self.eligible = eligible
        self.irreconcilable = irreconcilable
        self.calls = 0

    def execute(
        self,
        *,
        plan: BatchLearningPlan,
        cycle: BatchLearningCycle,
        window: BatchLearningEvidenceWindow,
    ) -> CycleExecutionResult:
        del plan, cycle, window
        self.calls += 1
        return CycleExecutionResult(
            dataset_id="dataset-cycle",
            dataset_hash="b" * 64,
            experiment_id="experiment-cycle",
            experiment_hash="c" * 64,
            search_plan_id="search-plan-cycle",
            search_plan_hash="d" * 64,
            search_run_id="search-run-cycle",
            candidate_decision_id="candidate-decision-cycle",
            candidate_recommendation=(
                CandidateGenerationRecommendation.ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW
                if self.eligible
                else CandidateGenerationRecommendation.REQUIRE_MORE_EVIDENCE
            ),
            eligible_candidate_id="candidate-cycle" if self.eligible else None,
            research_trial_ids=("trial-1", "trial-2"),
            candidate_ids=("candidate-cycle",),
            mlflow_record_ids=("mirror-1", "mirror-2"),
        )

    def reconcile(self, cycle: BatchLearningCycle) -> tuple[str, ...]:
        del cycle
        return ("IRRECONCILABLE",) if self.irreconcilable else ("LEASE_EXPIRED",)


class _ForwardExecutor:
    def __init__(self) -> None:
        self.inference_calls = 0
        self.resolve_calls = 0
        self.evaluation_calls = 0

    def execute(
        self, *, window: ForwardShadowEvaluationWindow, cutoff_at: datetime
    ) -> ForwardInferenceResult:
        del cutoff_at
        self.inference_calls += 1
        return ForwardInferenceResult(
            f"batch-{window.window_id}-champion",
            f"batch-{window.window_id}-challenger",
            100,
            100,
            0,
        )

    def resolve(
        self,
        *,
        deployment_id: str,
        inference_batch_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> int:
        del deployment_id, inference_batch_ids, cutoff_at
        self.resolve_calls += 1
        return 100

    def matched_counts(
        self,
        *,
        champion_deployment_id: str | None,
        challenger_deployment_id: str,
        inference_batch_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> tuple[int, int, int, str, tuple[str, ...]]:
        del (
            champion_deployment_id,
            challenger_deployment_id,
            inference_batch_ids,
            cutoff_at,
        )
        return 100, 100, 0, "2" * 64, ("champion-evaluation", "challenger-evaluation")

    def evaluate(
        self,
        *,
        deployment_id: str,
        inference_batch_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> StrategyLabChallengerEvaluation:
        del inference_batch_ids
        self.evaluation_calls += 1
        return StrategyLabChallengerEvaluation(
            "challenger-comparison",
            deployment_id,
            "deployment-champion",
            cutoff_at,
            100,
            100,
            20,
            Decimal("0.10"),
            Decimal("0.25"),
            Decimal("0.12"),
            Decimal("0.35"),
            Decimal("0.69"),
            Decimal("0.38"),
            100,
            {"brier": "0.10"},
            {"state": "IMPROVED"},
            {"state": "STABLE"},
            (),
            ChallengerEvaluationState.COMPLETED,
            ChallengerRecommendation.ELIGIBLE_FOR_CHAMPION_REVIEW,
            ("CHALLENGER_OUTPERFORMS_CHAMPION",),
            "3" * 64,
            "4" * 64,
            cutoff_at,
        )


def _draft(repository: InMemoryBatchLearningRepository) -> BatchLearningPlan:
    return CreateBatchLearningPlan(repository).execute(
        name="Bounded daily classifier learning",
        vertical="SECURITIES",
        research_family="DAILY_CLASSIFICATION",
        source_adapter_id="fixture-resolved-evidence",
        source_adapter_version="1",
        source_scope={"universe": "fixture"},
        feature_schema_id="schema-fixture",
        feature_schema_hash=HASH,
        dataset_policy_id="resolved-only-v1",
        dataset_policy={"historical_context": True, "resolved_only": True},
        experiment_template={"maximum_trials": 24, "benchmark": "PREVALENCE"},
        permitted_model_families=tuple(ModelFamily),
        search_template={"maximum_trials": 24, "seed": 17},
        objective_policy_id="brier-v1",
        objective_policy={"metric": "BRIER", "direction": "MINIMIZE"},
        fold_policy_id="grouped-walk-forward-v1",
        fold_policy={"grouped": True, "chronological": True},
        minimum_new_resolved_observations=12,
        minimum_new_independent_groups=6,
        maximum_observations_per_cycle=100,
        maximum_trials_per_cycle=20,
        maximum_failed_trials_per_cycle=4,
        maximum_cycles=3,
        minimum_cooldown_seconds=60,
        drift_policy={"version": "V1"},
        forward_policy={
            "minimum_predictions": 100,
            "minimum_resolved_outcomes": 100,
            "maximum_duration_seconds": 86400,
        },
        champion_comparison_policy={"matched_rows_only": True},
        mlflow_mirror_policy_id="local-mirror-v1",
        mlflow_mirror_policy={"host": "127.0.0.1"},
        operator_action_id="operator-plan-create",
        operator_reason="Focused bounded learning proof",
        idempotency_key="batch-plan-create",
        created_at=NOW,
    )


def _active(repository: InMemoryBatchLearningRepository) -> BatchLearningPlan:
    draft = _draft(repository)
    return BatchLearningLifecycle(repository).activate(
        plan_id=draft.plan_id,
        confirm_plan_id=draft.plan_id,
        confirm_plan_hash=draft.canonical_input_hash,
        opened_at=NOW + timedelta(seconds=1),
    )


def _trigger(
    repository: InMemoryBatchLearningRepository,
    *,
    count: int = 12,
    groups: int = 6,
    watermark: str = "wm-1",
    key: str = "trigger-1",
    cutoff: datetime = NOW + timedelta(minutes=2),
) -> tuple[BatchLearningEvidenceWindow, BatchLearningCycle, BatchLearningDecision]:
    plan = repository.plans()[0]
    return EvaluateBatchLearningTrigger(repository).execute(
        plan_id=plan.plan_id,
        source=_Evidence(count, groups, watermark),
        cutoff_at=cutoff,
        trigger_type=BatchLearningTriggerType.NEW_RESOLVED_EVIDENCE,
        trigger_evidence={"resolved": count},
        idempotency_key=key,
    )


def test_plan_requires_exact_activation_and_is_immutable() -> None:
    repository = InMemoryBatchLearningRepository()
    draft = _draft(repository)
    replay = _draft(repository)
    assert replay == draft
    with pytest.raises(BatchLearningError, match="BATCH_LEARNING_ACTIVATION_CONFIRMATION_MISMATCH"):
        BatchLearningLifecycle(repository).activate(
            plan_id=draft.plan_id,
            confirm_plan_id="wrong",
            confirm_plan_hash=draft.canonical_input_hash,
            opened_at=NOW,
        )
    active = BatchLearningLifecycle(repository).activate(
        plan_id=draft.plan_id,
        confirm_plan_id=draft.plan_id,
        confirm_plan_hash=draft.canonical_input_hash,
        opened_at=NOW,
    )
    assert active.state is BatchLearningPlanState.ACTIVE
    with pytest.raises(BatchLearningConflict, match="IDEMPOTENCY_CONFLICT"):
        repository.save_plan(
            replace(draft, name="Conflicting replay", canonical_input_hash="f" * 64)
        )


def test_insufficient_evidence_is_honest_and_creates_no_search() -> None:
    repository = InMemoryBatchLearningRepository()
    _active(repository)
    window, cycle, decision = _trigger(repository, count=5, groups=3)
    assert window.state is EvidenceWindowState.INSUFFICIENT_EVIDENCE
    assert cycle.state is BatchLearningCycleState.WAITING_FOR_EVIDENCE
    assert cycle.search_run_id is None
    assert decision.recommendation is BatchLearningRecommendation.WAIT_FOR_MORE_EVIDENCE


def test_ready_trigger_replays_exactly_and_cycle_uses_existing_search_boundary() -> None:
    repository = InMemoryBatchLearningRepository()
    _active(repository)
    first = _trigger(repository)
    replay = _trigger(repository)
    assert replay == first
    window, cycle, _ = first
    executor = _CycleExecutor()
    completed, binding, decision = RunBatchLearningCycle(repository, executor).execute(
        cycle_id=cycle.cycle_id,
        lease_owner="worker-once",
        lease_token="lease-1",
        lease_seconds=60,
        now=NOW + timedelta(minutes=3),
    )
    assert executor.calls == 1
    assert completed.state is BatchLearningCycleState.NO_ELIGIBLE_CANDIDATE
    assert completed.lease_token is None
    assert binding.research_trial_ids == ("trial-1", "trial-2")
    assert decision.recommendation is BatchLearningRecommendation.NO_ELIGIBLE_CANDIDATE
    assert window.new_watermark == "wm-1"
    replayed_window, replayed_cycle, replayed_trigger = _trigger(repository)
    assert replayed_window == window
    assert replayed_cycle == completed
    assert replayed_trigger.decision_stage.value == "CYCLE_TRIGGER"


def test_failed_or_waiting_cycles_do_not_advance_successful_watermark() -> None:
    repository = InMemoryBatchLearningRepository()
    _active(repository)
    _, waiting, _ = _trigger(repository, count=1, groups=1, watermark="wm-wait")
    assert waiting.state is BatchLearningCycleState.WAITING_FOR_EVIDENCE
    _, next_cycle, _ = _trigger(
        repository,
        watermark="wm-ready",
        key="trigger-2",
        cutoff=NOW + timedelta(minutes=4),
    )
    next_window = repository.evidence_window(next_cycle.evidence_window_id)
    assert next_window is not None
    assert next_window.prior_watermark is None


def test_cycle_lease_recovery_and_cancellation_preserve_evidence() -> None:
    repository = InMemoryBatchLearningRepository()
    _active(repository)
    _, cycle, _ = _trigger(repository)
    leased = repository.update_cycle(
        replace(
            cycle,
            state=BatchLearningCycleState.SEARCHING,
            lease_owner="dead-worker",
            lease_token="expired",
            lease_expires_at=NOW,
        )
    )
    recovered = RecoverBatchLearningCycle(repository, _CycleExecutor()).execute(
        cycle_id=leased.cycle_id, now=NOW + timedelta(seconds=1)
    )
    assert recovered.state is BatchLearningCycleState.REQUESTED
    canceled = CancelBatchLearningCycle(repository).execute(
        cycle_id=recovered.cycle_id,
        reason="operator bounded stop",
        canceled_at=NOW + timedelta(seconds=2),
    )
    assert canceled.state is BatchLearningCycleState.CANCELED
    assert repository.evidence_window(cycle.evidence_window_id) is not None


def test_pause_and_revoke_block_new_trigger_work() -> None:
    repository = InMemoryBatchLearningRepository()
    plan = _active(repository)
    lifecycle = BatchLearningLifecycle(repository)
    paused = lifecycle.control(
        plan_id=plan.plan_id, action="pause", reason="operator pause", at=NOW
    )
    assert paused.state is BatchLearningPlanState.PAUSED
    with pytest.raises(BatchLearningError, match="PLAN_NOT_ACTIVE"):
        _trigger(repository)
    resumed = lifecycle.control(
        plan_id=plan.plan_id, action="resume", reason="operator resume", at=NOW
    )
    revoked = lifecycle.control(
        plan_id=resumed.plan_id, action="revoke", reason="operator revoke", at=NOW
    )
    assert revoked.state is BatchLearningPlanState.REVOKED
    with pytest.raises(BatchLearningError, match="PLAN_NOT_ACTIVE"):
        _trigger(repository)


def test_attach_requires_exact_existing_active_shadow_deployment() -> None:
    repository = InMemoryBatchLearningRepository()
    _active(repository)
    _, cycle, _ = _trigger(repository)
    cycle, _, _ = RunBatchLearningCycle(repository, _CycleExecutor(eligible=True)).execute(
        cycle_id=cycle.cycle_id,
        lease_owner="worker",
        lease_token="lease",
        lease_seconds=60,
        now=NOW + timedelta(minutes=3),
    )
    deployment = _deployment("candidate-cycle")
    attached, binding, window = AttachApprovedBatchLearningChallenger(repository).execute(
        cycle_id=cycle.cycle_id,
        deployment=deployment,
        champion=None,
        operator_action_id="operator-approved-shadow",
        idempotency_key="attach-1",
        opened_at=NOW + timedelta(minutes=4),
    )
    assert attached.state is BatchLearningCycleState.FORWARD_EVALUATING
    assert binding.approved_deployment_id == deployment.deployment_id
    assert window.challenger_deployment_id == deployment.deployment_id
    assert window.minimum_resolved_outcomes == 100
    assert (
        AttachApprovedBatchLearningChallenger(repository).execute(
            cycle_id=cycle.cycle_id,
            deployment=deployment,
            champion=None,
            operator_action_id="operator-approved-shadow",
            idempotency_key="attach-1",
            opened_at=NOW + timedelta(minutes=4),
        )[2]
        == window
    )


def test_forward_evidence_is_idempotent_aligned_and_only_recommends_review() -> None:
    repository = InMemoryBatchLearningRepository()
    _active(repository)
    _, cycle, _ = _trigger(repository)
    cycle, _, _ = RunBatchLearningCycle(repository, _CycleExecutor(eligible=True)).execute(
        cycle_id=cycle.cycle_id,
        lease_owner="worker",
        lease_token="lease",
        lease_seconds=60,
        now=NOW + timedelta(minutes=3),
    )
    champion = _deployment(
        "candidate-champion",
        deployment_id="deployment-champion",
        role=DeploymentRole.CHAMPION,
    )
    challenger = _deployment("candidate-cycle")
    cycle, _, window = AttachApprovedBatchLearningChallenger(repository).execute(
        cycle_id=cycle.cycle_id,
        deployment=challenger,
        champion=champion,
        operator_action_id="operator-approved-shadow",
        idempotency_key="attach-forward",
        opened_at=NOW + timedelta(minutes=4),
    )
    executor = _ForwardExecutor()
    first = RunBatchForwardInference(repository, executor).execute(
        window_id=window.window_id,
        cutoff_at=NOW + timedelta(minutes=5),
    )
    replay = RunBatchForwardInference(repository, executor).execute(
        window_id=window.window_id,
        cutoff_at=NOW + timedelta(minutes=5),
    )
    assert replay == first
    assert first.champion_prediction_count == 100
    assert first.challenger_prediction_count == 100
    assert len(repository.binding(cycle.cycle_id).inference_batch_ids) == 2  # type: ignore[union-attr]

    resolver = ResolveBatchForwardOutcomes(repository, executor)
    assert resolver.execute(window_id=window.window_id, cutoff_at=NOW + timedelta(days=10)) == (
        100,
        100,
    )
    completed, completed_cycle, decision = EvaluateBatchForwardWindow(repository, executor).execute(
        window_id=window.window_id, cutoff_at=NOW + timedelta(days=10)
    )
    assert completed.state is ForwardWindowState.COMPLETED
    assert completed.unmatched_count == 0
    assert completed_cycle.state is BatchLearningCycleState.COMPLETED
    assert decision.recommendation is BatchLearningRecommendation.RECOMMEND_CHAMPION_REVIEW
    assert decision.champion_deployment_id == champion.deployment_id
    assert decision.challenger_deployment_id == challenger.deployment_id

    assert resolver.execute(window_id=window.window_id, cutoff_at=NOW + timedelta(days=11)) == (
        100,
        100,
    )
    assert EvaluateBatchForwardWindow(repository, executor).execute(
        window_id=window.window_id, cutoff_at=NOW + timedelta(days=11)
    ) == (completed, completed_cycle, decision)
    assert executor.resolve_calls == 2
    assert executor.evaluation_calls == 1


def _deployment(
    candidate_id: str,
    *,
    deployment_id: str = "deployment-fixture",
    role: DeploymentRole = DeploymentRole.CHALLENGER,
) -> StrategyLabModelDeployment:
    return StrategyLabModelDeployment(
        deployment_id=deployment_id,
        review_id="review-explicitly-approved",
        candidate_id=candidate_id,
        role=role,
        vertical="SECURITIES",
        research_family="DAILY_CLASSIFICATION",
        model_family=ModelFamily.LOGISTIC_REGRESSION,
        feature_schema_id="schema-fixture",
        feature_schema_hash=HASH,
        label_definition_id="label-fixture",
        label_definition_hash="b" * 64,
        dataset_id="dataset-cycle",
        dataset_hash="c" * 64,
        training_run_id="training-cycle",
        training_hash="d" * 64,
        evaluation_id="evaluation-cycle",
        evaluation_hash="e" * 64,
        artifact=StrategyLabArtifact(
            relative_location="models/fixture.json",
            sha256="f" * 64,
            byte_size=100,
        ),
        calibration_identity="PLATT",
        runtime_identity="fixture",
        state=DeploymentState.ACTIVE,
        created_at=NOW,
        activated_at=NOW,
        paused_at=None,
        retired_at=None,
        invalidated_at=None,
        operator_action_id="operator-approved-shadow",
        canonical_payload={"feature_names": ("signal",)},
        canonical_hash="1" * 64,
        last_integrity_check_at=NOW,
    )
