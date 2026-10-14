from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex.adapters.candidate_generation_postgres import (
    PostgresCandidateGenerationRepository,
)
from apex.adapters.candidate_generation_tools import (
    DeterministicFixtureCandidateSearchAdapter,
    LocalMLflowMirrorAdapter,
)
from apex.adapters.research_integrity_artifacts import (
    LocalResearchReturnArtifactStore,
)
from apex.adapters.research_integrity_postgres import (
    PostgresResearchIntegrityRepository,
)
from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.adapters.strategy_lab_postgres import PostgresStrategyLabStore
from apex.adapters.strategy_lab_synthetic import DeterministicStrategyLabDemoAdapter
from apex.application.candidate_generation import (
    CandidateGenerationConflict,
    CreateAutomatedCandidateSearchPlan,
    RunAutomatedCandidateSearch,
    StrategyLabCandidateTrialExecutor,
    create_local_mlflow_workspace,
    reconcile_mlflow_mirror,
)
from apex.application.research_integrity import (
    OpenResearchExperimentFamily,
    ResearchIntegrityService,
    ResearchTrialLedger,
)
from apex.application.strategy_lab import ModelFamily, StrategyLabService
from apex.domain.candidate_generation import (
    ObjectiveIdentity,
    initial_strategy_lab_search_space,
)
from apex.domain.research_integrity import ResearchFrequency

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 27, 15, tzinfo=UTC)
REVISION_0028 = "0028_research_overfitting"
REVISION_0029 = "0029_candidate_generation"
REVISION_0030 = "0030_batch_learning"
REVISION_0031 = "0118_evidence_gap_recovery"
TABLES = {
    "automated_candidate_search_plan",
    "automated_candidate_search_run",
    "automated_candidate_trial_binding",
    "mlflow_mirror_workspace",
    "mlflow_mirror_record",
    "candidate_generation_decision",
}


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _revision(connection: sa.Connection) -> str:
    return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _proposal(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "model_family": "logistic-regression-probability-v1",
        "calibration": "PLATT",
        "regularization_c": "1.0",
        "penalty": "l2",
        "class_weight": "NONE",
        "logistic_max_iter": 400,
        "learning_rate": "0.1",
        "histogram_max_iter": 80,
        "max_leaf_nodes": 15,
        "max_bins": 255,
        "min_samples_leaf": 10,
        "l2_regularization": "0.5",
    }
    value.update(overrides)
    return value


def _context(engine: Engine, root: Path):
    strategy_store = PostgresStrategyLabStore(engine)
    strategy_service = StrategyLabService(
        strategy_store,
        LocalStrategyLabArtifactStore(root / "models"),
    )
    dataset = strategy_service.build_dataset(
        adapter=DeterministicStrategyLabDemoAdapter(),
        name="bounded candidate persistence",
        cutoff_at=NOW,
        idempotency_key="candidate-persistence-dataset",
    )
    assert dataset.canonical_result_hash is not None
    space = initial_strategy_lab_search_space()
    research_repository = PostgresResearchIntegrityRepository(engine)
    family = OpenResearchExperimentFamily(research_repository).execute(
        name="Bounded candidate generation persistence",
        vertical="SECURITIES",
        research_family="STRATEGY_LAB_CANDIDATE_GENERATION",
        research_question="Can managed bounded model variants improve classification?",
        target_label_identity=dataset.label_definition.canonical_hash,
        benchmark_identities=("CONSTANT_PREVALENCE",),
        data_scope_identities=(dataset.dataset_id,),
        data_scope_hashes=(dataset.canonical_result_hash,),
        universe_hash=dataset.source_identity,
        period_start=str(dataset.feature_first_at),
        period_end=str(dataset.feature_last_at),
        cutoff_at=NOW,
        feature_schema_hashes=(dataset.schema.canonical_hash,),
        permitted_families=tuple(item.value for item in ModelFamily),
        parameter_space={"parameters": space.payload},
        maximum_trials=6,
        maximum_feature_variants=1,
        maximum_model_variants=6,
        maximum_simulator_variants=1,
        evaluation_policy_id="RESEARCH_SELECTION_INTEGRITY_POLICY_V1",
        evaluation_policy_payload={"grouped_walk_forward": True},
        cost_execution_assumptions={"authority": "NONE"},
        frequency=ResearchFrequency.DAILY,
        operator_action_id="operator-candidate-persistence",
        operator_reason="Focused candidate-generation persistence proof",
        idempotency_key="candidate-persistence-family",
        created_at=NOW,
    )
    candidate_repository = PostgresCandidateGenerationRepository(engine)
    plan = CreateAutomatedCandidateSearchPlan(candidate_repository).execute(
        experiment=family,
        existing_experiment_trial_count=0,
        dataset=dataset,
        search_space=space,
        display_name="Focused bounded search",
        permitted_model_families=tuple(ModelFamily),
        objective_identity=ObjectiveIdentity.BRIER,
        maximum_trials=3,
        maximum_failed_trials=1,
        wall_clock_budget_seconds=120,
        per_trial_budget_seconds=60,
        artifact_byte_budget=10_000_000,
        random_seed=17,
        operator_action_id="operator-candidate-plan",
        operator_reason="Focused PostgreSQL lifecycle",
        idempotency_key="candidate-persistence-plan",
        created_at=NOW,
    )
    return (
        strategy_service,
        research_repository,
        candidate_repository,
        dataset,
        family,
        plan,
        space,
    )


