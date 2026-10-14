from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.postgres import (
    strategy_lab_challenger_evaluation,
    strategy_lab_forward_evaluation,
    strategy_lab_forward_prediction,
    strategy_lab_model_deployment,
    strategy_lab_operator_action,
    strategy_lab_shadow_review,
)
from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.adapters.strategy_lab_postgres import (
    PostgresStrategyLabShadowStore,
    PostgresStrategyLabStore,
)
from apex.adapters.strategy_lab_shadow_models import StrategyLabManagedModelLoader
from apex.adapters.strategy_lab_shadow_synthetic import DeterministicForwardAdapter
from apex.adapters.strategy_lab_synthetic import DeterministicStrategyLabDemoAdapter
from apex.application.strategy_lab import (
    CalibrationMethod,
    ModelFamily,
    StrategyLabService,
)
from apex.application.strategy_lab_shadow import DeploymentRole, StrategyLabShadowService

pytestmark = pytest.mark.postgres


def test_shadow_research_evidence_is_durable_and_never_financial(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    artifacts = LocalStrategyLabArtifactStore(tmp_path / "artifacts")
    research = PostgresStrategyLabStore(postgres_engine)
    service = StrategyLabService(research, artifacts)
    dataset = service.build_dataset(
        adapter=DeterministicStrategyLabDemoAdapter(),
        name="durable shadow evidence",
        cutoff_at=datetime(2026, 3, 1, tzinfo=UTC),
        idempotency_key="durable-shadow-dataset",
    )
    run = service.train_model(
        dataset_id=dataset.dataset_id,
        family=ModelFamily.LOGISTIC_REGRESSION,
        hyperparameters={"C": 1.0},
        calibration=CalibrationMethod.PLATT,
        seed=42,
        idempotency_key="durable-shadow-training",
    )
    _, candidate = service.evaluate_candidate(training_run_id=run.training_run_id)
    assert run.model_artifact is not None
    shadow = StrategyLabShadowService(
        research,
        PostgresStrategyLabShadowStore(postgres_engine),
        artifacts,
        StrategyLabManagedModelLoader(),
    )
    review = shadow.request_review(
        candidate_id=candidate.candidate_id,
        reason="durable research review",
        idempotency_key="durable-shadow-review",
    )
    review = shadow.start_review(
        review_id=review.review_id,
        reason="lineage reviewed",
        idempotency_key="durable-shadow-start",
    )
    deployment = shadow.approve_review(
        review_id=review.review_id,
        role=DeploymentRole.CHALLENGER,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        artifact_sha256=run.model_artifact.sha256,
        evaluation_id=candidate.evaluation_id,
        reason="explicit research approval",
        idempotency_key="durable-shadow-approve",
    )
    batch = shadow.run_inference(
        deployment_id=deployment.deployment_id,
        adapter=DeterministicForwardAdapter(0),
        cutoff_at=datetime(2026, 4, 5, tzinfo=UTC),
        maximum_rows=40,
        idempotency_key="durable-shadow-batch",
    )
    assert batch.prediction_count == 40
    assert (
        shadow.resolve_predictions(
            deployment_id=deployment.deployment_id,
            adapter=DeterministicForwardAdapter(0),
            cutoff_at=datetime(2026, 4, 20, tzinfo=UTC),
        )
        == 40
    )
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(strategy_lab_operator_action))
            == 3
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(strategy_lab_shadow_review))
            == 1
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(strategy_lab_model_deployment))
            == 1
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(strategy_lab_forward_prediction)
            )
            == 40
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(strategy_lab_forward_evaluation)
            )
            == 40
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(strategy_lab_challenger_evaluation)
            )
            == 0
        )
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 0
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
