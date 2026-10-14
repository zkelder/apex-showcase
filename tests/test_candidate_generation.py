from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from mlflow.tracking import MlflowClient

from apex.adapters.candidate_generation_tools import (
    DeterministicFixtureCandidateSearchAdapter,
    LocalMLflowMirrorAdapter,
    OptunaCandidateSearchAdapter,
)
from apex.adapters.research_integrity_artifacts import (
    LocalResearchReturnArtifactStore,
)
from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.adapters.strategy_lab_synthetic import (
    DeterministicStrategyLabDemoAdapter,
)
from apex.application.candidate_generation import (
    AutomatedCandidateSearchRun,
    CancelAutomatedCandidateSearch,
    CandidateGenerationApplicationError,
    CandidateGenerationRecommendation,
    CreateAutomatedCandidateSearchPlan,
    InMemoryCandidateGenerationRepository,
    MirrorRecordState,
    ObjectiveState,
    RunAutomatedCandidateSearch,
    SearchPlanState,
    SearchRunState,
    StrategyLabCandidateTrialExecutor,
    create_local_mlflow_workspace,
    reconcile_mlflow_mirror,
)
from apex.application.research_integrity import (
    InMemoryResearchIntegrityRepository,
    OpenResearchExperimentFamily,
    ResearchIntegrityService,
    ResearchTrialLedger,
)
from apex.application.strategy_lab import (
    InMemoryStrategyLabRepository,
    ModelFamily,
    StrategyLabService,
)
from apex.domain.candidate_generation import (
    CandidateGenerationError,
    ObjectiveIdentity,
    initial_strategy_lab_search_space,
)
from apex.domain.research_integrity import ResearchFrequency

NOW = datetime(2026, 7, 27, 16, tzinfo=UTC)