def test_0029_empty_downgrade_and_reupgrade(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        assert _revision(connection) == REVISION_0031
        assert TABLES.issubset(inspect(connection).get_table_names())
        command.downgrade(_config(connection), REVISION_0028)
        connection.commit()
        assert _revision(connection) == REVISION_0028
        assert TABLES.isdisjoint(inspect(connection).get_table_names())
        command.upgrade(_config(connection), REVISION_0029)
        connection.commit()
        assert _revision(connection) == REVISION_0029
        assert TABLES.issubset(inspect(connection).get_table_names())


def test_postgres_search_retains_every_trial_and_recovers_after_restart(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    (
        strategy_service,
        research_repository,
        repository,
        _dataset,
        family,
        plan,
        space,
    ) = _context(postgres_engine, tmp_path)
    workspace = create_local_mlflow_workspace(mlflow_version="3.14.0", created_at=NOW)
    repository.save_workspace(workspace)
    mirror = LocalMLflowMirrorAdapter(
        root=tmp_path / "mlflow",
        fail_once_for_trial_numbers=frozenset({0}),
    )
    optimizer = DeterministicFixtureCandidateSearchAdapter(
        (
            _proposal(),
            _proposal(
                model_family="histogram-gradient-boosting-probability-v1",
                calibration="NONE",
            ),
            _proposal(calibration="UNSUPPORTED"),
        )
    )
    run, decision, bindings = RunAutomatedCandidateSearch(
        repository=repository,
        research_ledger=ResearchTrialLedger(research_repository),
        research_integrity=ResearchIntegrityService(
            research_repository,
            LocalResearchReturnArtifactStore(tmp_path / "returns"),
        ),
        optimizer=optimizer,
        executor=StrategyLabCandidateTrialExecutor(strategy_service),
        mirror=mirror,
    ).execute(
        plan=plan,
        experiment=family,
        search_space=space,
        workspace=workspace,
        requested_trials=3,
        idempotency_key="candidate-persistence-run",
        started_at=NOW,
    )

    assert run.reserved_trial_count == 3
    assert run.completed_trial_count == 2
    assert run.integrity_rejected_trial_count == 1
    assert len(bindings) == 3
    assert tuple(item.objective_state.value for item in bindings) == (
        "COMPLETED",
        "COMPLETED",
        "REJECTED_INTEGRITY",
    )
    assert decision.raw_best_trial_id is not None
    assert decision.selected_candidate_trial_id is None
    mirror_records = repository.mirror_records(family.experiment_id)
    assert mirror_records
    assert any(item.retry_count == 1 for item in mirror_records)
    reconciled = reconcile_mlflow_mirror(
        repository=repository,
        mirror=mirror,
        workspace=workspace,
        experiment=family,
        research_trials=research_repository.trials(family.experiment_id),
    )
    assert all(item.state.value == "MIRROR_CONFORMANT" for item in reconciled)

    restarted = PostgresCandidateGenerationRepository(postgres_engine)
    assert restarted.plan(plan.search_plan_id) is not None
    assert restarted.run(run.search_run_id) == run
    assert restarted.bindings(run.search_run_id) == bindings
    assert restarted.decision(run.search_run_id) == decision
    retained_plan = repository.plan(plan.search_plan_id)
    assert retained_plan is not None
    replay = RunAutomatedCandidateSearch(
        repository=restarted,
        research_ledger=ResearchTrialLedger(research_repository),
        research_integrity=ResearchIntegrityService(
            research_repository,
            LocalResearchReturnArtifactStore(tmp_path / "returns"),
        ),
        optimizer=optimizer,
        executor=StrategyLabCandidateTrialExecutor(strategy_service),
        mirror=LocalMLflowMirrorAdapter(root=tmp_path / "mlflow"),
    ).execute(
        plan=retained_plan,
        experiment=family,
        search_space=space,
        workspace=workspace,
        requested_trials=3,
        idempotency_key="candidate-persistence-run",
        started_at=NOW,
    )
    assert replay == (run, decision, bindings)


def test_ready_plan_and_decision_are_immutable_and_numeric_is_exact(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    *_unused, repository, _dataset, _family, plan, _space = _context(postgres_engine, tmp_path)
    with (
        pytest.raises(
            sa.exc.ProgrammingError,
            match="candidate search plan scope and budgets are immutable",
        ),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE automated_candidate_search_plan "
                "SET maximum_trials = maximum_trials + 1 "
                "WHERE search_plan_id = :search_plan_id"
            ),
            {"search_plan_id": plan.search_plan_id},
        )
    assert repository.plan(plan.search_plan_id) == plan
    numeric_columns = {
        (table, str(column["name"]))
        for table in TABLES
        for column in inspect(postgres_engine).get_columns(table)
        if isinstance(column["type"], sa.Numeric)
    }
    assert ("automated_candidate_search_run", "elapsed_seconds") in numeric_columns
    assert ("automated_candidate_trial_binding", "objective_value") in numeric_columns

    with pytest.raises(CandidateGenerationConflict, match="SEARCH_PLAN_IDEMPOTENCY_CONFLICT"):
        CreateAutomatedCandidateSearchPlan(repository).execute(
            experiment=_family,
            existing_experiment_trial_count=0,
            dataset=_dataset,
            search_space=_space,
            display_name="Conflicting replay",
            permitted_model_families=tuple(ModelFamily),
            objective_identity=ObjectiveIdentity.BRIER,
            maximum_trials=3,
            maximum_failed_trials=1,
            wall_clock_budget_seconds=120,
            per_trial_budget_seconds=60,
            artifact_byte_budget=10_000_000,
            random_seed=17,
            operator_action_id="operator-candidate-plan",
            operator_reason="Focused PostgreSQL lifecycle",
            idempotency_key="candidate-persistence-plan",
            created_at=NOW,
        )


def test_retained_0029_evidence_blocks_downgrade_before_ddl(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), REVISION_0029)
        connection.commit()
        assert _revision(connection) == REVISION_0029
    repository = PostgresCandidateGenerationRepository(postgres_engine)
    workspace = create_local_mlflow_workspace(mlflow_version="3.14.0", created_at=NOW)
    repository.save_workspace(workspace)
    before_tables = set(inspect(postgres_engine).get_table_names())
    with postgres_engine.connect() as connection:
        with pytest.raises(
            sa.exc.ProgrammingError,
            match="cannot downgrade 0029 with retained candidate search",
        ):
            command.downgrade(_config(connection), REVISION_0028)
        connection.rollback()
        assert _revision(connection) == REVISION_0029
        assert set(inspect(connection).get_table_names()) == before_tables
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM mlflow_mirror_workspace "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": workspace.workspace_id},
            )
            == 1
        )
