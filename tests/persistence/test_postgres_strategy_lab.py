from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.postgres import (
    strategy_lab_dataset_snapshot,
    strategy_lab_model_candidate,
    strategy_lab_training_run,
    strategy_lab_walk_forward_evaluation,
)
from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.adapters.strategy_lab_postgres import PostgresStrategyLabStore
from apex.adapters.strategy_lab_synthetic import DeterministicStrategyLabDemoAdapter
from apex.application.strategy_lab import (
    CalibrationMethod,
    ModelFamily,
    StrategyLabService,
)

pytestmark = pytest.mark.postgres


def test_strategy_lab_persists_reproducible_research_without_financial_state(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    service = StrategyLabService(
        PostgresStrategyLabStore(postgres_engine),
        LocalStrategyLabArtifactStore(tmp_path / "artifacts"),
    )
    adapter = DeterministicStrategyLabDemoAdapter()
    dataset = service.build_dataset(
        adapter=adapter,
        name="persistence proof",
        cutoff_at=datetime(2026, 3, 1, tzinfo=UTC),
        idempotency_key="dataset-v1",
    )
    run = service.train_model(
        dataset_id=dataset.dataset_id,
        family=ModelFamily.LOGISTIC_REGRESSION,
        hyperparameters={"C": 1.0},
        calibration=CalibrationMethod.PLATT,
        seed=42,
        idempotency_key="training-v1",
    )
    evaluation, candidate = service.evaluate_candidate(training_run_id=run.training_run_id)
    assert candidate.state.value == "ELIGIBLE_FOR_SHADOW_REVIEW"
    assert evaluation.completed_fold_count >= 3
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(strategy_lab_dataset_snapshot))
            == 1
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(strategy_lab_training_run))
            == 1
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(strategy_lab_walk_forward_evaluation)
            )
            == 1
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(strategy_lab_model_candidate))
            == 1
        )
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 0
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
