"""Subordinate local Optuna and MLflow adapters for bounded Apex research."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath

import mlflow
import optuna
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient
from optuna.trial import TrialState

from apex.application.candidate_generation import (
    AutomatedCandidateSearchPlan,
    AutomatedCandidateTrialBinding,
    CandidateSearchOptimizerPort,
    MirrorReconciliation,
    MirrorReconciliationState,
    MirrorRecordState,
    MLflowMirrorRecord,
    MLflowMirrorWorkspace,
    ObjectiveState,
    OptimizerProposal,
)
from apex.application.research_integrity import (
    ResearchExperimentFamily,
    ResearchTrialAttempt,
)
from apex.domain.candidate_generation import (
    OBJECTIVE_POLICY_ID,
    BoundedSearchSpace,
    ObjectiveDirection,
    ParameterKind,
)
from apex.domain.values import canonical_json_bytes, content_digest, deterministic_id


class CandidateToolAdapterError(RuntimeError):
    """A subordinate local research tool failed safely."""


class OptunaCandidateSearchAdapter(CandidateSearchOptimizerPort):
    """Seeded single-process ask/tell over an ignored local SQLite store."""

    runtime_version = optuna.__version__

    def __init__(
        self,
        *,
        plan_hash: str,
        seed: int,
        direction: ObjectiveDirection = ObjectiveDirection.MINIMIZE,
        root: Path | None = None,
    ) -> None:
        self._root = (root or Path(".local") / "optuna").resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise CandidateToolAdapterError("OPTUNA_ROOT_INVALID")
        if len(plan_hash) != 64 or seed < 0:
            raise CandidateToolAdapterError("OPTUNA_CONFIGURATION_INVALID")
        self.study_identity = f"apex-{plan_hash}"
        database = self._root / f"{plan_hash}.db"
        if not database.is_relative_to(self._root):
            raise CandidateToolAdapterError("OPTUNA_STORAGE_ESCAPES_ROOT")
        storage = optuna.storages.RDBStorage(url=f"sqlite:///{database}")
        self._study = optuna.create_study(
            study_name=self.study_identity,
            storage=storage,
            sampler=optuna.samplers.TPESampler(seed=seed),
            pruner=optuna.pruners.NopPruner(),
            direction=direction.value.lower(),
            load_if_exists=True,
        )
        self._live: dict[int, optuna.Trial] = {}

    def ask(self, search_space: BoundedSearchSpace) -> OptimizerProposal:
        trial = self._study.ask()
        parameters: dict[str, object] = {}
        for definition in search_space.parameters:
            if definition.kind is ParameterKind.CATEGORICAL:
                parameters[definition.name] = trial.suggest_categorical(
                    definition.name, list(definition.choices)
                )
            elif definition.kind is ParameterKind.INTEGER:
                assert (
                    definition.lower is not None
                    and definition.upper is not None
                    and definition.step is not None
                )
                parameters[definition.name] = trial.suggest_int(
                    definition.name,
                    int(definition.lower),
                    int(definition.upper),
                    step=int(definition.step),
                )
            else:
                assert definition.lower is not None and definition.upper is not None
                proposed = trial.suggest_float(
                    definition.name,
                    float(definition.lower),
                    float(definition.upper),
                    step=float(definition.step) if definition.step is not None else None,
                    log=definition.logarithmic,
                )
                decimal_proposed = Decimal(str(proposed))
                if definition.step is not None:
                    grid_index = (
                        (decimal_proposed - definition.lower) / definition.step
                    ).to_integral_value()
                    decimal_proposed = definition.lower + grid_index * definition.step
                parameters[definition.name] = format(decimal_proposed, "f")
        self._live[trial.number] = trial
        return OptimizerProposal(
            trial_number=trial.number,
            optimizer_token=f"{self.study_identity}:{trial.number}",
            parameters=parameters,
            sampler_metadata={
                "sampler": "TPESampler",
                "optuna_version": self.runtime_version,
                "study": self.study_identity,
            },
        )

    def tell(
        self,
        proposal: OptimizerProposal,
        *,
        state: ObjectiveState,
        objective_value: object,
    ) -> None:
        trial = self._live.pop(proposal.trial_number, None)
        if trial is None:
            frozen = self._study.get_trials(deepcopy=False)
            retained = next((item for item in frozen if item.number == proposal.trial_number), None)
            if retained is not None and retained.state.is_finished():
                return
            raise CandidateToolAdapterError("OPTUNA_TRIAL_RECONCILIATION_REQUIRED")
        if state is ObjectiveState.COMPLETED and objective_value is not None:
            self._study.tell(trial, float(str(objective_value)))
        elif state is ObjectiveState.PRUNED:
            self._study.tell(trial, state=TrialState.PRUNED)
        else:
            self._study.tell(trial, state=TrialState.FAIL)

    def reconcile(self, bindings: Sequence[AutomatedCandidateTrialBinding]) -> tuple[str, ...]:
        optuna_by_number = {item.number: item for item in self._study.get_trials(deepcopy=False)}
        reasons: list[str] = []
        for binding in bindings:
            retained = optuna_by_number.get(binding.optuna_trial_number)
            if retained is None:
                reasons.append(f"OPTUNA_TRIAL_MISSING:{binding.optuna_trial_number}")
                continue
            if (
                binding.objective_state
                in (
                    ObjectiveState.COMPLETED,
                    ObjectiveState.FAILED,
                    ObjectiveState.REJECTED_INTEGRITY,
                    ObjectiveState.PRUNED,
                    ObjectiveState.CANCELED,
                )
                and not retained.state.is_finished()
            ):
                if binding.objective_state is ObjectiveState.COMPLETED:
                    assert binding.objective_value is not None
                    self._study.tell(retained.number, float(str(binding.objective_value)))
                elif binding.objective_state is ObjectiveState.PRUNED:
                    self._study.tell(retained.number, state=TrialState.PRUNED)
                else:
                    self._study.tell(retained.number, state=TrialState.FAIL)
            elif (
                binding.objective_state in (ObjectiveState.PROPOSED, ObjectiveState.TRAINING)
                and retained.state.is_finished()
            ):
                reasons.append(f"OPTUNA_TERMINAL_BEFORE_APEX:{binding.optuna_trial_number}")
        return tuple(reasons)


class DeterministicFixtureCandidateSearchAdapter(CandidateSearchOptimizerPort):
    """Small deterministic adapter for focused lifecycle tests only."""

    runtime_version = "FIXTURE"

    def __init__(
        self,
        proposals: tuple[dict[str, object], ...],
        *,
        study_identity: str = "fixture-study",
    ) -> None:
        self.study_identity = study_identity
        self._proposals = proposals
        self._cursor = 0
        self.told: list[tuple[int, ObjectiveState, str | None]] = []

    def ask(self, search_space: BoundedSearchSpace) -> OptimizerProposal:
        del search_space
        if self._cursor >= len(self._proposals):
            raise CandidateToolAdapterError("FIXTURE_PROPOSALS_EXHAUSTED")
        number = self._cursor
        self._cursor += 1
        return OptimizerProposal(
            trial_number=number,
            optimizer_token=f"{self.study_identity}:{number}",
            parameters=dict(self._proposals[number]),
            sampler_metadata={"sampler": "DETERMINISTIC_FIXTURE"},
        )

    def tell(
        self,
        proposal: OptimizerProposal,
        *,
        state: ObjectiveState,
        objective_value: object,
    ) -> None:
        self.told.append(
            (
                proposal.trial_number,
                state,
                str(objective_value) if objective_value is not None else None,
            )
        )

    def reconcile(self, bindings: Sequence[AutomatedCandidateTrialBinding]) -> tuple[str, ...]:
        terminal_numbers = {number for number, _state, _value in self.told}
        return tuple(
            f"FIXTURE_TELL_MISSING:{item.optuna_trial_number}"
            for item in bindings
            if item.objective_state not in (ObjectiveState.PROPOSED, ObjectiveState.TRAINING)
            and item.optuna_trial_number not in terminal_numbers
        )


class LocalMLflowMirrorAdapter:
    """Local SQLite/file mirror; Apex hashes and decisions remain authoritative."""

    runtime_version = mlflow.__version__

    def __init__(
        self,
        *,
        root: Path | None = None,
        fail_once_for_trial_numbers: frozenset[int] = frozenset(),
    ) -> None:
        self._root = (root or Path(".local") / "mlflow").resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise CandidateToolAdapterError("MLFLOW_ROOT_INVALID")
        self._backend = self._root / "backend" / "mlflow.db"
        self._artifacts = self._root / "artifacts"
        self._backend.parent.mkdir(parents=True, exist_ok=True)
        self._artifacts.mkdir(parents=True, exist_ok=True)
        self._tracking_uri = f"sqlite:///{self._backend}"
        self._client = MlflowClient(tracking_uri=self._tracking_uri)
        self._fail_once = set(fail_once_for_trial_numbers)
        os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")

    @property
    def tracking_uri(self) -> str:
        return self._tracking_uri

    def mirror(
        self,
        *,
        workspace: MLflowMirrorWorkspace,
        experiment: ResearchExperimentFamily,
        plan: AutomatedCandidateSearchPlan,
        trial: ResearchTrialAttempt,
        binding: AutomatedCandidateTrialBinding,
        metrics: Mapping[str, Decimal | str | int | None],
        artifacts: Mapping[str, bytes],
        prior: MLflowMirrorRecord | None = None,
    ) -> MLflowMirrorRecord:
        if workspace.loopback_bind_host != "127.0.0.1":
            raise CandidateToolAdapterError("MLFLOW_NON_LOOPBACK_REJECTED")
        name = f"apex-{experiment.experiment_id}"
        input_payload = {
            "source": trial.trial_id,
            "source_hash": trial.canonical_input_hash,
            "workspace": workspace.workspace_id,
            "experiment": name,
        }
        record_id = (
            prior.mirror_record_id
            if prior is not None
            else deterministic_id("mlflow_mirror_record", input_payload)
        )
        parameter_hash = content_digest(binding.parameter_payload)
        metric_hash = content_digest(
            {key: str(value) if value is not None else None for key, value in metrics.items()}
        )
        artifact_hash = content_digest(
            {key: content_digest(value.hex()) for key, value in artifacts.items()}
        )
        attempted_at = datetime_now()
        retry_count = 0
        if prior is not None:
            retry_count = prior.retry_count
            if prior.state is MirrorRecordState.FAILED_RETRYABLE:
                retry_count += 1
        try:
            if binding.optuna_trial_number in self._fail_once:
                self._fail_once.remove(binding.optuna_trial_number)
                raise OSError("controlled local mirror failure")
            experiment_id = self._client.get_experiment_by_name(name)
            if experiment_id is None:
                created_id = self._client.create_experiment(
                    name,
                    artifact_location=self._artifacts.resolve().as_uri(),
                    tags={
                        "apex.authority": "NONE",
                        "apex.lifecycle": "RESEARCH_MIRROR",
                    },
                )
                mlflow_experiment_id = created_id
            else:
                mlflow_experiment_id = experiment_id.experiment_id
            run_id = prior.mlflow_run_id if prior is not None else None
            if run_id is not None:
                try:
                    retained_run = self._client.get_run(run_id)
                    if retained_run.info.lifecycle_stage != "active":
                        run_id = None
                except MlflowException:
                    run_id = None
            if run_id is None:
                created = self._client.create_run(
                    mlflow_experiment_id,
                    tags={
                        "apex.authority": "NONE",
                        "apex.lifecycle": "RESEARCH_MIRROR",
                        "apex.experiment_family_id": experiment.experiment_id,
                        "apex.experiment_family_hash": experiment.canonical_input_hash,
                        "apex.search_plan_id": plan.search_plan_id,
                        "apex.search_plan_hash": plan.canonical_input_hash,
                        "apex.research_trial_id": trial.trial_id,
                        "apex.research_trial_ordinal": str(trial.ordinal),
                    },
                )
                run_id = created.info.run_id
                for key, value in (
                    ("model_family", trial.strategy_model_family),
                    ("dataset_hash", plan.dataset_hash),
                    ("feature_schema_hash", plan.feature_schema_hash),
                    ("objective_policy", OBJECTIVE_POLICY_ID),
                    ("sampler", plan.sampler_identity),
                    ("sampler_version", plan.sampler_version),
                    ("seed", str(plan.random_seed)),
                    *(
                        (f"parameter.{key}", str(value))
                        for key, value in sorted(binding.parameter_payload.items())
                    ),
                ):
                    self._client.log_param(run_id, key, value)
            self._client.set_tag(run_id, "apex.trial_terminal_state", binding.objective_state.value)
            self._client.set_tag(run_id, "apex.artifact_sha256", ",".join(binding.artifact_hashes))
            self._client.set_tag(run_id, "apex.source_candidate_id", binding.candidate_id or "NONE")
            for key, value in (
                ("apex.source_hash", trial.canonical_input_hash),
                ("apex.parameter_hash", parameter_hash),
                ("apex.metric_hash", metric_hash),
                ("apex.artifact_set_hash", artifact_hash),
            ):
                self._client.set_tag(run_id, key, value)
            for key, metric_value in metrics.items():
                if metric_value is not None and _is_numeric(metric_value):
                    self._client.log_metric(run_id, key, float(str(metric_value)))
            for name_part, payload in sorted(artifacts.items()):
                safe = _safe_artifact_name(name_part)
                self._client.log_text(run_id, payload.decode("utf-8", errors="strict"), safe)
            self._client.log_text(
                run_id,
                canonical_json_bytes(
                    {
                        "source_hash": trial.canonical_input_hash,
                        "parameter_hash": parameter_hash,
                        "metric_hash": metric_hash,
                        "artifact_set_hash": artifact_hash,
                        "apex_authority": "NONE",
                    }
                ).decode("utf-8"),
                "apex-provenance-manifest.txt",
            )
            if binding.objective_state not in (
                ObjectiveState.PROPOSED,
                ObjectiveState.TRAINING,
            ):
                self._client.set_terminated(
                    run_id,
                    status=(
                        "FINISHED"
                        if binding.objective_state is ObjectiveState.COMPLETED
                        else "FAILED"
                    ),
                )
            return MLflowMirrorRecord(
                mirror_record_id=record_id,
                source_type="RESEARCH_TRIAL_ATTEMPT",
                source_identity=trial.trial_id,
                source_hash=trial.canonical_input_hash,
                workspace_id=workspace.workspace_id,
                mlflow_experiment_id=mlflow_experiment_id,
                mlflow_experiment_name=name,
                mlflow_run_id=run_id,
                mirrored_parameter_hash=parameter_hash,
                mirrored_metric_hash=metric_hash,
                mirrored_artifact_set_hash=artifact_hash,
                state=MirrorRecordState.MIRRORED,
                attempted_at=attempted_at,
                completed_at=datetime_now(),
                failure_code=None,
                failure_message=None,
                retry_count=retry_count,
                canonical_input_hash=content_digest(input_payload),
                canonical_result_hash=content_digest(
                    {"run_id": run_id, "state": "MIRRORED", "retry": retry_count}
                ),
            )
        except Exception as error:
            return MLflowMirrorRecord(
                mirror_record_id=record_id,
                source_type="RESEARCH_TRIAL_ATTEMPT",
                source_identity=trial.trial_id,
                source_hash=trial.canonical_input_hash,
                workspace_id=workspace.workspace_id,
                mlflow_experiment_id=prior.mlflow_experiment_id if prior else None,
                mlflow_experiment_name=name,
                mlflow_run_id=prior.mlflow_run_id if prior else None,
                mirrored_parameter_hash=parameter_hash,
                mirrored_metric_hash=metric_hash,
                mirrored_artifact_set_hash=artifact_hash,
                state=MirrorRecordState.FAILED_RETRYABLE,
                attempted_at=attempted_at,
                completed_at=datetime_now(),
                failure_code=type(error).__name__,
                failure_message=str(error)[:500],
                retry_count=retry_count,
                canonical_input_hash=content_digest(input_payload),
                canonical_result_hash=content_digest(
                    {
                        "state": "FAILED_RETRYABLE",
                        "failure": type(error).__name__,
                        "retry": retry_count,
                    }
                ),
            )

    def reconcile(
        self, workspace: MLflowMirrorWorkspace, record: MLflowMirrorRecord
    ) -> MirrorReconciliation:
        if workspace.state.value != "READY":
            return MirrorReconciliation(
                record.mirror_record_id,
                MirrorReconciliationState.MLFLOW_UNAVAILABLE,
                ("MLFLOW_WORKSPACE_DISABLED",),
            )
        if record.mlflow_run_id is None:
            return MirrorReconciliation(
                record.mirror_record_id,
                MirrorReconciliationState.MIRROR_MISSING,
                ("MLFLOW_RUN_ID_MISSING",),
            )
        try:
            run = self._client.get_run(record.mlflow_run_id)
        except MlflowException:
            return MirrorReconciliation(
                record.mirror_record_id,
                MirrorReconciliationState.MIRROR_MISSING,
                ("MLFLOW_RUN_MISSING",),
            )
        except Exception:
            return MirrorReconciliation(
                record.mirror_record_id,
                MirrorReconciliationState.MLFLOW_UNAVAILABLE,
                ("MLFLOW_LOCAL_STORE_UNAVAILABLE",),
            )
        tags = run.data.tags
        if run.info.lifecycle_stage != "active":
            return MirrorReconciliation(
                record.mirror_record_id,
                MirrorReconciliationState.MIRROR_MISSING,
                ("MLFLOW_RUN_DELETED",),
            )
        if tags.get("apex.authority") != "NONE":
            return MirrorReconciliation(
                record.mirror_record_id,
                MirrorReconciliationState.MIRROR_DIVERGED,
                ("MLFLOW_AUTHORITY_TAG_DIVERGED",),
            )
        if tags.get("apex.research_trial_id") != record.source_identity:
            return MirrorReconciliation(
                record.mirror_record_id,
                MirrorReconciliationState.MIRROR_DIVERGED,
                ("MLFLOW_SOURCE_IDENTITY_DIVERGED",),
            )
        expected_tags = {
            "apex.source_hash": record.source_hash,
            "apex.parameter_hash": record.mirrored_parameter_hash,
            "apex.metric_hash": record.mirrored_metric_hash,
            "apex.artifact_set_hash": record.mirrored_artifact_set_hash,
        }
        mismatched = tuple(
            f"MLFLOW_{key.removeprefix('apex.').upper()}_DIVERGED"
            for key, expected in expected_tags.items()
            if tags.get(key) != expected
        )
        if mismatched:
            return MirrorReconciliation(
                record.mirror_record_id,
                MirrorReconciliationState.MIRROR_DIVERGED,
                mismatched,
            )
        if not tags.get("apex.trial_terminal_state"):
            return MirrorReconciliation(
                record.mirror_record_id,
                MirrorReconciliationState.MIRROR_STALE,
                ("MLFLOW_TERMINAL_STATE_MISSING",),
            )
        artifacts = self._client.list_artifacts(record.mlflow_run_id)
        if not any(
            item.path == "apex-provenance-manifest.txt" and not item.is_dir for item in artifacts
        ):
            return MirrorReconciliation(
                record.mirror_record_id,
                MirrorReconciliationState.MIRROR_MISSING,
                ("MLFLOW_PROVENANCE_MANIFEST_MISSING",),
            )
        return MirrorReconciliation(
            record.mirror_record_id,
            MirrorReconciliationState.MIRROR_CONFORMANT,
            (),
        )


def _safe_artifact_name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or any(part in ("", ".") for part in path.parts)
        or path.suffix not in (".json", ".txt")
    ):
        raise CandidateToolAdapterError("MLFLOW_ARTIFACT_NAME_INVALID")
    return path.as_posix()


def _is_numeric(value: object) -> bool:
    try:
        float(str(value))
    except (TypeError, ValueError):
        return False
    return True


def datetime_now() -> datetime:
    return datetime.now(tz=UTC)