def _proposal(*, family: str = ModelFamily.LOGISTIC_REGRESSION.value):
    return {
        "model_family": family,
        "calibration": "NONE",
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


def _context(tmp_path: Path, *, maximum_trials: int = 4):
    lab_repository = InMemoryStrategyLabRepository()
    lab_service = StrategyLabService(
        lab_repository, LocalStrategyLabArtifactStore(tmp_path / "strategy-lab")
    )
    adapter = DeterministicStrategyLabDemoAdapter()
    dataset = lab_service.build_dataset(
        adapter=adapter,
        name="Bounded Candidate Search Dataset",
        cutoff_at=datetime(2026, 3, 1, tzinfo=UTC),
        idempotency_key="candidate-search-dataset",
    )
    space = initial_strategy_lab_search_space()
    research_repository = InMemoryResearchIntegrityRepository()
    family = OpenResearchExperimentFamily(research_repository).execute(
        name="Bounded candidate family",
        vertical="SECURITIES",
        research_family="STRATEGY_LAB_BOUNDED_SEARCH",
        research_question="Can a predeclared bounded classifier improve calibration?",
        target_label_identity=dataset.label_definition.canonical_hash,
        benchmark_identities=("MARKET_CONSENSUS",),
        data_scope_identities=(dataset.dataset_id,),
        data_scope_hashes=(dataset.canonical_result_hash,),
        universe_hash="b" * 64,
        period_start="2026-02-01",
        period_end="2026-03-01",
        cutoff_at=NOW,
        feature_schema_hashes=(dataset.schema.canonical_hash,),
        permitted_families=(
            ModelFamily.LOGISTIC_REGRESSION.value,
            ModelFamily.HISTOGRAM_GRADIENT_BOOSTING.value,
        ),
        parameter_space={"parameters": space.payload},
        maximum_trials=maximum_trials,
        maximum_feature_variants=1,
        maximum_model_variants=2,
        maximum_simulator_variants=1,
        evaluation_policy_id="RESEARCH_SELECTION_INTEGRITY_POLICY_V1",
        evaluation_policy_payload={"minimum_psr": "0.95"},
        cost_execution_assumptions={"fees": "NONE_CLASSIFICATION"},
        frequency=ResearchFrequency.DAILY,
        operator_action_id="operator-bounded-search",
        operator_reason="Focused bounded search proof",
        idempotency_key="candidate-family",
        created_at=NOW,
    )
    return (
        lab_service,
        dataset,
        space,
        research_repository,
        family,
    )


def _plan(
    tmp_path: Path,
    *,
    maximum_trials: int = 4,
    artifact_byte_budget: int = 20_000_000,
):
    lab_service, dataset, space, research_repository, family = _context(
        tmp_path, maximum_trials=maximum_trials
    )
    repository = InMemoryCandidateGenerationRepository()
    plan = CreateAutomatedCandidateSearchPlan(repository).execute(
        experiment=family,
        existing_experiment_trial_count=0,
        dataset=dataset,
        search_space=space,
        display_name="Seeded bounded classifier search",
        permitted_model_families=(
            ModelFamily.LOGISTIC_REGRESSION,
            ModelFamily.HISTOGRAM_GRADIENT_BOOSTING,
        ),
        objective_identity=ObjectiveIdentity.BRIER,
        maximum_trials=maximum_trials,
        maximum_failed_trials=2,
        wall_clock_budget_seconds=300,
        per_trial_budget_seconds=60,
        artifact_byte_budget=artifact_byte_budget,
        random_seed=17,
        operator_action_id="operator-create-search",
        operator_reason="Predeclared focused research",
        idempotency_key="search-plan",
        created_at=NOW + timedelta(seconds=1),
    )
    return (
        lab_service,
        dataset,
        space,
        research_repository,
        family,
        repository,
        plan,
    )


def test_finite_search_space_rejects_missing_and_off_step_proposals() -> None:
    space = initial_strategy_lab_search_space()
    assert space.theoretical_combinations > 0
    assert len(space.canonical_hash) == 64
    normalized = space.validate_proposal(_proposal())
    assert normalized["regularization_c"] == "1.0"
    with pytest.raises(CandidateGenerationError, match="PROPOSAL_PARAMETER_SET_MISMATCH"):
        space.validate_proposal({"model_family": ModelFamily.LOGISTIC_REGRESSION.value})
    invalid = _proposal()
    invalid["regularization_c"] = "1.05"
    with pytest.raises(CandidateGenerationError, match="PROPOSAL_OFF_STEP"):
        space.validate_proposal(invalid)


def test_plan_is_bounded_subset_and_replay_is_exact(tmp_path: Path) -> None:
    _, dataset, space, _, family, repository, plan = _plan(tmp_path)
    assert plan.concurrency_limit == 1
    assert plan.theoretical_combinations == space.theoretical_combinations
    replay = CreateAutomatedCandidateSearchPlan(repository).execute(
        experiment=family,
        existing_experiment_trial_count=0,
        dataset=dataset,
        search_space=space,
        display_name="Seeded bounded classifier search",
        permitted_model_families=(
            ModelFamily.LOGISTIC_REGRESSION,
            ModelFamily.HISTOGRAM_GRADIENT_BOOSTING,
        ),
        objective_identity=ObjectiveIdentity.BRIER,
        maximum_trials=4,
        maximum_failed_trials=2,
        wall_clock_budget_seconds=300,
        per_trial_budget_seconds=60,
        artifact_byte_budget=20_000_000,
        random_seed=17,
        operator_action_id="operator-create-search",
        operator_reason="Predeclared focused research",
        idempotency_key="search-plan",
        created_at=NOW + timedelta(seconds=1),
    )
    assert replay == plan
    with pytest.raises(
        CandidateGenerationApplicationError,
        match="EXPERIMENT_TRIAL_BUDGET_EXCEEDED",
    ):
        CreateAutomatedCandidateSearchPlan(repository).execute(
            experiment=family,
            existing_experiment_trial_count=3,
            dataset=dataset,
            search_space=space,
            display_name="Exceeds remaining family budget",
            permitted_model_families=(ModelFamily.LOGISTIC_REGRESSION,),
            objective_identity=ObjectiveIdentity.BRIER,
            maximum_trials=2,
            maximum_failed_trials=1,
            wall_clock_budget_seconds=300,
            per_trial_budget_seconds=60,
            artifact_byte_budget=20_000_000,
            random_seed=17,
            operator_action_id="operator-invalid",
            operator_reason="Must fail",
            idempotency_key="invalid-plan",
            created_at=NOW + timedelta(seconds=2),
        )


def test_cost_adjusted_objective_requires_compatible_return_evidence(
    tmp_path: Path,
) -> None:
    _, dataset, space, _, family, repository, _plan_value = _plan(tmp_path)
    with pytest.raises(
        CandidateGenerationApplicationError,
        match="COST_ADJUSTED_OBJECTIVE_REQUIRES_SECURITIES_RETURN_EVIDENCE",
    ):
        CreateAutomatedCandidateSearchPlan(repository).execute(
            experiment=family,
            existing_experiment_trial_count=0,
            dataset=dataset,
            search_space=space,
            display_name="Unsupported cost-adjusted search",
            permitted_model_families=tuple(ModelFamily),
            objective_identity=ObjectiveIdentity.COST_ADJUSTED_EXCESS_RETURN,
            maximum_trials=4,
            maximum_failed_trials=1,
            wall_clock_budget_seconds=300,
            per_trial_budget_seconds=60,
            artifact_byte_budget=20_000_000,
            random_seed=17,
            operator_action_id="operator-cost-search",
            operator_reason="Must fail without compatible Securities returns",
            idempotency_key="cost-search-plan",
            created_at=NOW + timedelta(seconds=3),
        )


def test_cancellation_updates_run_and_plan_without_discarding_evidence(
    tmp_path: Path,
) -> None:
    _, _, _, _, _, repository, plan = _plan(tmp_path)
    payload_hash = "a" * 64
    run = repository.save_run(
        AutomatedCandidateSearchRun(
            search_run_id="candidate-search-run-cancel",
            search_plan_id=plan.search_plan_id,
            search_plan_hash=plan.canonical_input_hash,
            experiment_id=plan.experiment_id,
            experiment_hash=plan.experiment_hash,
            optuna_study_identity="fixture-study",
            optuna_version="FIXTURE",
            mlflow_experiment_identity=None,
            requested_trial_count=2,
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
            started_at=NOW,
            completed_at=None,
            idempotency_key="cancel-search-run",
            canonical_input_hash=payload_hash,
            canonical_result_hash=None,
        )
    )
    repository.update_plan(replace(plan, state=SearchPlanState.RUNNING))
    canceled = CancelAutomatedCandidateSearch(repository).execute(
        search_run_id=run.search_run_id,
        reason="operator bounded stop",
        canceled_at=NOW + timedelta(minutes=1),
    )
    assert canceled.state is SearchRunState.CANCELED
    retained_plan = repository.plan(plan.search_plan_id)
    assert retained_plan is not None
    assert retained_plan.state is SearchPlanState.CANCELED
    assert repository.bindings(run.search_run_id) == ()


def test_seeded_optuna_ask_tell_is_local_and_recoverable(tmp_path: Path) -> None:
    space = initial_strategy_lab_search_space()
    adapter = OptunaCandidateSearchAdapter(
        plan_hash=space.canonical_hash, seed=11, root=tmp_path / "optuna"
    )
    for _ in range(20):
        proposal = adapter.ask(space)
        assert space.validate_proposal(proposal.parameters)
        adapter.tell(proposal, state=ObjectiveState.COMPLETED, objective_value="0.25")
    restarted = OptunaCandidateSearchAdapter(
        plan_hash=space.canonical_hash, seed=11, root=tmp_path / "optuna"
    )
    assert restarted.study_identity == adapter.study_identity
    assert not restarted.reconcile(())


def test_artifact_budget_stops_after_retaining_the_atomic_trial(
    tmp_path: Path,
) -> None:
    (
        lab_service,
        _dataset,
        space,
        research_repository,
        family,
        repository,
        plan,
    ) = _plan(tmp_path, maximum_trials=3, artifact_byte_budget=1)
    optimizer = DeterministicFixtureCandidateSearchAdapter((_proposal(), _proposal(), _proposal()))
    mirror = LocalMLflowMirrorAdapter(root=tmp_path / "mlflow-budget")
    workspace = repository.save_workspace(
        create_local_mlflow_workspace(
            mlflow_version=mirror.runtime_version,
            created_at=NOW,
        )
    )
    run, decision, bindings = RunAutomatedCandidateSearch(
        repository=repository,
        research_ledger=ResearchTrialLedger(research_repository),
        research_integrity=ResearchIntegrityService(
            research_repository,
            LocalResearchReturnArtifactStore(tmp_path / "research-budget"),
        ),
        optimizer=optimizer,
        executor=StrategyLabCandidateTrialExecutor(lab_service),
        mirror=mirror,
    ).execute(
        plan=plan,
        experiment=family,
        search_space=space,
        workspace=workspace,
        requested_trials=3,
        idempotency_key="artifact-budget-run",
        started_at=NOW,
    )
    assert run.state is SearchRunState.PARTIAL
    assert run.reserved_trial_count == 1
    assert run.artifact_bytes_written > plan.artifact_byte_budget
    assert bindings[0].objective_state is ObjectiveState.CANCELED
    assert bindings[0].failure_reason == "ARTIFACT_BYTE_BUDGET_EXCEEDED"
    assert decision.selected_candidate_trial_id is None
    assert len(research_repository.trials(family.experiment_id)) == 1


def test_search_retains_completed_failure_and_invalid_proposal_with_mlflow(
    tmp_path: Path,
) -> None:
    (
        lab_service,
        _dataset,
        space,
        research_repository,
        family,
        repository,
        plan,
    ) = _plan(tmp_path, maximum_trials=3)
    valid = _proposal()
    invalid = _proposal()
    invalid.pop("penalty")
    optimizer = DeterministicFixtureCandidateSearchAdapter(
        (valid, _proposal(family=ModelFamily.HISTOGRAM_GRADIENT_BOOSTING.value), invalid)
    )

    class OneFailureExecutor:
        def __init__(self) -> None:
            self._delegate = StrategyLabCandidateTrialExecutor(lab_service)

        def execute(self, *, plan, proposal, research_trial):
            if research_trial.ordinal == 2:
                raise RuntimeError("controlled training failure")
            return self._delegate.execute(
                plan=plan, proposal=proposal, research_trial=research_trial
            )

    mirror = LocalMLflowMirrorAdapter(
        root=tmp_path / "mlflow", fail_once_for_trial_numbers=frozenset({0})
    )
    workspace = repository.save_workspace(
        create_local_mlflow_workspace(mlflow_version=mirror.runtime_version, created_at=NOW)
    )
    service = RunAutomatedCandidateSearch(
        repository=repository,
        research_ledger=ResearchTrialLedger(research_repository),
        research_integrity=ResearchIntegrityService(
            research_repository,
            LocalResearchReturnArtifactStore(tmp_path / "research-returns"),
        ),
        optimizer=optimizer,
        executor=OneFailureExecutor(),
        mirror=mirror,
    )
    run, decision, bindings = service.execute(
        plan=plan,
        experiment=family,
        search_space=space,
        workspace=workspace,
        requested_trials=3,
        idempotency_key="search-run",
        started_at=NOW + timedelta(minutes=1),
    )
    assert run.state is SearchRunState.COMPLETED, (
        run.reserved_trial_count,
        run.completed_trial_count,
        run.failed_trial_count,
        run.integrity_rejected_trial_count,
        tuple((item.objective_state, item.failure_reason) for item in bindings),
    )
    assert run.reserved_trial_count == 3
    assert run.completed_trial_count == 1
    assert run.failed_trial_count == 1
    assert run.integrity_rejected_trial_count == 1
    assert tuple(item.objective_state for item in bindings) == (
        ObjectiveState.COMPLETED,
        ObjectiveState.FAILED,
        ObjectiveState.REJECTED_INTEGRITY,
    )
    assert len(research_repository.trials(family.experiment_id)) == 3
    assert decision.raw_best_trial_id == bindings[0].research_trial_id
    assert decision.selected_candidate_trial_id is None
    assert decision.recommendation in (
        CandidateGenerationRecommendation.NO_ELIGIBLE_CANDIDATE,
        CandidateGenerationRecommendation.REQUIRE_MORE_EVIDENCE,
    )
    mirrored_records = repository.mirror_records(family.experiment_id)
    retryable = replace(
        mirrored_records[0],
        state=MirrorRecordState.FAILED_RETRYABLE,
        failure_code="CONTROLLED_RECONCILIATION_RETRY",
        failure_message="controlled focused proof",
    )
    repository.save_mirror_record(retryable)
    reconciled = reconcile_mlflow_mirror(
        repository=repository,
        mirror=mirror,
        workspace=workspace,
        experiment=family,
        research_trials=research_repository.trials(family.experiment_id),
    )
    assert reconciled
    assert all(item.state.value == "MIRROR_CONFORMANT" for item in reconciled)
    retried = repository.mirror_records(family.experiment_id)
    retried_record = next(
        item for item in retried if item.mirror_record_id == retryable.mirror_record_id
    )
    assert retried_record.retry_count == retryable.retry_count + 1
    deleted = retried_record
    assert deleted.mlflow_run_id is not None
    MlflowClient(tracking_uri=mirror.tracking_uri).delete_run(deleted.mlflow_run_id)
    recovered = reconcile_mlflow_mirror(
        repository=repository,
        mirror=mirror,
        workspace=workspace,
        experiment=family,
        research_trials=research_repository.trials(family.experiment_id),
    )
    assert all(item.state.value == "MIRROR_CONFORMANT" for item in recovered)
    rebuilt = repository.mirror_records(family.experiment_id)[0]
    assert rebuilt.mlflow_run_id != deleted.mlflow_run_id
    replay = service.execute(
        plan=plan,
        experiment=family,
        search_space=space,
        workspace=workspace,
        requested_trials=3,
        idempotency_key="search-run",
        started_at=NOW + timedelta(minutes=1),
    )
    assert replay[0] == run
    assert not hasattr(decision, "deployment_id")
    assert not hasattr(decision, "capital")
