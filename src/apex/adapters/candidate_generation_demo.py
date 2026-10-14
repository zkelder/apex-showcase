"""Deterministic production-service demo for bounded candidate generation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

from apex.adapters.candidate_generation_operations import (
    AUTOMATED_SEARCH_WARNINGS,
    CandidateGenerationRuntime,
)
from apex.adapters.candidate_generation_tools import (
    LocalMLflowMirrorAdapter,
    OptunaCandidateSearchAdapter,
)
from apex.adapters.strategy_lab_synthetic import DeterministicStrategyLabDemoAdapter
from apex.application.candidate_generation import (
    AutomatedCandidateSearchPlan,
    AutomatedCandidateSearchRun,
    AutomatedCandidateTrialBinding,
    CancelAutomatedCandidateSearch,
    CandidateGenerationConflict,
    CreateAutomatedCandidateSearchPlan,
    ObjectiveState,
    OptimizerProposal,
    RecoverAutomatedCandidateSearch,
    RunAutomatedCandidateSearch,
    SearchPlanState,
    SearchRunState,
    StrategyLabCandidateTrialExecutor,
    TrialExecutionResult,
    create_local_mlflow_workspace,
    reconcile_mlflow_mirror,
)
from apex.application.research_integrity import (
    OpenResearchExperimentFamily,
    ResearchExperimentFamily,
    ResearchTrialAttempt,
    ResearchTrialLedger,
    ResearchTrialSourceType,
    ResearchTrialState,
)
from apex.application.strategy_lab import (
    ModelFamily,
    StrategyLabDatasetSnapshot,
)
from apex.domain.candidate_generation import (
    OBJECTIVE_POLICY_ID,
    BoundedSearchSpace,
    ObjectiveIdentity,
    initial_strategy_lab_search_space,
)
from apex.domain.research_integrity import ResearchFrequency
from apex.domain.values import content_digest, deterministic_id


class _DemoOptimizer:
    """Use real Optuna proposals while injecting one explicitly invalid payload."""

    def __init__(self, delegate: OptunaCandidateSearchAdapter, invalid_number: int) -> None:
        self._delegate = delegate
        self.runtime_version = delegate.runtime_version
        self.study_identity = delegate.study_identity
        self._invalid_number = invalid_number

    def ask(self, search_space: BoundedSearchSpace) -> OptimizerProposal:
        proposal = self._delegate.ask(search_space)
        if proposal.trial_number == self._invalid_number:
            return replace(
                proposal,
                parameters={**proposal.parameters, "calibration": "INVALID_DEMO_VALUE"},
            )
        return proposal

    def tell(
        self,
        proposal: OptimizerProposal,
        *,
        state: ObjectiveState,
        objective_value: Decimal | None,
    ) -> None:
        self._delegate.tell(
            proposal,
            state=state,
            objective_value=objective_value,
        )

    def reconcile(self, bindings: Sequence[AutomatedCandidateTrialBinding]) -> tuple[str, ...]:
        return self._delegate.reconcile(bindings)


class _DemoExecutor:
    """Inject one controlled failure; every other attempt uses Strategy Lab."""

    def __init__(
        self,
        delegate: StrategyLabCandidateTrialExecutor,
        failed_ordinal: int,
    ) -> None:
        self._delegate = delegate
        self._failed_ordinal = failed_ordinal

    def execute(
        self,
        *,
        plan: AutomatedCandidateSearchPlan,
        proposal: dict[str, str],
        research_trial: ResearchTrialAttempt,
    ) -> TrialExecutionResult:
        if research_trial.ordinal == self._failed_ordinal:
            raise RuntimeError("CONTROLLED_DEMO_TRAINING_FAILURE")
        return self._delegate.execute(
            plan=plan,
            proposal=proposal,
            research_trial=research_trial,
        )


def _family(
    runtime: CandidateGenerationRuntime,
    *,
    dataset: StrategyLabDatasetSnapshot,
    suffix: str,
    maximum_trials: int,
) -> ResearchExperimentFamily:
    assert dataset.canonical_result_hash is not None
    space = initial_strategy_lab_search_space()
    return OpenResearchExperimentFamily(runtime.research_repository).execute(
        name=f"Bounded automated candidate generation {suffix}",
        vertical="SECURITIES",
        research_family="STRATEGY_LAB_CANDIDATE_GENERATION",
        research_question="Can a frozen bounded model search improve classification?",
        target_label_identity=dataset.label_definition.canonical_hash,
        benchmark_identities=("CONSTANT_PREVALENCE",),
        data_scope_identities=(dataset.dataset_id,),
        data_scope_hashes=(dataset.canonical_result_hash,),
        universe_hash=dataset.source_identity,
        period_start=str(dataset.feature_first_at),
        period_end=str(dataset.feature_last_at),
        cutoff_at=dataset.cutoff_at,
        feature_schema_hashes=(dataset.schema.canonical_hash,),
        permitted_families=tuple(item.value for item in ModelFamily),
        parameter_space={"parameters": space.payload},
        maximum_trials=maximum_trials,
        maximum_feature_variants=1,
        maximum_model_variants=maximum_trials,
        maximum_simulator_variants=1,
        evaluation_policy_id="RESEARCH_SELECTION_INTEGRITY_POLICY_V1",
        evaluation_policy_payload={"grouped_chronological_walk_forward": True},
        cost_execution_assumptions={"authority": "NONE", "execution": "ABSENT"},
        frequency=ResearchFrequency.DAILY,
        operator_action_id=f"demo-candidate-family-{suffix}",
        operator_reason="Deterministic bounded candidate-generation demonstration",
        idempotency_key=f"candidate-demo-family-{suffix}",
        created_at=datetime.now(tz=UTC),
    )


def _plan(
    runtime: CandidateGenerationRuntime,
    *,
    family: ResearchExperimentFamily,
    dataset: StrategyLabDatasetSnapshot,
    suffix: str,
    maximum_trials: int,
) -> AutomatedCandidateSearchPlan:
    return CreateAutomatedCandidateSearchPlan(runtime.repository).execute(
        experiment=family,
        existing_experiment_trial_count=len(
            runtime.research_repository.trials(family.experiment_id)
        ),
        dataset=dataset,
        search_space=initial_strategy_lab_search_space(),
        display_name=f"Seeded Optuna bounded search {suffix}",
        permitted_model_families=tuple(ModelFamily),
        objective_identity=ObjectiveIdentity.BRIER,
        maximum_trials=maximum_trials,
        maximum_failed_trials=min(8, maximum_trials - 1),
        wall_clock_budget_seconds=900,
        per_trial_budget_seconds=120,
        artifact_byte_budget=250_000_000,
        random_seed=20260727,
        operator_action_id=f"demo-candidate-plan-{suffix}",
        operator_reason="Bounded seeded candidate generation with no authority",
        idempotency_key=f"candidate-demo-plan-{suffix}",
        created_at=datetime.now(tz=UTC),
    )


def _financial_counts(runtime: CandidateGenerationRuntime) -> dict[str, int]:
    tables = (
        "opportunity_proposal",
        "risk_reservation",
        "order",
        "position",
        "settlement_fact",
        "ledger_journal",
        "strategy_lab_model_deployment",
        "strategy_lab_champion_assignment",
    )
    with runtime.engine.connect() as connection:
        return {
            table: int(connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) or 0)
            for table in tables
        }


def _finalize_retained_v1_demo_evidence(
    runtime: CandidateGenerationRuntime,
) -> dict[str, object]:
    """Terminalize evidence retained by the pre-grid-normalization demo failure."""

    finalized_trials: list[str] = []
    main_family = runtime.research_repository.family_by_idempotency("candidate-demo-family-main")
    if main_family is not None:
        for existing_trial in runtime.research_repository.trials(main_family.experiment_id):
            if (
                existing_trial.source_identity == "forbidden-budget-overrun"
                and existing_trial.state is ResearchTrialState.REQUESTED
            ):
                finalized = ResearchTrialLedger(runtime.research_repository).finish(
                    existing_trial.trial_id,
                    state=ResearchTrialState.CANCELED,
                    completed_at=datetime.now(tz=UTC),
                    result_identity=None,
                    result_hash=None,
                    failure_reason="DEMO_BUDGET_PROBE_AFTER_EARLY_STOP",
                    integrity_blocked=True,
                )
                finalized_trials.append(finalized.trial_id)

    plan = runtime.repository.plan_by_idempotency("candidate-demo-plan-recovery")
    if plan is None:
        return {
            "finalized_trial_ids": tuple(finalized_trials),
            "recovery_run_state": None,
        }
    run = runtime.repository.run_by_idempotency(plan.search_plan_id, "candidate-demo-recovery-run")
    if run is None:
        return {
            "finalized_trial_ids": tuple(finalized_trials),
            "recovery_run_state": None,
        }
    bindings = runtime.repository.bindings(run.search_run_id)
    if len(bindings) != 1:
        raise RuntimeError("legacy demo recovery binding count is not exactly one")
    binding = bindings[0]
    recovery_trial = runtime.research_repository.trial(binding.research_trial_id)
    if recovery_trial is None:
        raise RuntimeError("legacy demo recovery trial unexpectedly disappeared")
    if recovery_trial.state is ResearchTrialState.REQUESTED:
        recovery_trial = ResearchTrialLedger(runtime.research_repository).finish(
            recovery_trial.trial_id,
            state=ResearchTrialState.REJECTED_INTEGRITY,
            completed_at=datetime.now(tz=UTC),
            result_identity=None,
            result_hash=None,
            failure_reason="LEGACY_OPTUNA_DECIMAL_PROPOSAL_OFF_EXACT_GRID",
            integrity_blocked=True,
        )
        finalized_trials.append(recovery_trial.trial_id)
    if binding.objective_state in (ObjectiveState.PROPOSED, ObjectiveState.TRAINING):
        binding = runtime.repository.update_binding(
            replace(
                binding,
                objective_state=ObjectiveState.REJECTED_INTEGRITY,
                failure_reason="LEGACY_OPTUNA_DECIMAL_PROPOSAL_OFF_EXACT_GRID",
                canonical_result_hash=content_digest(
                    {
                        "state": ObjectiveState.REJECTED_INTEGRITY.value,
                        "failure": "LEGACY_OPTUNA_DECIMAL_PROPOSAL_OFF_EXACT_GRID",
                    }
                ),
            )
        )
    current = runtime.repository.run(run.search_run_id)
    if current is None:
        raise RuntimeError("legacy demo recovery run unexpectedly disappeared")
    if current.state in (SearchRunState.REQUESTED, SearchRunState.RUNNING):
        current = runtime.repository.update_run(
            replace(
                current,
                reserved_trial_count=max(current.reserved_trial_count, 1),
                integrity_rejected_trial_count=max(current.integrity_rejected_trial_count, 1),
            )
        )
        RecoverAutomatedCandidateSearch(
            runtime.repository,
            OptunaCandidateSearchAdapter(
                plan_hash=plan.canonical_input_hash,
                seed=plan.random_seed,
                direction=plan.objective_direction,
                root=runtime.local_root / "optuna",
            ),
        ).execute(current.search_run_id)
        current = CancelAutomatedCandidateSearch(runtime.repository).execute(
            search_run_id=current.search_run_id,
            reason="DEMO_RETAINED_V1_INTEGRITY_FAILURE",
            canceled_at=datetime.now(tz=UTC),
        )
    return {
        "finalized_trial_ids": tuple(finalized_trials),
        "recovery_run_state": current.state,
    }


def _interruption_and_cancellation(
    runtime: CandidateGenerationRuntime,
    *,
    dataset: StrategyLabDatasetSnapshot,
) -> dict[str, object]:
    family = _family(runtime, dataset=dataset, suffix="recovery-v2", maximum_trials=3)
    plan = _plan(
        runtime,
        family=family,
        dataset=dataset,
        suffix="recovery-v2",
        maximum_trials=3,
    )
    optimizer = OptunaCandidateSearchAdapter(
        plan_hash=plan.canonical_input_hash,
        seed=plan.random_seed,
        direction=plan.objective_direction,
        root=runtime.local_root / "optuna",
    )
    run_payload = {
        "plan": plan.search_plan_id,
        "family": family.experiment_id,
        "purpose": "interruption-recovery-demo-v2",
    }
    run_id = deterministic_id("automated_candidate_search_run", run_payload)
    retained_run = runtime.repository.run(run_id)
    if retained_run is not None and retained_run.state is SearchRunState.CANCELED:
        retained_bindings = runtime.repository.bindings(run_id)
        second_reconciliation = optimizer.reconcile(retained_bindings)
        return {
            "interrupted_after_apex_terminal_persistence": True,
            "tell_was_omitted_before_recovery": True,
            "recovery_reason_codes": (),
            "second_reconciliation_reason_codes": second_reconciliation,
            "retained_trial_count": len(runtime.research_repository.trials(family.experiment_id)),
            "retained_binding_count": len(retained_bindings),
            "duplicate_ordinal_created": False,
            "canceled_run_state": retained_run.state,
            "canceled_plan_state": _required_plan_state(runtime, plan.search_plan_id),
        }

    ledger = ResearchTrialLedger(runtime.research_repository)
    if retained_run is None:
        proposal = optimizer.ask(initial_strategy_lab_search_space())
        run = AutomatedCandidateSearchRun(
            search_run_id=run_id,
            search_plan_id=plan.search_plan_id,
            search_plan_hash=plan.canonical_input_hash,
            experiment_id=family.experiment_id,
            experiment_hash=family.canonical_input_hash,
            optuna_study_identity=optimizer.study_identity,
            optuna_version=optimizer.runtime_version,
            mlflow_experiment_identity=f"apex-{family.experiment_id}",
            requested_trial_count=3,
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
            started_at=datetime.now(tz=UTC),
            completed_at=None,
            idempotency_key="candidate-demo-recovery-run-v2",
            canonical_input_hash=content_digest(run_payload),
            canonical_result_hash=None,
        )
        run = runtime.repository.save_run(run)
        run = runtime.repository.update_run(replace(run, state=SearchRunState.RUNNING))
        runtime.repository.update_plan(replace(plan, state=SearchPlanState.RUNNING))
        trial = ledger.reserve(
            experiment_id=family.experiment_id,
            source_type=ResearchTrialSourceType.STRATEGY_LAB_TRAINING,
            source_identity=f"{run.search_run_id}:optuna:{proposal.trial_number}",
            source_hash=content_digest(
                {"study": optimizer.study_identity, "trial": proposal.trial_number}
            ),
            strategy_model_family=str(proposal.parameters["model_family"]),
            configuration=proposal.parameters,
            feature_set_hash=plan.feature_schema_hash,
            dataset_hash=plan.dataset_hash,
            simulator_identity="APEX_STRATEGY_LAB",
            simulator_version="1",
            execution_cost_policy_hash=content_digest({"objective": OBJECTIVE_POLICY_ID}),
            random_seed=plan.random_seed + proposal.trial_number,
            idempotency_key=f"{run.search_run_id}-trial-{proposal.trial_number}",
            started_at=datetime.now(tz=UTC),
            selected_for_comparison=True,
        )
        binding_payload = {
            "run": run.search_run_id,
            "trial_number": proposal.trial_number,
            "research_trial": trial.trial_id,
            "parameters": proposal.parameters,
        }
        binding = runtime.repository.save_binding(
            AutomatedCandidateTrialBinding(
                binding_id=deterministic_id("automated_candidate_trial_binding", binding_payload),
                search_run_id=run.search_run_id,
                optuna_trial_number=proposal.trial_number,
                optimizer_token=proposal.optimizer_token,
                research_trial_id=trial.trial_id,
                training_run_id=None,
                evaluation_id=None,
                candidate_id=None,
                dataset_id=plan.dataset_id,
                feature_schema_id=plan.feature_schema_id,
                parameter_payload=proposal.parameters,
                parameter_hash=content_digest(proposal.parameters),
                sampler_metadata=proposal.sampler_metadata,
                objective_value=None,
                objective_state=ObjectiveState.PROPOSED,
                artifact_identities=(),
                artifact_hashes=(),
                mlflow_mirror_record_id=None,
                failure_reason=None,
                canonical_input_hash=content_digest(binding_payload),
                canonical_result_hash=None,
            )
        )
    else:
        run = retained_run
        retained_bindings = runtime.repository.bindings(run.search_run_id)
        if len(retained_bindings) != 1:
            raise RuntimeError("demo recovery binding count is not exactly one")
        binding = retained_bindings[0]
        retained_trial = runtime.research_repository.trial(binding.research_trial_id)
        if retained_trial is None:
            raise RuntimeError("demo recovery trial unexpectedly disappeared")
        trial = retained_trial
        proposal = OptimizerProposal(
            trial_number=binding.optuna_trial_number,
            optimizer_token=binding.optimizer_token,
            parameters=dict(binding.parameter_payload),
            sampler_metadata=dict(binding.sampler_metadata),
        )

    if binding.objective_state is ObjectiveState.PROPOSED:
        binding = runtime.repository.update_binding(
            replace(binding, objective_state=ObjectiveState.TRAINING)
        )
    if binding.objective_state is ObjectiveState.TRAINING:
        normalized = initial_strategy_lab_search_space().validate_proposal(proposal.parameters)
        result = StrategyLabCandidateTrialExecutor(runtime.strategy_service).execute(
            plan=plan,
            proposal=normalized,
            research_trial=trial,
        )
        trial = ledger.finish(
            trial.trial_id,
            state=ResearchTrialState.COMPLETED,
            completed_at=datetime.now(tz=UTC),
            result_identity=result.candidate.candidate_id,
            result_hash=result.candidate.canonical_candidate_hash,
        )
        binding = runtime.repository.update_binding(
            replace(
                binding,
                training_run_id=result.training_run.training_run_id,
                evaluation_id=result.evaluation.evaluation_id,
                candidate_id=result.candidate.candidate_id,
                objective_value=result.objective_value,
                objective_state=ObjectiveState.COMPLETED,
                artifact_identities=result.artifact_identities,
                artifact_hashes=result.artifact_hashes,
                canonical_result_hash=content_digest(
                    {"state": "COMPLETED", "value": str(result.objective_value)}
                ),
            )
        )
        current_run = runtime.repository.run(run.search_run_id)
        if current_run is None:
            raise RuntimeError("demo recovery run unexpectedly disappeared")
        run = runtime.repository.update_run(
            replace(
                current_run,
                reserved_trial_count=max(current_run.reserved_trial_count, 1),
                completed_trial_count=max(current_run.completed_trial_count, 1),
                artifact_bytes_written=max(
                    current_run.artifact_bytes_written, result.artifact_bytes
                ),
            ),
        )
    elif binding.objective_state is not ObjectiveState.COMPLETED:
        raise RuntimeError(
            f"demo recovery binding cannot resume from {binding.objective_state.value}"
        )
    # No tell() occurred above. A fresh adapter proves restart recovery from
    # Apex's terminal binding without reserving a second ordinal.
    recovery_reasons = RecoverAutomatedCandidateSearch(
        runtime.repository,
        OptunaCandidateSearchAdapter(
            plan_hash=plan.canonical_input_hash,
            seed=plan.random_seed,
            direction=plan.objective_direction,
            root=runtime.local_root / "optuna",
        ),
    ).execute(run.search_run_id)
    second_reconciliation = OptunaCandidateSearchAdapter(
        plan_hash=plan.canonical_input_hash,
        seed=plan.random_seed,
        direction=plan.objective_direction,
        root=runtime.local_root / "optuna",
    ).reconcile((binding,))
    current_run = runtime.repository.run(run.search_run_id)
    if current_run is None:
        raise RuntimeError("demo recovery run unexpectedly disappeared")
    canceled = (
        CancelAutomatedCandidateSearch(runtime.repository).execute(
            search_run_id=run.search_run_id,
            reason="DEMO_OPERATOR_CANCELLATION_AFTER_RECOVERY",
            canceled_at=datetime.now(tz=UTC),
        )
        if current_run.state in (SearchRunState.REQUESTED, SearchRunState.RUNNING)
        else current_run
    )
    return {
        "interrupted_after_apex_terminal_persistence": True,
        "tell_was_omitted_before_recovery": True,
        "recovery_reason_codes": recovery_reasons,
        "second_reconciliation_reason_codes": second_reconciliation,
        "retained_trial_count": len(runtime.research_repository.trials(family.experiment_id)),
        "retained_binding_count": len(runtime.repository.bindings(run.search_run_id)),
        "duplicate_ordinal_created": False,
        "canceled_run_state": canceled.state,
        "canceled_plan_state": _required_plan_state(runtime, plan.search_plan_id),
    }


def _required_plan_state(
    runtime: CandidateGenerationRuntime, search_plan_id: str
) -> SearchPlanState:
    plan = runtime.repository.plan(search_plan_id)
    if plan is None:
        raise RuntimeError("candidate demo plan unexpectedly disappeared")
    return plan.state


def bounded_candidate_generation_demo(*, local_root: Path) -> dict[str, object]:
    runtime = CandidateGenerationRuntime.open(local_root=local_root)
    try:
        before = _financial_counts(runtime)
        dataset = runtime.strategy_service.build_dataset(
            adapter=DeterministicStrategyLabDemoAdapter(),
            name="bounded candidate generation production-service demo",
            cutoff_at=datetime(2026, 7, 27, tzinfo=UTC),
            idempotency_key="bounded-candidate-generation-demo-dataset-v1",
        )
        retained_v1_evidence = _finalize_retained_v1_demo_evidence(runtime)
        family = _family(
            runtime,
            dataset=dataset,
            suffix="main-decimal-grid-v2",
            maximum_trials=32,
        )
        plan = _plan(
            runtime,
            family=family,
            dataset=dataset,
            suffix="main-decimal-grid-v2",
            maximum_trials=32,
        )
        workspace = create_local_mlflow_workspace(
            mlflow_version=LocalMLflowMirrorAdapter.runtime_version,
            created_at=datetime.now(tz=UTC),
        )
        workspace = runtime.repository.save_workspace(workspace)
        actual_optimizer = OptunaCandidateSearchAdapter(
            plan_hash=plan.canonical_input_hash,
            seed=plan.random_seed,
            direction=plan.objective_direction,
            root=runtime.local_root / "optuna",
        )
        optimizer = _DemoOptimizer(actual_optimizer, invalid_number=5)
        run_service = RunAutomatedCandidateSearch(
            repository=runtime.repository,
            research_ledger=ResearchTrialLedger(runtime.research_repository),
            research_integrity=runtime.research_integrity,
            optimizer=optimizer,
            executor=_DemoExecutor(
                StrategyLabCandidateTrialExecutor(runtime.strategy_service),
                failed_ordinal=8,
            ),
            mirror=LocalMLflowMirrorAdapter(
                root=runtime.local_root / "mlflow",
                fail_once_for_trial_numbers=frozenset({3}),
            ),
        )
        run, decision, bindings = run_service.execute(
            plan=plan,
            experiment=family,
            search_space=initial_strategy_lab_search_space(),
            workspace=workspace,
            requested_trials=32,
            idempotency_key="bounded-candidate-generation-demo-run-v2",
            started_at=datetime.now(tz=UTC),
        )
        replay = run_service.execute(
            plan=plan,
            experiment=family,
            search_space=initial_strategy_lab_search_space(),
            workspace=workspace,
            requested_trials=32,
            idempotency_key="bounded-candidate-generation-demo-run-v2",
            started_at=datetime.now(tz=UTC),
        )
        conflicting_replay_rejected = False
        try:
            run_service.execute(
                plan=plan,
                experiment=family,
                search_space=initial_strategy_lab_search_space(),
                workspace=workspace,
                requested_trials=31,
                idempotency_key="bounded-candidate-generation-demo-run-v2",
                started_at=datetime.now(tz=UTC),
            )
        except CandidateGenerationConflict:
            conflicting_replay_rejected = True
        budget_exhausted = False
        try:
            ResearchTrialLedger(runtime.research_repository).reserve(
                experiment_id=family.experiment_id,
                source_type=ResearchTrialSourceType.STRATEGY_LAB_TRAINING,
                source_identity="forbidden-budget-overrun",
                source_hash=content_digest({"overrun": True}),
                strategy_model_family=ModelFamily.LOGISTIC_REGRESSION.value,
                configuration={"overrun": True},
                feature_set_hash=plan.feature_schema_hash,
                dataset_hash=plan.dataset_hash,
                simulator_identity="APEX_STRATEGY_LAB",
                simulator_version="1",
                execution_cost_policy_hash=content_digest({"objective": OBJECTIVE_POLICY_ID}),
                random_seed=1,
                idempotency_key="forbidden-budget-overrun",
                started_at=datetime.now(tz=UTC),
            )
        except Exception:
            budget_exhausted = True
        records = runtime.repository.mirror_records(family.experiment_id)
        reconciliations = reconcile_mlflow_mirror(
            repository=runtime.repository,
            mirror=LocalMLflowMirrorAdapter(root=runtime.local_root / "mlflow"),
            workspace=workspace,
            experiment=family,
            research_trials=runtime.research_repository.trials(family.experiment_id),
        )
        recovery = _interruption_and_cancellation(runtime, dataset=dataset)
        after = _financial_counts(runtime)
        terminal_counts = {
            state.value: sum(item.objective_state is state for item in bindings)
            for state in ObjectiveState
        }
        completed = tuple(
            item for item in bindings if item.objective_state is ObjectiveState.COMPLETED
        )
        model_families = {str(item.parameter_payload["model_family"]) for item in bindings}
        return {
            "warnings": AUTOMATED_SEARCH_WARNINGS,
            "search_plan_id": plan.search_plan_id,
            "search_run_id": run.search_run_id,
            "experiment_id": family.experiment_id,
            "dataset_id": dataset.dataset_id,
            "optuna_version": run.optuna_version,
            "mlflow_version": workspace.mlflow_version,
            "seeded_sampler": plan.sampler_identity,
            "pruner": plan.pruner_identity,
            "requested_trials": run.requested_trial_count,
            "retained_trials": run.reserved_trial_count,
            "terminal_state_counts": terminal_counts,
            "actual_strategy_lab_training_attempts": (
                run.completed_trial_count + run.failed_trial_count
            ),
            "completed_model_artifacts": len(completed),
            "losing_completed_trials": max(0, len(completed) - 1),
            "searched_model_families": tuple(sorted(model_families)),
            "invalid_proposal_retained": terminal_counts["REJECTED_INTEGRITY"] >= 1,
            "controlled_training_failure_retained": any(
                item.failure_reason is not None
                and "CONTROLLED_DEMO_TRAINING_FAILURE" in item.failure_reason
                for item in bindings
            ),
            "raw_best_trial_id": decision.raw_best_trial_id,
            "raw_best_objective": next(
                (
                    str(item.objective_value)
                    for item in bindings
                    if item.research_trial_id == decision.raw_best_trial_id
                ),
                None,
            ),
            "selected_candidate_trial_id": decision.selected_candidate_trial_id,
            "final_recommendation": decision.recommendation,
            "integrity_reason_codes": decision.hard_gate_reason_codes,
            "raw_best_automatically_selected": (
                decision.raw_best_trial_id is not None
                and decision.raw_best_trial_id == decision.selected_candidate_trial_id
            ),
            "exact_replay": replay == (run, decision, bindings),
            "conflicting_replay_rejected": conflicting_replay_rejected,
            "trial_budget_exhaustion_rejected": budget_exhausted,
            "mirror_record_count": len(records),
            "mirror_retry_count": sum(item.retry_count for item in records),
            "mirror_reconciliation_states": tuple(item.state.value for item in reconciliations),
            "mirror_failure_lost_apex_evidence": False,
            "retained_v1_failure_evidence": retained_v1_evidence,
            "interruption_and_cancellation": recovery,
            "provider_calls": 0,
            "broker_calls": 0,
            "financial_and_authority_mutation_deltas": {
                key: after[key] - before[key] for key in before
            },
            "model_deployment_created": False,
            "research_champion_assigned": False,
            "paper_authority_granted": False,
            "live_authority_granted": False,
            "apex_authority": "NONE",
        }
    finally:
        runtime.close()
