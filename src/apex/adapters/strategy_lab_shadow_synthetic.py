"""Deterministic, provider-free synthetic evidence for the SHADOW demo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.adapters.strategy_lab_shadow_models import StrategyLabManagedModelLoader
from apex.adapters.strategy_lab_synthetic import DeterministicStrategyLabDemoAdapter
from apex.application.strategy_lab import (
    CalibrationMethod,
    GroupedWalkForwardPolicy,
    InMemoryStrategyLabRepository,
    ModelFamily,
    StrategyLabModelCandidate,
    StrategyLabService,
    StrategyLabTrainingRun,
)
from apex.application.strategy_lab_shadow import (
    DeploymentRole,
    InMemoryStrategyLabShadowRepository,
    StrategyLabForwardFeatureAdapter,
    StrategyLabForwardPrediction,
    StrategyLabForwardRow,
    StrategyLabModelDeployment,
    StrategyLabResolvedOutcome,
    StrategyLabShadowService,
)
from apex.domain.values import content_digest


class DeterministicForwardAdapter(StrategyLabForwardFeatureAdapter):
    adapter_id = "synthetic-forward-event-market-v1"
    adapter_version = "1"
    feature_schema_hash = DeterministicStrategyLabDemoAdapter.schema.canonical_hash

    def __init__(self, batch_ordinal: int, *, degraded: bool = False) -> None:
        self._batch_ordinal = batch_ordinal
        self._degraded = degraded

    def source_identity(self, *, cutoff_at: datetime) -> str:
        return content_digest({"synthetic-forward": self._batch_ordinal, "cutoff": cutoff_at})

    def rows(self, *, cutoff_at: datetime, maximum_rows: int) -> tuple[StrategyLabForwardRow, ...]:
        base = datetime(2026, 4, 1, tzinfo=UTC) + timedelta(days=self._batch_ordinal)
        config = content_digest({"synthetic-forward-config": "v1"})
        values: list[StrategyLabForwardRow] = []
        for group in range(40):
            signal = Decimal((group * 29 + self._batch_ordinal * 17 + 7) % 97 + 2) / Decimal(100)
            at = base + timedelta(minutes=group)
            if at > cutoff_at:
                continue
            values.append(
                StrategyLabForwardRow(
                    row_id=f"forward-{self._batch_ordinal}-{group}",
                    entity_id=f"forward-market-{self._batch_ordinal}-{group}",
                    grouping_id=f"forward-event-{group}",
                    feature_at=at,
                    feature_values=(
                        Decimal("0.5"),
                        signal,
                        Decimal(10_000 + group) if self._degraded else Decimal(100 + group),
                        Decimal("0.02"),
                    ),
                    baseline_probability=Decimal("0.5"),
                    positive_outcome_identity="YES" if signal >= Decimal("0.5") else "NO",
                    source_lineage_hash=content_digest(
                        {"forward": self._batch_ordinal, "group": group}
                    ),
                    source_configuration_hash=config,
                )
            )
        return tuple(values[:maximum_rows])

    def resolve(
        self, prediction: StrategyLabForwardPrediction, *, cutoff_at: datetime
    ) -> StrategyLabResolvedOutcome | None:
        resolved_at = prediction.feature_at + timedelta(days=10)
        if resolved_at > cutoff_at:
            return None
        return StrategyLabResolvedOutcome(
            content_digest(
                {
                    "prediction": prediction.prediction_id,
                    "resolved": prediction.positive_outcome_identity,
                }
            ),
            resolved_at,
            (prediction.positive_outcome_identity == "YES") != self._degraded,
        )


def run_strategy_lab_shadow_demo(*, artifact_root: Path | None = None) -> dict[str, object]:
    artifacts = LocalStrategyLabArtifactStore(artifact_root)
    research = InMemoryStrategyLabRepository()
    shadow = InMemoryStrategyLabShadowRepository()
    foundation = StrategyLabService(research, artifacts)
    adapter = DeterministicStrategyLabDemoAdapter()
    policy = GroupedWalkForwardPolicy()
    dataset = foundation.build_dataset(
        adapter=adapter,
        name="SHADOW demo dataset",
        cutoff_at=datetime(2026, 3, 1, tzinfo=UTC),
        idempotency_key="shadow-demo-dataset",
    )
    run = foundation.train_model(
        dataset_id=dataset.dataset_id,
        family=ModelFamily.LOGISTIC_REGRESSION,
        hyperparameters={"C": 1.0},
        calibration=CalibrationMethod.PLATT,
        seed=42,
        idempotency_key="shadow-demo-train",
        policy=policy,
    )
    _, candidate = foundation.evaluate_candidate(training_run_id=run.training_run_id, policy=policy)
    service = StrategyLabShadowService(research, shadow, artifacts, StrategyLabManagedModelLoader())
    review = service.request_review(
        candidate_id=candidate.candidate_id,
        reason="deterministic research review",
        idempotency_key="shadow-demo-review",
    )
    review = service.start_review(
        review_id=review.review_id, reason="lineage checked", idempotency_key="shadow-demo-start"
    )
    wrong_confirmation_rejected = False
    try:
        service.approve_review(
            review_id=review.review_id,
            role=DeploymentRole.CHALLENGER,
            candidate_id="wrong",
            dataset_id=dataset.dataset_id,
            artifact_sha256=run.model_artifact.sha256 if run.model_artifact else "",
            evaluation_id=candidate.evaluation_id,
            reason="wrong",
            idempotency_key="shadow-demo-approve-wrong",
        )
    except Exception:
        wrong_confirmation_rejected = True
    assert run.model_artifact is not None
    deployment = service.approve_review(
        review_id=review.review_id,
        role=DeploymentRole.CHALLENGER,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        artifact_sha256=run.model_artifact.sha256,
        evaluation_id=candidate.evaluation_id,
        reason="approved",
        idempotency_key="shadow-demo-approve",
    )
    for ordinal in range(3):
        service.run_inference(
            deployment_id=deployment.deployment_id,
            adapter=DeterministicForwardAdapter(ordinal),
            cutoff_at=datetime(2026, 4, 5, tzinfo=UTC),
            maximum_rows=40,
            idempotency_key=f"shadow-demo-batch-{ordinal}",
        )
    resolved = service.resolve_predictions(
        deployment_id=deployment.deployment_id,
        adapter=DeterministicForwardAdapter(0),
        cutoff_at=datetime(2026, 4, 20, tzinfo=UTC),
    )
    evaluation = service.evaluate_challenger(
        deployment_id=deployment.deployment_id, cutoff_at=datetime(2026, 4, 20, tzinfo=UTC)
    )
    assignment = service.assign_champion(
        deployment_id=deployment.deployment_id,
        reason="synthetic evidence only",
        confirm_deployment=deployment.deployment_id,
        idempotency_key="shadow-demo-champion",
    )
    paused = service.control_deployment(
        deployment_id=deployment.deployment_id,
        action="pause",
        reason="demo",
        idempotency_key="shadow-demo-pause",
    )
    paused_blocks_inference = False
    try:
        service.run_inference(
            deployment_id=deployment.deployment_id,
            adapter=DeterministicForwardAdapter(4),
            cutoff_at=datetime(2026, 4, 10, tzinfo=UTC),
            maximum_rows=40,
            idempotency_key="shadow-demo-paused-block",
        )
    except Exception:
        paused_blocks_inference = True
    resumed = service.control_deployment(
        deployment_id=deployment.deployment_id,
        action="resume",
        reason="demo",
        idempotency_key="shadow-demo-resume",
    )
    replay = service.run_inference(
        deployment_id=deployment.deployment_id,
        adapter=DeterministicForwardAdapter(2),
        cutoff_at=datetime(2026, 4, 5, tzinfo=UTC),
        maximum_rows=40,
        idempotency_key="shadow-demo-batch-2",
    )
    replay_conflict_rejected = False
    try:
        service.run_inference(
            deployment_id=deployment.deployment_id,
            adapter=DeterministicForwardAdapter(2),
            cutoff_at=datetime(2026, 4, 5, tzinfo=UTC),
            maximum_rows=39,
            idempotency_key="shadow-demo-batch-2",
        )
    except Exception:
        replay_conflict_rejected = True
    revoked = service.control_deployment(
        deployment_id=deployment.deployment_id,
        action="revoke",
        reason="demo",
        idempotency_key="shadow-demo-revoke",
    )
    revoked_blocks_inference = False
    try:
        service.run_inference(
            deployment_id=deployment.deployment_id,
            adapter=DeterministicForwardAdapter(5),
            cutoff_at=datetime(2026, 4, 10, tzinfo=UTC),
            maximum_rows=40,
            idempotency_key="shadow-demo-revoked-block",
        )
    except Exception:
        revoked_blocks_inference = True

    def deploy_candidate(
        *, suffix: str, hyperparameters: dict[str, object], seed: int
    ) -> tuple[StrategyLabModelCandidate, StrategyLabTrainingRun, StrategyLabModelDeployment]:
        candidate_run = foundation.train_model(
            dataset_id=dataset.dataset_id,
            family=ModelFamily.LOGISTIC_REGRESSION,
            hyperparameters=hyperparameters,
            calibration=CalibrationMethod.PLATT,
            seed=seed,
            idempotency_key=f"shadow-demo-train-{suffix}",
            policy=policy,
        )
        _, candidate_value = foundation.evaluate_candidate(
            training_run_id=candidate_run.training_run_id, policy=policy
        )
        assert candidate_run.model_artifact is not None
        review_value = service.request_review(
            candidate_id=candidate_value.candidate_id,
            reason=f"deterministic {suffix} research review",
            idempotency_key=f"shadow-demo-review-{suffix}",
        )
        review_value = service.start_review(
            review_id=review_value.review_id,
            reason="lineage checked",
            idempotency_key=f"shadow-demo-start-{suffix}",
        )
        deployment_value = service.approve_review(
            review_id=review_value.review_id,
            role=DeploymentRole.CHALLENGER,
            candidate_id=candidate_value.candidate_id,
            dataset_id=dataset.dataset_id,
            artifact_sha256=candidate_run.model_artifact.sha256,
            evaluation_id=candidate_value.evaluation_id,
            reason="approved for synthetic shadow evidence",
            idempotency_key=f"shadow-demo-approve-{suffix}",
        )
        return candidate_value, candidate_run, deployment_value

    candidate_b, _run_b, deployment_b = deploy_candidate(
        suffix="b", hyperparameters={"C": 1.0}, seed=43
    )
    for ordinal in range(3):
        service.run_inference(
            deployment_id=deployment_b.deployment_id,
            adapter=DeterministicForwardAdapter(ordinal, degraded=True),
            cutoff_at=datetime(2026, 4, 5, tzinfo=UTC),
            maximum_rows=40,
            idempotency_key=f"shadow-demo-batch-b-{ordinal}",
        )
    resolved_b = service.resolve_predictions(
        deployment_id=deployment_b.deployment_id,
        adapter=DeterministicForwardAdapter(0, degraded=True),
        cutoff_at=datetime(2026, 4, 20, tzinfo=UTC),
    )
    evaluation_b = service.evaluate_challenger(
        deployment_id=deployment_b.deployment_id, cutoff_at=datetime(2026, 4, 20, tzinfo=UTC)
    )
    active_champion_after_b = shadow.active_champion(
        deployment.vertical, deployment.research_family
    )

    _candidate_c, run_c, deployment_c = deploy_candidate(
        suffix="c", hyperparameters={"C": 1.0}, seed=44
    )
    assert run_c.model_artifact is not None
    artifact_path = artifacts.root / run_c.model_artifact.relative_location
    artifact_path.write_bytes(b"fixture-managed-corrupt-model")
    invalidated = False
    try:
        service.run_inference(
            deployment_id=deployment_c.deployment_id,
            adapter=DeterministicForwardAdapter(0),
            cutoff_at=datetime(2026, 4, 5, tzinfo=UTC),
            maximum_rows=40,
            idempotency_key="shadow-demo-corrupt-batch",
        )
    except Exception:
        invalidated = True
    invalidated_deployment = shadow.deployment(deployment_c.deployment_id)
    retired = service.control_deployment(
        deployment_id=deployment_b.deployment_id,
        action="pause",
        reason="deterministic retirement proof",
        idempotency_key="shadow-demo-pause-b",
    )
    retired = service.control_deployment(
        deployment_id=deployment_b.deployment_id,
        action="retire",
        reason="deterministic retirement proof",
        idempotency_key="shadow-demo-retire-b",
    )
    retired_blocks_inference = False
    try:
        service.run_inference(
            deployment_id=deployment_b.deployment_id,
            adapter=DeterministicForwardAdapter(6),
            cutoff_at=datetime(2026, 4, 10, tzinfo=UTC),
            maximum_rows=40,
            idempotency_key="shadow-demo-retired-block",
        )
    except Exception:
        retired_blocks_inference = True
    return {
        "warning": "SHADOW MODEL — NO CAPITAL AUTHORITY",
        "candidate_state": candidate.state.value,
        "deployment_state": deployment.state.value,
        "wrong_confirmation_rejected": wrong_confirmation_rejected,
        "forward_predictions": len(shadow.predictions(deployment.deployment_id)),
        "resolved_predictions": resolved,
        "challenger_recommendation": evaluation.recommendation.value,
        "champion_assignment": assignment.state.value,
        "pause_resume_revoke": (paused.state.value, resumed.state.value, revoked.state.value),
        "paused_blocks_inference": paused_blocks_inference,
        "revoked_blocks_inference": revoked_blocks_inference,
        "retire_state": retired.state.value,
        "retired_blocks_inference": retired_blocks_inference,
        "exact_replay_batch_id": replay.batch_id,
        "replay_conflict_rejected": replay_conflict_rejected,
        "candidate_b_state": candidate_b.state.value,
        "candidate_b_resolved_predictions": resolved_b,
        "candidate_b_recommendation": evaluation_b.recommendation.value,
        "candidate_b_drift_state": evaluation_b.drift_metrics["state"],
        "champion_unchanged_after_b": active_champion_after_b is not None
        and active_champion_after_b.deployment_id == deployment.deployment_id,
        "artifact_integrity_failed": invalidated,
        "artifact_invalidated_state": invalidated_deployment.state.value
        if invalidated_deployment
        else None,
        "artifact_invalidated_predictions": len(shadow.predictions(deployment_c.deployment_id)),
        "provider_calls": 0,
        "opportunity_proposals": 0,
        "financial_mutations": 0,
        "strategy_authority": 0,
    }
