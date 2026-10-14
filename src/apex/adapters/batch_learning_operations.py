"""Local, one-pass operations for bounded automated batch learning."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import sqlalchemy as sa

from apex.adapters.batch_learning_postgres import PostgresBatchLearningRepository
from apex.adapters.candidate_generation_operations import CandidateGenerationRuntime
from apex.adapters.postgres import (
    PostgresSettings,
    create_postgres_engine,
    strategy_lab_forward_evaluation,
    strategy_lab_forward_prediction,
    strategy_lab_model_deployment,
)
from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.adapters.strategy_lab_postgres import (
    PostgresStrategyLabShadowStore,
    PostgresStrategyLabStore,
)
from apex.adapters.strategy_lab_shadow_models import StrategyLabManagedModelLoader
from apex.adapters.strategy_lab_shadow_synthetic import DeterministicForwardAdapter
from apex.adapters.strategy_lab_synthetic import DeterministicStrategyLabDemoAdapter
from apex.application.batch_learning import (
    AUTOMATED_LEARNING_WARNING,
    CHAMPION_WARNING,
    AttachApprovedBatchLearningChallenger,
    BatchForwardInferenceExecutor,
    BatchForwardOutcomeExecutor,
    BatchLearningCycle,
    BatchLearningCycleBinding,
    BatchLearningCycleExecutor,
    BatchLearningCycleState,
    BatchLearningDecision,
    BatchLearningError,
    BatchLearningEvidenceSource,
    BatchLearningEvidenceWindow,
    BatchLearningLifecycle,
    BatchLearningPlan,
    BatchLearningPlanState,
    BatchLearningTriggerType,
    CancelBatchLearningCycle,
    CreateBatchLearningPlan,
    CycleExecutionResult,
    EvaluateBatchForwardWindow,
    EvaluateBatchLearningTrigger,
    EvidenceSnapshot,
    ForwardInferenceResult,
    ForwardShadowEvaluationWindow,
    RecoverBatchLearningCycle,
    ResolveBatchForwardOutcomes,
    RunBatchForwardInference,
    RunBatchLearningCycle,
)
from apex.application.candidate_generation import MirrorReconciliation
from apex.application.research_integrity import OpenResearchExperimentFamily
from apex.application.strategy_lab import ModelFamily
from apex.application.strategy_lab_shadow import (
    StrategyLabChallengerEvaluation,
    StrategyLabForwardFeatureAdapter,
    StrategyLabShadowRepository,
    StrategyLabShadowService,
)
from apex.domain.candidate_generation import ObjectiveIdentity, initial_strategy_lab_search_space
from apex.domain.research_integrity import ResearchFrequency
from apex.domain.values import content_digest

MANAGED_RETAINED_SOURCE_SCOPE = "strategy-lab-forward-evaluations-v1"
MANAGED_SYNTHETIC_SOURCE_SCOPE = "strategy-lab-synthetic-resolved-v1"
MANAGED_SEARCH_TEMPLATE = "strategy-lab-initial-bounded-v1"
BATCH_LEARNING_WARNINGS = (
    AUTOMATED_LEARNING_WARNING,
    CHAMPION_WARNING,
    "AUTOMATED LEARNING DOES NOT AUTHORIZE MODEL DEPLOYMENT",
    "CHAMPION REPLACEMENT REQUIRES EXPLICIT OPERATOR ACTION",
)


class PostgresResolvedEvidenceSource(BatchLearningEvidenceSource):
    """Read only already-resolved Strategy Lab forward evidence."""

    adapter_id = MANAGED_RETAINED_SOURCE_SCOPE
    adapter_version = "1"

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def snapshot(
        self,
        *,
        source_scope: Mapping[str, object],
        prior_watermark: str | None,
        cutoff_at: datetime,
        maximum_observations: int,
    ) -> EvidenceSnapshot:
        if source_scope.get("managed_specification") != MANAGED_RETAINED_SOURCE_SCOPE:
            raise BatchLearningError("BATCH_LEARNING_SOURCE_SCOPE_INVALID")
        prior_at, prior_id = _decode_watermark(prior_watermark)
        statement = (
            sa.select(
                strategy_lab_forward_evaluation.c.evaluation_id,
                strategy_lab_forward_evaluation.c.resolved_at,
                strategy_lab_forward_prediction.c.grouping_id,
                strategy_lab_forward_prediction.c.source_lineage_hash,
            )
            .join(
                strategy_lab_forward_prediction,
                strategy_lab_forward_prediction.c.prediction_id
                == strategy_lab_forward_evaluation.c.prediction_id,
            )
            .join(
                strategy_lab_model_deployment,
                strategy_lab_model_deployment.c.deployment_id
                == strategy_lab_forward_prediction.c.deployment_id,
            )
            .where(
                strategy_lab_model_deployment.c.vertical == "SECURITIES",
                strategy_lab_forward_evaluation.c.resolved_at <= cutoff_at,
            )
            .order_by(
                strategy_lab_forward_evaluation.c.resolved_at,
                strategy_lab_forward_evaluation.c.evaluation_id,
            )
            .limit(maximum_observations + 1)
        )
        if prior_at is not None:
            statement = statement.where(
                sa.tuple_(
                    strategy_lab_forward_evaluation.c.resolved_at,
                    strategy_lab_forward_evaluation.c.evaluation_id,
                )
                > sa.tuple_(sa.literal(prior_at), sa.literal(prior_id))
            )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        bounded = rows[:maximum_observations]
        ids = tuple(str(row["evaluation_id"]) for row in bounded)
        groups = tuple(str(row["grouping_id"]) for row in bounded)
        latest = bounded[-1] if bounded else None
        watermark = (
            _encode_watermark(latest["resolved_at"], str(latest["evaluation_id"]))
            if latest is not None
            else prior_watermark or "EMPTY"
        )
        lineage = tuple(str(row["source_lineage_hash"]) for row in bounded)
        return EvidenceSnapshot(
            cutoff_at=cutoff_at,
            source_artifact_id=f"strategy-lab-resolved:{watermark}",
            source_artifact_hash=content_digest(
                {"evaluations": ids, "lineage": lineage, "cutoff": cutoff_at}
            ),
            observation_ids=ids,
            group_ids=groups,
            new_watermark=watermark,
            excluded_duplicate_count=len(ids) - len(set(ids)),
            earliest_evidence_at=bounded[0]["resolved_at"] if bounded else None,
            latest_evidence_at=latest["resolved_at"] if latest is not None else None,
            integrity_valid=len(ids) == len(set(ids)),
        )


class SyntheticResolvedEvidenceSource(BatchLearningEvidenceSource):
    adapter_id = MANAGED_SYNTHETIC_SOURCE_SCOPE
    adapter_version = "1"

    def __init__(self, *, observations: int, watermark: str) -> None:
        self._observations = observations
        self._watermark = watermark

    def snapshot(
        self,
        *,
        source_scope: Mapping[str, object],
        prior_watermark: str | None,
        cutoff_at: datetime,
        maximum_observations: int,
    ) -> EvidenceSnapshot:
        if source_scope.get("managed_specification") != MANAGED_SYNTHETIC_SOURCE_SCOPE:
            raise BatchLearningError("BATCH_LEARNING_SOURCE_SCOPE_INVALID")
        count = min(self._observations, maximum_observations)
        ids = tuple(f"synthetic-resolved-{self._watermark}-{index}" for index in range(count))
        return EvidenceSnapshot(
            cutoff_at=cutoff_at,
            source_artifact_id=f"synthetic-batch-learning:{self._watermark}",
            source_artifact_hash=content_digest({"ids": ids, "cutoff": cutoff_at}),
            observation_ids=ids,
            group_ids=tuple(f"synthetic-group-{index % 40}" for index in range(count)),
            new_watermark=self._watermark,
            earliest_evidence_at=cutoff_at - timedelta(days=30) if ids else None,
            latest_evidence_at=cutoff_at if ids else None,
        )


class ProductionBatchCycleExecutor(BatchLearningCycleExecutor):
    """Build one frozen family and delegate its search to migration 0029."""

    def __init__(self, runtime: CandidateGenerationRuntime) -> None:
        self._runtime = runtime

    def execute(
        self,
        *,
        plan: BatchLearningPlan,
        cycle: BatchLearningCycle,
        window: BatchLearningEvidenceWindow,
    ) -> CycleExecutionResult:
        if plan.source_scope.get("managed_specification") != MANAGED_SYNTHETIC_SOURCE_SCOPE:
            raise BatchLearningError("REAL_SECURITIES_BATCH_DATASET_ADAPTER_NOT_YET_AVAILABLE")
        dataset = self._runtime.strategy_service.build_dataset(
            adapter=DeterministicStrategyLabDemoAdapter(),
            name=f"{plan.name} cycle {cycle.ordinal}",
            cutoff_at=window.cutoff_at,
            idempotency_key=f"batch-learning-dataset-{cycle.cycle_id}",
        )
        if dataset.canonical_result_hash is None:
            raise BatchLearningError("BATCH_LEARNING_DATASET_NOT_READY")
        space = initial_strategy_lab_search_space()
        family = OpenResearchExperimentFamily(self._runtime.research_repository).execute(
            name=f"{plan.name} cycle {cycle.ordinal}",
            vertical=plan.vertical,
            research_family=f"{plan.research_family}:cycle:{cycle.ordinal}",
            research_question=str(
                plan.experiment_template.get(
                    "research_question",
                    "Can bounded retraining improve forward classification?",
                )
            ),
            target_label_identity=dataset.label_definition.canonical_hash,
            benchmark_identities=_string_sequence(
                plan.experiment_template.get("benchmarks", ("CONSTANT_PREVALENCE",))
            ),
            data_scope_identities=(dataset.dataset_id, window.window_id),
            data_scope_hashes=(
                dataset.canonical_result_hash,
                window.canonical_result_hash,
            ),
            universe_hash=plan.source_scope_hash,
            period_start=str(dataset.feature_first_at),
            period_end=str(dataset.feature_last_at),
            cutoff_at=window.cutoff_at,
            feature_schema_hashes=(dataset.schema.canonical_hash,),
            permitted_families=plan.permitted_model_families,
            parameter_space={"parameters": space.payload},
            maximum_trials=plan.maximum_trials_per_cycle,
            maximum_feature_variants=1,
            maximum_model_variants=plan.maximum_trials_per_cycle,
            maximum_simulator_variants=1,
            evaluation_policy_id=plan.fold_policy_id,
            evaluation_policy_payload=dict(plan.experiment_template),
            cost_execution_assumptions={"authority": "NONE", "execution": "ABSENT"},
            frequency=ResearchFrequency.DAILY,
            operator_action_id=f"batch-learning-cycle:{cycle.cycle_id}",
            operator_reason="Immutable cycle-specific bounded learning family",
            idempotency_key=f"batch-learning-family-{cycle.cycle_id}",
            created_at=window.cutoff_at,
        )
        search_plan = self._runtime.create_plan(
            experiment_id=family.experiment_id,
            name=f"{plan.name} cycle {cycle.ordinal} search",
            dataset_id=dataset.dataset_id,
            feature_schema_id=dataset.schema.schema_id,
            specification=MANAGED_SEARCH_TEMPLATE,
            objective=ObjectiveIdentity.BRIER,
            maximum_trials=plan.maximum_trials_per_cycle,
            seed=_integer_setting(plan.search_template, "seed", 20260727),
            reason="Frozen batch-learning cycle search",
            idempotency_key=f"batch-learning-search-plan-{cycle.cycle_id}",
        )
        run, decision, bindings = self._runtime.run_search(
            search_plan_id=search_plan.search_plan_id,
            requested_trials=plan.maximum_trials_per_cycle,
            idempotency_key=f"batch-learning-search-run-{cycle.cycle_id}",
        )
        eligible_binding = next(
            (
                item
                for item in bindings
                if item.research_trial_id == decision.selected_candidate_trial_id
            ),
            None,
        )
        eligible_candidate = eligible_binding.candidate_id if eligible_binding is not None else None
        return CycleExecutionResult(
            dataset.dataset_id,
            dataset.canonical_result_hash,
            family.experiment_id,
            family.canonical_input_hash,
            search_plan.search_plan_id,
            search_plan.canonical_input_hash,
            run.search_run_id,
            decision.decision_id,
            decision.recommendation,
            eligible_candidate,
            tuple(item.research_trial_id for item in bindings),
            tuple(item.candidate_id for item in bindings if item.candidate_id is not None),
            tuple(
                item.mlflow_mirror_record_id
                for item in bindings
                if item.mlflow_mirror_record_id is not None
            ),
        )

    def reconcile(self, cycle: BatchLearningCycle) -> tuple[str, ...]:
        reasons: list[str] = []
        if cycle.search_run_id is not None:
            reasons.extend(self._runtime.recover(cycle.search_run_id))
            if cycle.experiment_id is not None:
                result = self._runtime.reconcile_mlflow(cycle.experiment_id)
                reconciliations = cast(Sequence[MirrorReconciliation], result["reconciliations"])
                reasons.extend(f"MLFLOW_{item.state.value}" for item in reconciliations)
        if (
            cycle.dataset_id is not None
            and self._runtime.strategy_store.dataset(cycle.dataset_id) is None
        ):
            return ("IRRECONCILABLE", "DATASET_BINDING_MISSING")
        return tuple(dict.fromkeys(reasons or ("NO_SUBORDINATE_WORK_RECORDED",)))


class ProductionBatchForwardExecutor(BatchForwardInferenceExecutor, BatchForwardOutcomeExecutor):
    def __init__(
        self,
        service: StrategyLabShadowService,
        store: StrategyLabShadowRepository,
    ) -> None:
        self._service = service
        self._store = store

    @staticmethod
    def _adapter(cutoff_at: datetime) -> StrategyLabForwardFeatureAdapter:
        return DeterministicForwardAdapter(
            max(0, (cutoff_at.date() - datetime(2026, 4, 1, tzinfo=UTC).date()).days) % 100
        )

    def execute(
        self,
        *,
        window: ForwardShadowEvaluationWindow,
        cutoff_at: datetime,
    ) -> ForwardInferenceResult:
        adapter = self._adapter(cutoff_at)
        champion_batch = (
            self._service.run_inference(
                deployment_id=window.champion_deployment_id,
                adapter=adapter,
                cutoff_at=cutoff_at,
                maximum_rows=10_000,
                idempotency_key=f"batch-forward:{window.window_id}:champion:{cutoff_at.isoformat()}",
            )
            if window.champion_deployment_id is not None
            else None
        )
        challenger_batch = self._service.run_inference(
            deployment_id=window.challenger_deployment_id,
            adapter=adapter,
            cutoff_at=cutoff_at,
            maximum_rows=10_000,
            idempotency_key=f"batch-forward:{window.window_id}:challenger:{cutoff_at.isoformat()}",
        )
        return ForwardInferenceResult(
            champion_batch.batch_id if champion_batch is not None else None,
            challenger_batch.batch_id,
            champion_batch.prediction_count if champion_batch is not None else 0,
            challenger_batch.prediction_count,
            (champion_batch.rejected_row_count if champion_batch is not None else 0)
            + challenger_batch.rejected_row_count,
        )

    def resolve(
        self,
        *,
        deployment_id: str,
        inference_batch_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> int:
        return self._service.resolve_predictions(
            deployment_id=deployment_id,
            adapter=self._adapter(cutoff_at - timedelta(days=10)),
            cutoff_at=cutoff_at,
            batch_ids=inference_batch_ids,
        )

    def evaluate(
        self,
        *,
        deployment_id: str,
        inference_batch_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> StrategyLabChallengerEvaluation:
        return self._service.evaluate_challenger(
            deployment_id=deployment_id,
            cutoff_at=cutoff_at,
            batch_ids=inference_batch_ids,
        )

    def matched_counts(
        self,
        *,
        champion_deployment_id: str | None,
        challenger_deployment_id: str,
        inference_batch_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> tuple[int, int, int, str, tuple[str, ...]]:
        del cutoff_at

        selected_batches = set(inference_batch_ids)

        def resolved(deployment_id: str) -> dict[tuple[object, ...], str]:
            evaluations = {
                item.prediction_id: item for item in self._store.forward_evaluations(deployment_id)
            }
            return {
                (
                    item.entity_id,
                    item.grouping_id,
                    item.feature_at,
                    item.positive_outcome_identity,
                ): evaluations[item.prediction_id].evaluation_id
                for item in self._store.predictions(deployment_id)
                if item.prediction_id in evaluations and item.batch_id in selected_batches
            }

        challenger = resolved(challenger_deployment_id)
        champion = resolved(champion_deployment_id) if champion_deployment_id else {}
        matched = set(challenger) & set(champion) if champion_deployment_id else set(challenger)
        evaluation_ids = tuple(
            sorted(
                {
                    *(challenger[key] for key in matched),
                    *(champion[key] for key in matched if key in champion),
                }
            )
        )
        unmatched = len(challenger) + len(champion) - 2 * len(matched)
        return (
            len(matched) if champion_deployment_id else 0,
            len(matched),
            unmatched,
            content_digest(tuple(sorted(str(item) for item in matched))),
            evaluation_ids,
        )


@dataclass(slots=True)
class BatchLearningRuntime:
    engine: Any
    repository: PostgresBatchLearningRepository
    candidate_runtime: CandidateGenerationRuntime
    shadow_store: PostgresStrategyLabShadowStore
    shadow_service: StrategyLabShadowService
    local_root: Path

    @classmethod
    def open(
        cls,
        *,
        settings: PostgresSettings | None = None,
        local_root: Path | None = None,
    ) -> BatchLearningRuntime:
        root = (local_root or Path(".local")).resolve()
        selected = settings or PostgresSettings.from_env()
        engine = create_postgres_engine(selected)
        candidate = CandidateGenerationRuntime.open(settings=selected, local_root=root)
        shadow_store = PostgresStrategyLabShadowStore(engine)
        artifacts = LocalStrategyLabArtifactStore(root / "strategy_lab" / "artifacts")
        return cls(
            engine,
            PostgresBatchLearningRepository(engine),
            candidate,
            shadow_store,
            StrategyLabShadowService(
                PostgresStrategyLabStore(engine),
                shadow_store,
                artifacts,
                StrategyLabManagedModelLoader(),
            ),
            root,
        )

    def close(self) -> None:
        self.candidate_runtime.close()
        self.engine.dispose()

    def create_plan(
        self,
        *,
        name: str,
        source_scope: str,
        feature_schema_id: str,
        search_template: str,
        minimum_new_groups: int,
        minimum_new_observations: int,
        maximum_trials_per_cycle: int,
        maximum_cycles: int,
        reason: str,
        idempotency_key: str,
    ) -> BatchLearningPlan:
        if source_scope not in {
            MANAGED_RETAINED_SOURCE_SCOPE,
            MANAGED_SYNTHETIC_SOURCE_SCOPE,
        }:
            raise BatchLearningError("MANAGED_BATCH_SOURCE_SCOPE_REQUIRED")
        if search_template != MANAGED_SEARCH_TEMPLATE:
            raise BatchLearningError("MANAGED_SEARCH_TEMPLATE_REQUIRED")
        schema = self.candidate_runtime.strategy_store.feature_schema(feature_schema_id)
        if schema is None:
            raise BatchLearningError("BATCH_LEARNING_FEATURE_SCHEMA_NOT_FOUND")
        failed = min(maximum_trials_per_cycle - 1, max(1, maximum_trials_per_cycle // 5))
        return CreateBatchLearningPlan(self.repository).execute(
            name=name,
            vertical="SECURITIES",
            research_family="AUTOMATED_BATCH_CLASSIFICATION",
            source_adapter_id=source_scope,
            source_adapter_version="1",
            source_scope={"managed_specification": source_scope},
            feature_schema_id=schema.schema_id,
            feature_schema_hash=schema.canonical_hash,
            dataset_policy_id="RESOLVED_GROUPED_SNAPSHOT_V1",
            dataset_policy={
                "resolved_only": True,
                "availability_cutoff": True,
                "historical_context": "FROZEN",
            },
            experiment_template={
                "maximum_trials": maximum_trials_per_cycle,
                "research_question": "Can bounded retraining improve forward classification?",
                "benchmarks": ("CONSTANT_PREVALENCE",),
            },
            permitted_model_families=tuple(ModelFamily),
            search_template={
                "managed_specification": search_template,
                "maximum_trials": maximum_trials_per_cycle,
                "seed": 20260727,
            },
            objective_policy_id="BRIER_CLASSIFICATION_V1",
            objective_policy={"identity": "BRIER", "direction": "MINIMIZE"},
            fold_policy_id="GROUPED_CHRONOLOGICAL_WALK_FORWARD_V1",
            fold_policy={"grouped": True, "chronological": True},
            minimum_new_resolved_observations=minimum_new_observations,
            minimum_new_independent_groups=minimum_new_groups,
            maximum_observations_per_cycle=10_000,
            maximum_trials_per_cycle=maximum_trials_per_cycle,
            maximum_failed_trials_per_cycle=failed,
            maximum_cycles=maximum_cycles,
            minimum_cooldown_seconds=86_400,
            drift_policy={"identity": "STRATEGY_LAB_DRIFT_V1"},
            forward_policy={
                "minimum_predictions": 120,
                "minimum_resolved_outcomes": 120,
                "maximum_duration_seconds": 2_592_000,
            },
            champion_comparison_policy={"matched_rows_only": True},
            mlflow_mirror_policy_id="LOCAL_MLFLOW_MIRROR_V1",
            mlflow_mirror_policy={"host": "127.0.0.1", "authority": "NONE"},
            operator_action_id=f"batch-learning-create:{idempotency_key}",
            operator_reason=reason,
            idempotency_key=idempotency_key,
            created_at=datetime.now(tz=UTC),
        )

    def activate(
        self, *, plan_id: str, confirm_plan_id: str, confirm_plan_hash: str
    ) -> BatchLearningPlan:
        return BatchLearningLifecycle(self.repository).activate(
            plan_id=plan_id,
            confirm_plan_id=confirm_plan_id,
            confirm_plan_hash=confirm_plan_hash,
            opened_at=datetime.now(tz=UTC),
        )

    def control(self, *, plan_id: str, action: str, reason: str) -> BatchLearningPlan:
        return BatchLearningLifecycle(self.repository).control(
            plan_id=plan_id,
            action=action,
            reason=reason,
            at=datetime.now(tz=UTC),
        )

    def trigger(
        self,
        *,
        plan_id: str,
        idempotency_key: str,
        source: BatchLearningEvidenceSource | None = None,
        cutoff_at: datetime | None = None,
    ) -> tuple[BatchLearningEvidenceWindow, BatchLearningCycle, BatchLearningDecision]:
        plan = self.repository.plan(plan_id)
        if plan is None:
            raise BatchLearningError("BATCH_LEARNING_PLAN_NOT_FOUND")
        selected = source or self._source_for_plan(plan)
        return EvaluateBatchLearningTrigger(self.repository).execute(
            plan_id=plan_id,
            source=selected,
            cutoff_at=cutoff_at or datetime.now(tz=UTC),
            trigger_type=BatchLearningTriggerType.NEW_RESOLVED_EVIDENCE,
            trigger_evidence={"source": selected.adapter_id, "provider_fetch": False},
            idempotency_key=idempotency_key,
        )

    def _source_for_plan(self, plan: BatchLearningPlan) -> BatchLearningEvidenceSource:
        if plan.source_adapter_id == MANAGED_SYNTHETIC_SOURCE_SCOPE:
            return SyntheticResolvedEvidenceSource(
                observations=0,
                watermark=f"worker-empty:{plan.plan_id}",
            )
        return PostgresResolvedEvidenceSource(self.engine)

    def run_cycle(
        self, *, cycle_id: str
    ) -> tuple[BatchLearningCycle, BatchLearningCycleBinding, BatchLearningDecision]:
        return RunBatchLearningCycle(
            self.repository, ProductionBatchCycleExecutor(self.candidate_runtime)
        ).execute(
            cycle_id=cycle_id,
            lease_owner="batch-learning-worker-once",
            lease_token=content_digest({"cycle": cycle_id, "started": datetime.now(tz=UTC)}),
            lease_seconds=600,
            now=datetime.now(tz=UTC),
        )

    def recover(self, *, cycle_id: str) -> BatchLearningCycle:
        return RecoverBatchLearningCycle(
            self.repository, ProductionBatchCycleExecutor(self.candidate_runtime)
        ).execute(cycle_id=cycle_id, now=datetime.now(tz=UTC))

    def cancel(self, *, cycle_id: str, reason: str) -> BatchLearningCycle:
        return CancelBatchLearningCycle(self.repository).execute(
            cycle_id=cycle_id, reason=reason, canceled_at=datetime.now(tz=UTC)
        )

    def attach(
        self, *, cycle_id: str, deployment_id: str, idempotency_key: str
    ) -> tuple[
        BatchLearningCycle,
        BatchLearningCycleBinding,
        ForwardShadowEvaluationWindow,
    ]:
        deployment = self.shadow_store.deployment(deployment_id)
        cycle = self.repository.cycle(cycle_id)
        plan = self.repository.plan(cycle.plan_id) if cycle is not None else None
        if deployment is None or plan is None:
            raise BatchLearningError("APPROVED_BATCH_CHALLENGER_REQUIRED")
        assignment = self.shadow_store.active_champion(plan.vertical, deployment.research_family)
        champion = (
            self.shadow_store.deployment(assignment.deployment_id)
            if assignment is not None
            else None
        )
        return AttachApprovedBatchLearningChallenger(self.repository).execute(
            cycle_id=cycle_id,
            deployment=deployment,
            champion=champion,
            operator_action_id=f"batch-learning-attach:{idempotency_key}",
            idempotency_key=idempotency_key,
            opened_at=datetime.now(tz=UTC),
        )

    def infer(self, *, window_id: str, cutoff_at: datetime) -> object:
        window = self.repository.forward_window(window_id)
        cycle = self.repository.cycle(window.cycle_id) if window is not None else None
        plan = self.repository.plan(cycle.plan_id) if cycle is not None else None
        if plan is None or plan.source_adapter_id != MANAGED_SYNTHETIC_SOURCE_SCOPE:
            raise BatchLearningError("REAL_SECURITIES_FORWARD_ADAPTER_NOT_YET_AVAILABLE")
        executor = ProductionBatchForwardExecutor(self.shadow_service, self.shadow_store)
        return RunBatchForwardInference(self.repository, executor).execute(
            window_id=window_id, cutoff_at=cutoff_at
        )

    def resolve_and_evaluate(self, *, window_id: str, cutoff_at: datetime) -> dict[str, object]:
        window = self.repository.forward_window(window_id)
        cycle = self.repository.cycle(window.cycle_id) if window is not None else None
        plan = self.repository.plan(cycle.plan_id) if cycle is not None else None
        if plan is None or plan.source_adapter_id != MANAGED_SYNTHETIC_SOURCE_SCOPE:
            raise BatchLearningError("REAL_SECURITIES_FORWARD_ADAPTER_NOT_YET_AVAILABLE")
        executor = ProductionBatchForwardExecutor(self.shadow_service, self.shadow_store)
        resolved = ResolveBatchForwardOutcomes(self.repository, executor).execute(
            window_id=window_id, cutoff_at=cutoff_at
        )
        window, cycle, decision = EvaluateBatchForwardWindow(self.repository, executor).execute(
            window_id=window_id, cutoff_at=cutoff_at
        )
        return {
            "resolved": resolved,
            "window": window,
            "cycle": cycle,
            "decision": decision,
        }

    def status(self, *, plan_id: str) -> dict[str, object]:
        plan = self.repository.plan(plan_id)
        if plan is None:
            raise BatchLearningError("BATCH_LEARNING_PLAN_NOT_FOUND")
        cycles = self.repository.cycles(plan_id)
        return {
            "warnings": BATCH_LEARNING_WARNINGS,
            "plan": plan,
            "evidence_windows": self.repository.evidence_windows(plan_id),
            "cycles": cycles,
            "decisions": tuple(
                decision
                for cycle in cycles
                for decision in self.repository.decisions(cycle.cycle_id)
            ),
            "authority": "NONE",
        }

    def report(self, *, cycle_id: str) -> dict[str, object]:
        cycle = self.repository.cycle(cycle_id)
        if cycle is None:
            raise BatchLearningError("BATCH_LEARNING_CYCLE_NOT_FOUND")
        return {
            "warnings": BATCH_LEARNING_WARNINGS,
            "cycle": cycle,
            "evidence_window": self.repository.evidence_window(cycle.evidence_window_id),
            "binding": self.repository.binding(cycle_id),
            "forward_window": self.repository.forward_window(cycle.forward_window_id)
            if cycle.forward_window_id
            else None,
            "decisions": self.repository.decisions(cycle_id),
            "automatic_deployment": False,
            "automatic_champion_replacement": False,
            "authority": "NONE",
        }

    def worker_once(self, *, plan_id: str | None = None) -> dict[str, object]:
        plans = tuple(
            item
            for item in self.repository.plans()
            if item.state is BatchLearningPlanState.ACTIVE
            and (plan_id is None or item.plan_id == plan_id)
        )
        if not plans:
            return {"status": "NO_ACTIVE_PLAN", "processed": 0}
        plan = plans[0]
        forward_cycle = next(
            (
                item
                for item in self.repository.cycles(plan.plan_id)
                if item.state is BatchLearningCycleState.FORWARD_EVALUATING
                and item.forward_window_id is not None
            ),
            None,
        )
        if forward_cycle is not None:
            cutoff = datetime.now(tz=UTC).replace(hour=23, minute=59, second=0, microsecond=0)
            inferred = self.infer(
                window_id=cast(str, forward_cycle.forward_window_id),
                cutoff_at=cutoff,
            )
            evaluated = self.resolve_and_evaluate(
                window_id=cast(str, forward_cycle.forward_window_id),
                cutoff_at=cutoff,
            )
            return {
                "status": "FORWARD_WINDOW_ADVANCED",
                "processed": 1,
                "inference": inferred,
                "evaluation": evaluated,
            }
        requested = next(
            (
                item
                for item in self.repository.cycles(plan.plan_id)
                if item.state is BatchLearningCycleState.REQUESTED
            ),
            None,
        )
        if requested is not None:
            result = self.run_cycle(cycle_id=requested.cycle_id)
            return {"status": "CYCLE_ADVANCED", "processed": 1, "result": result}
        trigger_result = self.trigger(
            plan_id=plan.plan_id,
            idempotency_key=f"worker-trigger:{plan.plan_id}:{datetime.now(tz=UTC).date()}",
        )
        return {
            "status": "TRIGGER_EVALUATED",
            "processed": 1,
            "result": trigger_result,
        }

    def readiness(self) -> dict[str, object]:
        datasets = tuple(
            item
            for item in self.candidate_runtime.strategy_store.datasets()
            if item.state.value == "READY"
        )
        deployments = self.shadow_store.deployments()
        champions = self.shadow_store.champions()
        plans = self.repository.plans()
        compatible = tuple(
            item for item in plans if item.source_adapter_id == MANAGED_RETAINED_SOURCE_SCOPE
        )
        schema_ids = {item.schema.schema_id for item in datasets}
        source = PostgresResolvedEvidenceSource(self.engine)
        scope_readiness: list[dict[str, object]] = []
        for plan in compatible:
            terminal = tuple(
                item
                for item in self.repository.cycles(plan.plan_id)
                if item.state
                in {
                    BatchLearningCycleState.NO_ELIGIBLE_CANDIDATE,
                    BatchLearningCycleState.ELIGIBLE_FOR_SHADOW_REVIEW,
                    BatchLearningCycleState.WAITING_FOR_SHADOW_APPROVAL,
                    BatchLearningCycleState.FORWARD_EVALUATING,
                    BatchLearningCycleState.COMPLETED,
                }
            )
            prior_window = (
                self.repository.evidence_window(terminal[-1].evidence_window_id)
                if terminal
                else None
            )
            snapshot = source.snapshot(
                source_scope=plan.source_scope,
                prior_watermark=prior_window.new_watermark if prior_window else None,
                cutoff_at=datetime.now(tz=UTC),
                maximum_observations=plan.maximum_observations_per_cycle,
            )
            observations = len(snapshot.observation_ids)
            groups = len(set(snapshot.group_ids))
            remaining = max(0, plan.maximum_cycles - len(terminal))
            evidence_ready = (
                observations >= plan.minimum_new_resolved_observations
                and groups >= plan.minimum_new_independent_groups
                and snapshot.integrity_valid
            )
            scope_readiness.append(
                {
                    "plan_id": plan.plan_id,
                    "state": plan.state.value,
                    "source_scope_hash": plan.source_scope_hash,
                    "prior_watermark": prior_window.new_watermark if prior_window else None,
                    "new_resolved_observations": observations,
                    "new_independent_groups": groups,
                    "excluded_unresolved": snapshot.excluded_unresolved_count,
                    "excluded_duplicate": snapshot.excluded_duplicate_count,
                    "excluded_unavailable": snapshot.excluded_unavailable_count,
                    "excluded_integrity": snapshot.excluded_integrity_count,
                    "feature_schema_compatible": plan.feature_schema_id in schema_ids,
                    "remaining_cycles": remaining,
                    "remaining_trials": remaining * plan.maximum_trials_per_cycle,
                    "evidence_ready": evidence_ready,
                    "real_cycle_ready": False,
                    "blocked_reason": "REAL_SECURITIES_BATCH_DATASET_ADAPTER_NOT_YET_AVAILABLE",
                }
            )
        return {
            "warnings": BATCH_LEARNING_WARNINGS,
            "plans": plans,
            "compatible_retained_scopes": compatible,
            "ready_datasets": datasets,
            "feature_schema_ids": tuple(sorted(schema_ids)),
            "deployments": deployments,
            "champions": champions,
            "scope_readiness": tuple(scope_readiness),
            "remaining_experiment_search_budgets": tuple(
                {
                    "plan_id": item["plan_id"],
                    "remaining_cycles": item["remaining_cycles"],
                    "remaining_trials": item["remaining_trials"],
                }
                for item in scope_readiness
            ),
            "classification_forward_evaluation_only": True,
            "simulator_mismatch_blocked_families": (),
            "insufficient_return_evidence_families": (),
            "classification_only_plan_ids": tuple(item.plan_id for item in plans),
            "real_cycle_ready": False,
            "recommended_initial_cycle_budget": {
                "trials": 16,
                "maximum_failed_trials": 3,
                "concurrency": 1,
            },
            "real_cycle_started": False,
            "provider_calls": 0,
            "authority": "NONE",
        }


def _encode_watermark(value: datetime, identity: str) -> str:
    return f"{value.isoformat()}|{identity}"


def _decode_watermark(value: str | None) -> tuple[datetime | None, str]:
    if value in {None, "EMPTY"}:
        return None, ""
    try:
        at, identity = str(value).split("|", 1)
        parsed = datetime.fromisoformat(at)
    except (ValueError, TypeError) as error:
        raise BatchLearningError("BATCH_LEARNING_WATERMARK_INVALID") from error
    if parsed.tzinfo is None or not identity:
        raise BatchLearningError("BATCH_LEARNING_WATERMARK_INVALID")
    return parsed, identity


def _string_sequence(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, (tuple, list))
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise BatchLearningError("BATCH_LEARNING_TEMPLATE_SEQUENCE_INVALID")
    return tuple(value)


def _integer_setting(values: Mapping[str, object], name: str, default: int) -> int:
    value = values.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BatchLearningError("BATCH_LEARNING_TEMPLATE_INTEGER_INVALID")
    return value
