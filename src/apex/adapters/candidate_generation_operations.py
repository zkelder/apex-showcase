"""Local operator orchestration for bounded candidate-generation research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apex.adapters.candidate_generation_postgres import (
    PostgresCandidateGenerationRepository,
)
from apex.adapters.candidate_generation_tools import (
    LocalMLflowMirrorAdapter,
    OptunaCandidateSearchAdapter,
)
from apex.adapters.postgres import (
    PostgresSettings,
    create_postgres_engine,
)
from apex.adapters.research_integrity_artifacts import (
    LocalResearchReturnArtifactStore,
)
from apex.adapters.research_integrity_postgres import (
    PostgresResearchIntegrityRepository,
)
from apex.adapters.securities_strategy_lab_dataset_postgres import (
    PostgresSecuritiesDirectionDatasetRepository,
)
from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.adapters.strategy_lab_postgres import PostgresStrategyLabStore
from apex.application.candidate_generation import (
    AutomatedCandidateSearchPlan,
    AutomatedCandidateSearchRun,
    AutomatedCandidateTrialBinding,
    CancelAutomatedCandidateSearch,
    CandidateGenerationApplicationError,
    CandidateGenerationDecision,
    CreateAutomatedCandidateSearchPlan,
    RecoverAutomatedCandidateSearch,
    RunAutomatedCandidateSearch,
    StrategyLabCandidateTrialExecutor,
    create_local_mlflow_workspace,
    reconcile_mlflow_mirror,
)
from apex.application.research_integrity import (
    ResearchFamilyState,
    ResearchIntegrityService,
    ResearchTrialLedger,
)
from apex.application.securities_candidate_evaluation import (
    GenuineSecuritiesCandidateTrialExecutor,
)
from apex.application.strategy_lab import DatasetState, ModelFamily, StrategyLabService
from apex.domain.candidate_generation import (
    ObjectiveIdentity,
    initial_strategy_lab_search_space,
)
from apex.domain.values import content_digest

MANAGED_SPECIFICATION_ID = "strategy-lab-initial-bounded-v1"
AUTOMATED_SEARCH_WARNINGS = (
    "AUTOMATED SEARCH EXPLORES ONLY THE PREDECLARED BOUNDED SPACE",
    "OPTUNA PROPOSES PARAMETERS — APEX OWNS THE TRIAL LEDGER AND DECISION",
    "MLFLOW IS A LOCAL RESEARCH MIRROR — IT GRANTS NO AUTHORITY",
    "CANDIDATE ELIGIBILITY DOES NOT GRANT DEPLOYMENT OR CAPITAL AUTHORITY",
)


@dataclass(slots=True)
class CandidateGenerationRuntime:
    engine: Any
    repository: PostgresCandidateGenerationRepository
    research_repository: PostgresResearchIntegrityRepository
    strategy_store: PostgresStrategyLabStore
    strategy_service: StrategyLabService
    research_integrity: ResearchIntegrityService
    local_root: Path

    @classmethod
    def open(
        cls,
        *,
        settings: PostgresSettings | None = None,
        local_root: Path | None = None,
    ) -> CandidateGenerationRuntime:
        root = (local_root or Path(".local")).resolve()
        engine = create_postgres_engine(settings or PostgresSettings.from_env())
        research_repository = PostgresResearchIntegrityRepository(engine)
        strategy_store = PostgresStrategyLabStore(engine)
        return cls(
            engine=engine,
            repository=PostgresCandidateGenerationRepository(engine),
            research_repository=research_repository,
            strategy_store=strategy_store,
            strategy_service=StrategyLabService(
                strategy_store,
                LocalStrategyLabArtifactStore(root / "strategy_lab" / "artifacts"),
            ),
            research_integrity=ResearchIntegrityService(
                research_repository,
                LocalResearchReturnArtifactStore(root / "research-integrity" / "return-series"),
            ),
            local_root=root,
        )

    def close(self) -> None:
        self.engine.dispose()

    def create_plan(
        self,
        *,
        experiment_id: str,
        name: str,
        dataset_id: str,
        feature_schema_id: str,
        specification: str,
        objective: ObjectiveIdentity,
        maximum_trials: int,
        seed: int,
        reason: str,
        idempotency_key: str,
    ) -> AutomatedCandidateSearchPlan:
        if specification != MANAGED_SPECIFICATION_ID:
            raise CandidateGenerationApplicationError("MANAGED_SEARCH_SPECIFICATION_REQUIRED")
        family = self.research_repository.family(experiment_id)
        dataset = self.strategy_store.dataset(dataset_id)
        if family is None:
            raise CandidateGenerationApplicationError("RESEARCH_FAMILY_NOT_FOUND")
        if dataset is None:
            raise CandidateGenerationApplicationError("STRATEGY_LAB_DATASET_NOT_FOUND")
        if dataset.schema.schema_id != feature_schema_id:
            raise CandidateGenerationApplicationError("FEATURE_SCHEMA_ID_MISMATCH")
        prior = self.repository.plan_by_idempotency(idempotency_key)
        if prior is not None:
            if (
                prior.experiment_id != experiment_id
                or prior.dataset_id != dataset_id
                or prior.feature_schema_id != feature_schema_id
                or prior.objective_identity is not objective
                or prior.maximum_trials != maximum_trials
                or prior.random_seed != seed
            ):
                raise CandidateGenerationApplicationError("SEARCH_PLAN_IDEMPOTENCY_CONFLICT")
            return prior
        failed_budget = min(5, max(0, maximum_trials // 5))
        if failed_budget >= maximum_trials:
            failed_budget = maximum_trials - 1
        return CreateAutomatedCandidateSearchPlan(self.repository).execute(
            experiment=family,
            existing_experiment_trial_count=len(self.research_repository.trials(experiment_id)),
            dataset=dataset,
            search_space=initial_strategy_lab_search_space(),
            display_name=name,
            permitted_model_families=tuple(ModelFamily),
            objective_identity=objective,
            maximum_trials=maximum_trials,
            maximum_failed_trials=failed_budget,
            wall_clock_budget_seconds=3_600,
            per_trial_budget_seconds=300,
            artifact_byte_budget=500_000_000,
            random_seed=seed,
            operator_action_id=f"candidate-search-cli:{idempotency_key}",
            operator_reason=reason,
            idempotency_key=idempotency_key,
            created_at=datetime.now(tz=UTC),
        )

    def run_search(
        self,
        *,
        search_plan_id: str,
        requested_trials: int,
        idempotency_key: str,
    ) -> tuple[
        AutomatedCandidateSearchRun,
        CandidateGenerationDecision,
        tuple[AutomatedCandidateTrialBinding, ...],
    ]:
        plan = self.repository.plan(search_plan_id)
        if plan is None:
            raise CandidateGenerationApplicationError("SEARCH_PLAN_NOT_FOUND")
        experiment = self.research_repository.family(plan.experiment_id)
        if experiment is None:
            raise CandidateGenerationApplicationError("RESEARCH_FAMILY_NOT_FOUND")
        workspace = create_local_mlflow_workspace(
            mlflow_version=LocalMLflowMirrorAdapter.runtime_version,
            created_at=datetime.now(tz=UTC),
        )
        workspace = self.repository.save_workspace(workspace)
        optimizer = OptunaCandidateSearchAdapter(
            plan_hash=plan.canonical_input_hash,
            seed=plan.random_seed,
            direction=plan.objective_direction,
            root=self.local_root / "optuna",
        )
        return RunAutomatedCandidateSearch(
            repository=self.repository,
            research_ledger=ResearchTrialLedger(self.research_repository),
            research_integrity=self.research_integrity,
            optimizer=optimizer,
            executor=StrategyLabCandidateTrialExecutor(self.strategy_service),
            mirror=LocalMLflowMirrorAdapter(root=self.local_root / "mlflow"),
        ).execute(
            plan=plan,
            experiment=experiment,
            search_space=initial_strategy_lab_search_space(),
            workspace=workspace,
            requested_trials=requested_trials,
            idempotency_key=idempotency_key,
            started_at=datetime.now(tz=UTC),
        )

    def run_genuine_search(
        self,
        *,
        search_plan_id: str,
        requested_trials: int,
        idempotency_key: str,
    ) -> tuple[
        AutomatedCandidateSearchRun,
        CandidateGenerationDecision,
        tuple[AutomatedCandidateTrialBinding, ...],
    ]:
        plan = self.repository.plan(search_plan_id)
        if plan is None:
            raise CandidateGenerationApplicationError("SEARCH_PLAN_NOT_FOUND")
        experiment = self.research_repository.family(plan.experiment_id)
        dataset = next(
            (
                item
                for item in PostgresSecuritiesDirectionDatasetRepository(self.engine).datasets()
                if item.strategy_lab_dataset_id == plan.dataset_id
            ),
            None,
        )
        snapshot = self.strategy_store.dataset(plan.dataset_id)
        if experiment is None:
            raise CandidateGenerationApplicationError("RESEARCH_FAMILY_NOT_FOUND")
        if (
            dataset is None
            or snapshot is None
            or dataset.strategy_lab_dataset_id != plan.dataset_id
            or dataset.strategy_lab_dataset_id != snapshot.dataset_id
        ):
            raise CandidateGenerationApplicationError("GENUINE_DATASET_PLAN_MISMATCH")
        workspace = create_local_mlflow_workspace(
            mlflow_version=LocalMLflowMirrorAdapter.runtime_version,
            created_at=datetime.now(tz=UTC),
        )
        workspace = self.repository.save_workspace(workspace)
        optimizer = OptunaCandidateSearchAdapter(
            plan_hash=plan.canonical_input_hash,
            seed=plan.random_seed,
            direction=plan.objective_direction,
            root=self.local_root / "optuna",
        )
        return RunAutomatedCandidateSearch(
            repository=self.repository,
            research_ledger=ResearchTrialLedger(self.research_repository),
            research_integrity=self.research_integrity,
            optimizer=optimizer,
            executor=GenuineSecuritiesCandidateTrialExecutor(
                strategy_service=self.strategy_service,
                artifacts=LocalStrategyLabArtifactStore(
                    self.local_root / "strategy_lab" / "artifacts"
                ),
                dataset=dataset,
                snapshot=snapshot,
            ),
            mirror=LocalMLflowMirrorAdapter(root=self.local_root / "mlflow"),
        ).execute(
            plan=plan,
            experiment=experiment,
            search_space=initial_strategy_lab_search_space(),
            workspace=workspace,
            requested_trials=requested_trials,
            idempotency_key=idempotency_key,
            started_at=datetime.now(tz=UTC),
        )

    def recover(self, search_run_id: str) -> tuple[str, ...]:
        run = self.repository.run(search_run_id)
        if run is None:
            raise CandidateGenerationApplicationError("SEARCH_RUN_NOT_FOUND")
        plan = self.repository.plan(run.search_plan_id)
        if plan is None:
            raise CandidateGenerationApplicationError("SEARCH_PLAN_NOT_FOUND")
        return RecoverAutomatedCandidateSearch(
            self.repository,
            OptunaCandidateSearchAdapter(
                plan_hash=plan.canonical_input_hash,
                seed=plan.random_seed,
                direction=plan.objective_direction,
                root=self.local_root / "optuna",
            ),
        ).execute(search_run_id)

    def cancel(self, *, search_run_id: str, reason: str) -> AutomatedCandidateSearchRun:
        return CancelAutomatedCandidateSearch(self.repository).execute(
            search_run_id=search_run_id,
            reason=reason,
            canceled_at=datetime.now(tz=UTC),
        )

    def status(self, search_run_id: str) -> dict[str, object]:
        run = self.repository.run(search_run_id)
        if run is None:
            raise CandidateGenerationApplicationError("SEARCH_RUN_NOT_FOUND")
        plan = self.repository.plan(run.search_plan_id)
        return {
            "warnings": AUTOMATED_SEARCH_WARNINGS,
            "plan": plan,
            "run": run,
            "bindings": self.repository.bindings(search_run_id),
            "decision": self.repository.decision(search_run_id),
            "authority": "NONE",
        }

    def reconcile_mlflow(self, experiment_id: str) -> dict[str, object]:
        experiment = self.research_repository.family(experiment_id)
        if experiment is None:
            raise CandidateGenerationApplicationError("RESEARCH_FAMILY_NOT_FOUND")
        workspace = create_local_mlflow_workspace(
            mlflow_version=LocalMLflowMirrorAdapter.runtime_version,
            created_at=datetime.now(tz=UTC),
        )
        retained = self.repository.workspace(workspace.workspace_id)
        if retained is None:
            retained = self.repository.save_workspace(workspace)
        reconciliations = reconcile_mlflow_mirror(
            repository=self.repository,
            mirror=LocalMLflowMirrorAdapter(root=self.local_root / "mlflow"),
            experiment=experiment,
            research_trials=self.research_repository.trials(experiment_id),
            workspace=retained,
        )
        return {
            "warnings": AUTOMATED_SEARCH_WARNINGS,
            "experiment_id": experiment_id,
            "records": self.repository.mirror_records(experiment_id),
            "reconciliations": reconciliations,
            "authority": "NONE",
        }

    def readiness(self) -> dict[str, object]:
        datasets = tuple(
            item for item in self.strategy_store.datasets() if item.state is DatasetState.READY
        )
        schemas = {item.schema.schema_id: item.schema for item in datasets}
        families = tuple(
            item
            for item in self.research_repository.families()
            if item.state is ResearchFamilyState.OPEN
        )
        compatible: list[dict[str, object]] = []
        for family in families:
            trials = self.research_repository.trials(family.experiment_id)
            family_datasets = tuple(
                item
                for item in datasets
                if item.canonical_result_hash in family.data_scope_hashes
                and item.schema.canonical_hash in family.feature_schema_hashes
            )
            remaining = max(0, family.maximum_trials - len(trials))
            model_compatible = set(item.value for item in ModelFamily).issubset(
                family.permitted_families
            )
            space_compatible = (
                content_digest(family.parameter_space.get("parameters"))
                == initial_strategy_lab_search_space().canonical_hash
            )
            compatible.append(
                {
                    "experiment_id": family.experiment_id,
                    "remaining_trial_budget": remaining,
                    "ready_dataset_ids": tuple(item.dataset_id for item in family_datasets),
                    "supported_model_families": tuple(item.value for item in ModelFamily),
                    "aligned_fold_policy": family.evaluation_policy_id,
                    "blocked_by_simulator_mismatch": any(item.integrity_blocked for item in trials),
                    "lacking_observations": len(trials) < 3,
                    "ready_for_bounded_search": bool(
                        family_datasets
                        and remaining >= 3
                        and model_compatible
                        and space_compatible
                        and not any(item.integrity_blocked for item in trials)
                    ),
                    "safe_initial_trial_budget": min(30, remaining),
                }
            )
        return {
            "warnings": AUTOMATED_SEARCH_WARNINGS,
            "open_experiment_family_count": len(families),
            "ready_dataset_ids": tuple(item.dataset_id for item in datasets),
            "compatible_feature_schemas": tuple(schemas.values()),
            "families": tuple(compatible),
            "any_retained_family_ready": any(
                bool(item["ready_for_bounded_search"]) for item in compatible
            ),
            "search_started": False,
            "candidate_promoted": False,
            "authority": "NONE",
        }
