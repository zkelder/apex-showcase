from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex.adapters.batch_learning_postgres import PostgresBatchLearningRepository
from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.adapters.strategy_lab_postgres import PostgresStrategyLabStore
from apex.adapters.strategy_lab_synthetic import DeterministicStrategyLabDemoAdapter
from apex.application.batch_learning import (
    BatchLearningConflict,
    BatchLearningCycleState,
    BatchLearningLifecycle,
    BatchLearningPlan,
    BatchLearningPlanState,
    BatchLearningTriggerType,
    CreateBatchLearningPlan,
    EvaluateBatchLearningTrigger,
    EvidenceSnapshot,
)
from apex.application.strategy_lab import (
    ModelFamily,
    StrategyLabDatasetSnapshot,
    StrategyLabService,
)
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
REVISION_0029 = "0029_candidate_generation"
REVISION_0030 = "0030_batch_learning"
HEAD_REVISION = "0118_evidence_gap_recovery"
TABLES = {
    "batch_learning_plan",
    "batch_learning_evidence_window",
    "batch_learning_cycle",
    "batch_learning_cycle_binding",
    "forward_shadow_evaluation_window",
    "batch_learning_decision",
}


class _Source:
    adapter_id = "fixture-resolved-evidence"
    adapter_version = "1"

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
            source_artifact_id="retained-evidence-1",
            source_artifact_hash=content_digest({"evidence": 1}),
            observation_ids=tuple(f"resolved-{index}" for index in range(12)),
            group_ids=tuple(f"group-{index % 6}" for index in range(12)),
            new_watermark="resolved-12",
            earliest_evidence_at=NOW,
            latest_evidence_at=cutoff_at,
        )


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _revision(connection: sa.Connection) -> str:
    return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _plan(
    engine: Engine, root: Path, repository: PostgresBatchLearningRepository
) -> tuple[StrategyLabDatasetSnapshot, BatchLearningPlan]:
    strategy = StrategyLabService(
        PostgresStrategyLabStore(engine),
        LocalStrategyLabArtifactStore(root / "strategy-lab"),
    )
    dataset = strategy.build_dataset(
        adapter=DeterministicStrategyLabDemoAdapter(),
        name="batch learning persistence dataset",
        cutoff_at=NOW,
        idempotency_key="batch-learning-persistence-dataset",
    )
    plan = CreateBatchLearningPlan(repository).execute(
        name="PostgreSQL bounded learning",
        vertical="SECURITIES",
        research_family="DAILY_CLASSIFICATION",
        source_adapter_id="fixture-resolved-evidence",
        source_adapter_version="1",
        source_scope={"universe": "fixture"},
        feature_schema_id=dataset.schema.schema_id,
        feature_schema_hash=dataset.schema.canonical_hash,
        dataset_policy_id="resolved-only-v1",
        dataset_policy={"resolved_only": True},
        experiment_template={"maximum_trials": 24},
        permitted_model_families=tuple(ModelFamily),
        search_template={"maximum_trials": 24, "seed": 17},
        objective_policy_id="brier-v1",
        objective_policy={"metric": "BRIER"},
        fold_policy_id="grouped-walk-forward-v1",
        fold_policy={"grouped": True},
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
        operator_action_id="operator-batch-plan",
        operator_reason="Focused PostgreSQL batch-learning proof",
        idempotency_key="batch-learning-plan",
        created_at=NOW,
    )
    return dataset, plan


def test_0030_empty_downgrade_and_reupgrade(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        assert _revision(connection) == HEAD_REVISION
        assert TABLES.issubset(inspect(connection).get_table_names())
        command.downgrade(_config(connection), REVISION_0029)
        connection.commit()
        assert _revision(connection) == REVISION_0029
        assert TABLES.isdisjoint(inspect(connection).get_table_names())
        command.upgrade(_config(connection), REVISION_0030)
        connection.commit()
        assert _revision(connection) == REVISION_0030
        assert TABLES.issubset(inspect(connection).get_table_names())


def test_plan_trigger_restart_and_exact_replay(postgres_engine: Engine, tmp_path: Path) -> None:
    repository = PostgresBatchLearningRepository(postgres_engine)
    _dataset, draft = _plan(postgres_engine, tmp_path, repository)
    active = BatchLearningLifecycle(repository).activate(
        plan_id=draft.plan_id,
        confirm_plan_id=draft.plan_id,
        confirm_plan_hash=draft.canonical_input_hash,
        opened_at=NOW,
    )
    assert active.state is BatchLearningPlanState.ACTIVE
    result = EvaluateBatchLearningTrigger(repository).execute(
        plan_id=active.plan_id,
        source=_Source(),
        cutoff_at=NOW,
        trigger_type=BatchLearningTriggerType.NEW_RESOLVED_EVIDENCE,
        trigger_evidence={"resolved": 12},
        idempotency_key="batch-trigger-1",
    )
    assert result[1].state is BatchLearningCycleState.REQUESTED
    restarted = PostgresBatchLearningRepository(postgres_engine)
    assert restarted.plan(active.plan_id) == active
    assert restarted.evidence_window(result[0].window_id) == result[0]
    assert restarted.cycle(result[1].cycle_id) == result[1]
    assert (
        EvaluateBatchLearningTrigger(restarted).execute(
            plan_id=active.plan_id,
            source=_Source(),
            cutoff_at=NOW,
            trigger_type=BatchLearningTriggerType.NEW_RESOLVED_EVIDENCE,
            trigger_evidence={"resolved": 12},
            idempotency_key="batch-trigger-1",
        )
        == result
    )


def test_active_plan_is_database_immutable_and_replay_conflicts(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    repository = PostgresBatchLearningRepository(postgres_engine)
    _dataset, draft = _plan(postgres_engine, tmp_path, repository)
    active = BatchLearningLifecycle(repository).activate(
        plan_id=draft.plan_id,
        confirm_plan_id=draft.plan_id,
        confirm_plan_hash=draft.canonical_input_hash,
        opened_at=NOW,
    )
    with (
        pytest.raises(
            sa.exc.ProgrammingError,
            match="active batch learning plan scope and budgets are immutable",
        ),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE batch_learning_plan "
                "SET maximum_cycles = maximum_cycles + 1 WHERE plan_id = :plan_id"
            ),
            {"plan_id": active.plan_id},
        )
    assert repository.plan(active.plan_id) == active
    with pytest.raises(BatchLearningConflict, match="PLAN_IDEMPOTENCY_CONFLICT"):
        repository.save_plan(replace(active, canonical_input_hash="f" * 64))


def test_retained_0030_evidence_blocks_downgrade_before_ddl(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), REVISION_0030)
        connection.commit()
    repository = PostgresBatchLearningRepository(postgres_engine)
    _plan(postgres_engine, tmp_path, repository)
    before_tables = set(inspect(postgres_engine).get_table_names())
    with postgres_engine.connect() as connection:
        with pytest.raises(
            sa.exc.ProgrammingError,
            match="cannot downgrade 0030 with retained batch learning",
        ):
            command.downgrade(_config(connection), REVISION_0029)
        connection.rollback()
        assert _revision(connection) == REVISION_0030
    assert set(inspect(postgres_engine).get_table_names()) == before_tables
    assert repository.plans()
