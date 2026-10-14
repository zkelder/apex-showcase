"""Typed PostgreSQL repository for bounded candidate-generation evidence."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.postgres import (
    automated_candidate_search_plan,
    automated_candidate_search_run,
    automated_candidate_trial_binding,
    candidate_generation_decision,
    mlflow_mirror_record,
    mlflow_mirror_workspace,
)
from apex.application.candidate_generation import (
    AutomatedCandidateSearchPlan,
    AutomatedCandidateSearchRun,
    AutomatedCandidateTrialBinding,
    CandidateGenerationConflict,
    CandidateGenerationDecision,
    CandidateGenerationRecommendation,
    CandidateGenerationRepository,
    MirrorRecordState,
    MirrorWorkspaceState,
    MLflowMirrorRecord,
    MLflowMirrorWorkspace,
    ObjectiveState,
    SearchPlanState,
    SearchRunState,
)
from apex.domain.candidate_generation import ObjectiveDirection, ObjectiveIdentity
from apex.domain.values import canonical_json_bytes


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _mapping(payload: str) -> dict[str, object]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise CandidateGenerationConflict("CANDIDATE_JSON_OBJECT_REQUIRED")
    return cast(dict[str, object], value)


def _sequence(payload: str) -> tuple[dict[str, object], ...]:
    value = json.loads(payload)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CandidateGenerationConflict("CANDIDATE_JSON_SEQUENCE_REQUIRED")
    restored: list[dict[str, object]] = []
    for item in value:
        mapping = dict(cast(dict[str, object], item))
        choices = mapping.get("choices")
        if isinstance(choices, list):
            mapping["choices"] = tuple(choices)
        restored.append(mapping)
    return tuple(restored)


class PostgresCandidateGenerationRepository(CandidateGenerationRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def plan_by_idempotency(self, key: str) -> AutomatedCandidateSearchPlan | None:
        return self._one_plan(automated_candidate_search_plan.c.idempotency_key == key)

    def plans(self, experiment_id: str | None = None) -> tuple[AutomatedCandidateSearchPlan, ...]:
        statement = sa.select(automated_candidate_search_plan)
        if experiment_id is not None:
            statement = statement.where(
                automated_candidate_search_plan.c.experiment_id == experiment_id
            )
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    statement.order_by(
                        automated_candidate_search_plan.c.created_at,
                        automated_candidate_search_plan.c.search_plan_id,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_plan(row) for row in rows)

    def plan(self, search_plan_id: str) -> AutomatedCandidateSearchPlan | None:
        return self._one_plan(automated_candidate_search_plan.c.search_plan_id == search_plan_id)

    def _one_plan(self, clause: Any) -> AutomatedCandidateSearchPlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(sa.select(automated_candidate_search_plan).where(clause))
                .mappings()
                .one_or_none()
            )
        return _plan(row) if row is not None else None

    def save_plan(self, value: AutomatedCandidateSearchPlan) -> AutomatedCandidateSearchPlan:
        prior = self.plan_by_idempotency(value.idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != value.canonical_input_hash:
                raise CandidateGenerationConflict("SEARCH_PLAN_IDEMPOTENCY_CONFLICT")
            return prior
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    automated_candidate_search_plan.insert().values(**_plan_values(value))
                )
        except sa.exc.IntegrityError as error:
            raise CandidateGenerationConflict("SEARCH_PLAN_CONFLICT") from error
        return value

    def update_plan(self, value: AutomatedCandidateSearchPlan) -> AutomatedCandidateSearchPlan:
        with self._engine.begin() as connection:
            result = connection.execute(
                automated_candidate_search_plan.update()
                .where(
                    automated_candidate_search_plan.c.search_plan_id == value.search_plan_id,
                    automated_candidate_search_plan.c.canonical_input_hash
                    == value.canonical_input_hash,
                )
                .values(
                    state=value.state.value,
                    completed_at=value.completed_at,
                    canceled_at=value.canceled_at,
                    canonical_result_hash=value.canonical_result_hash,
                )
            )
            if result.rowcount != 1:
                raise CandidateGenerationConflict("SEARCH_PLAN_STATE_CONFLICT")
        return value

    def run_by_idempotency(
        self, search_plan_id: str, key: str
    ) -> AutomatedCandidateSearchRun | None:
        return self._one_run(
            sa.and_(
                automated_candidate_search_run.c.search_plan_id == search_plan_id,
                automated_candidate_search_run.c.idempotency_key == key,
            )
        )

    def runs(self, search_plan_id: str | None = None) -> tuple[AutomatedCandidateSearchRun, ...]:
        statement = sa.select(automated_candidate_search_run)
        if search_plan_id is not None:
            statement = statement.where(
                automated_candidate_search_run.c.search_plan_id == search_plan_id
            )
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    statement.order_by(
                        automated_candidate_search_run.c.started_at,
                        automated_candidate_search_run.c.search_run_id,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_run(row) for row in rows)

    def run(self, search_run_id: str) -> AutomatedCandidateSearchRun | None:
        return self._one_run(automated_candidate_search_run.c.search_run_id == search_run_id)

    def _one_run(self, clause: Any) -> AutomatedCandidateSearchRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(sa.select(automated_candidate_search_run).where(clause))
                .mappings()
                .one_or_none()
            )
        return _run(row) if row is not None else None

    def save_run(self, value: AutomatedCandidateSearchRun) -> AutomatedCandidateSearchRun:
        prior = self.run_by_idempotency(value.search_plan_id, value.idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != value.canonical_input_hash:
                raise CandidateGenerationConflict("SEARCH_RUN_IDEMPOTENCY_CONFLICT")
            return prior
        with self._engine.begin() as connection:
            connection.execute(automated_candidate_search_run.insert().values(**_run_values(value)))
        return value

    def update_run(self, value: AutomatedCandidateSearchRun) -> AutomatedCandidateSearchRun:
        updates = _run_values(value)
        for immutable in (
            "search_run_id",
            "search_plan_id",
            "search_plan_hash",
            "experiment_id",
            "experiment_hash",
            "optuna_study_identity",
            "optuna_version",
            "mlflow_experiment_identity",
            "requested_trial_count",
            "started_at",
            "idempotency_key",
            "canonical_input_hash",
        ):
            updates.pop(immutable)
        with self._engine.begin() as connection:
            result = connection.execute(
                automated_candidate_search_run.update()
                .where(
                    automated_candidate_search_run.c.search_run_id == value.search_run_id,
                    automated_candidate_search_run.c.canonical_input_hash
                    == value.canonical_input_hash,
                )
                .values(**updates)
            )
            if result.rowcount != 1:
                raise CandidateGenerationConflict("SEARCH_RUN_STATE_CONFLICT")
        return value

    def save_binding(self, value: AutomatedCandidateTrialBinding) -> AutomatedCandidateTrialBinding:
        with self._engine.begin() as connection:
            retained = (
                connection.execute(
                    sa.select(automated_candidate_trial_binding).where(
                        automated_candidate_trial_binding.c.binding_id == value.binding_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if retained is not None:
                restored = _binding(retained)
                if restored.canonical_input_hash != value.canonical_input_hash:
                    raise CandidateGenerationConflict("SEARCH_BINDING_CONFLICT")
                return restored
            connection.execute(
                automated_candidate_trial_binding.insert().values(**_binding_values(value))
            )
        return value

    def update_binding(
        self, value: AutomatedCandidateTrialBinding
    ) -> AutomatedCandidateTrialBinding:
        updates = _binding_values(value)
        for immutable in (
            "binding_id",
            "search_run_id",
            "optuna_trial_number",
            "optimizer_token",
            "research_trial_id",
            "dataset_id",
            "feature_schema_id",
            "parameter_payload",
            "parameter_hash",
            "sampler_metadata",
            "canonical_input_hash",
        ):
            updates.pop(immutable)
        with self._engine.begin() as connection:
            result = connection.execute(
                automated_candidate_trial_binding.update()
                .where(
                    automated_candidate_trial_binding.c.binding_id == value.binding_id,
                    automated_candidate_trial_binding.c.canonical_input_hash
                    == value.canonical_input_hash,
                )
                .values(**updates)
            )
            if result.rowcount != 1:
                raise CandidateGenerationConflict("SEARCH_BINDING_STATE_CONFLICT")
        return value

    def bindings(self, search_run_id: str) -> tuple[AutomatedCandidateTrialBinding, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(automated_candidate_trial_binding)
                    .where(automated_candidate_trial_binding.c.search_run_id == search_run_id)
                    .order_by(automated_candidate_trial_binding.c.optuna_trial_number)
                )
                .mappings()
                .all()
            )
        return tuple(_binding(row) for row in rows)

    def save_workspace(self, value: MLflowMirrorWorkspace) -> MLflowMirrorWorkspace:
        prior = self.workspace(value.workspace_id)
        if prior is not None:
            if prior.canonical_configuration_hash != value.canonical_configuration_hash:
                raise CandidateGenerationConflict("MLFLOW_WORKSPACE_IMMUTABLE")
            return prior
        with self._engine.begin() as connection:
            connection.execute(mlflow_mirror_workspace.insert().values(**_workspace_values(value)))
        return value

    def workspace(self, workspace_id: str) -> MLflowMirrorWorkspace | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(mlflow_mirror_workspace).where(
                        mlflow_mirror_workspace.c.workspace_id == workspace_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _workspace(row) if row is not None else None

    def save_mirror_record(self, value: MLflowMirrorRecord) -> MLflowMirrorRecord:
        with self._engine.begin() as connection:
            retained = (
                connection.execute(
                    sa.select(mlflow_mirror_record).where(
                        mlflow_mirror_record.c.mirror_record_id == value.mirror_record_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if retained is None:
                connection.execute(mlflow_mirror_record.insert().values(**_mirror_values(value)))
                return value
            prior = _mirror(retained)
            if prior.canonical_input_hash != value.canonical_input_hash:
                raise CandidateGenerationConflict("MLFLOW_MIRROR_SOURCE_CONFLICT")
            updates = _mirror_values(value)
            updates.pop("mirror_record_id")
            updates.pop("source_type")
            updates.pop("source_identity")
            updates.pop("source_hash")
            updates.pop("workspace_id")
            updates.pop("mlflow_experiment_name")
            updates.pop("canonical_input_hash")
            connection.execute(
                mlflow_mirror_record.update()
                .where(mlflow_mirror_record.c.mirror_record_id == value.mirror_record_id)
                .values(**updates)
            )
        return value

    def mirror_records(self, experiment_id: str) -> tuple[MLflowMirrorRecord, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(mlflow_mirror_record)
                    .where(mlflow_mirror_record.c.mlflow_experiment_name == f"apex-{experiment_id}")
                    .order_by(mlflow_mirror_record.c.attempted_at)
                )
                .mappings()
                .all()
            )
        return tuple(_mirror(row) for row in rows)

    def save_decision(self, value: CandidateGenerationDecision) -> CandidateGenerationDecision:
        prior = self.decision(value.search_run_id)
        if prior is not None:
            if prior.canonical_result_hash != value.canonical_result_hash:
                raise CandidateGenerationConflict("CANDIDATE_DECISION_IMMUTABLE")
            return prior
        with self._engine.begin() as connection:
            connection.execute(
                candidate_generation_decision.insert().values(**_decision_values(value))
            )
        return value

    def decision(self, search_run_id: str) -> CandidateGenerationDecision | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(candidate_generation_decision).where(
                        candidate_generation_decision.c.search_run_id == search_run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _decision(row) if row is not None else None


def _plan_values(value: AutomatedCandidateSearchPlan) -> dict[str, object]:
    return {
        "search_plan_id": value.search_plan_id,
        "experiment_id": value.experiment_id,
        "experiment_hash": value.experiment_hash,
        "display_name": value.display_name,
        "objective_identity": value.objective_identity.value,
        "objective_version": value.objective_version,
        "objective_direction": value.objective_direction.value,
        "permitted_model_families": list(value.permitted_model_families),
        "parameter_space_payload": _json(value.search_space_payload),
        "parameter_space_hash": value.search_space_hash,
        "theoretical_combinations": value.theoretical_combinations,
        "calibration_choices": list(value.calibration_choices),
        "feature_schema_id": value.feature_schema_id,
        "feature_schema_hash": value.feature_schema_hash,
        "dataset_id": value.dataset_id,
        "dataset_hash": value.dataset_hash,
        "fold_policy_id": value.fold_policy_id,
        "fold_policy_hash": value.fold_policy_hash,
        "random_seed": value.random_seed,
        "sampler_identity": value.sampler_identity,
        "sampler_version": value.sampler_version,
        "sampler_configuration": _json(value.sampler_configuration),
        "sampler_hash": value.sampler_hash,
        "pruner_identity": value.pruner_identity,
        "pruner_version": value.pruner_version,
        "pruner_configuration": _json(value.pruner_configuration),
        "pruner_hash": value.pruner_hash,
        "maximum_trials": value.maximum_trials,
        "maximum_failed_trials": value.maximum_failed_trials,
        "wall_clock_budget_seconds": value.wall_clock_budget_seconds,
        "per_trial_budget_seconds": value.per_trial_budget_seconds,
        "artifact_byte_budget": value.artifact_byte_budget,
        "concurrency_limit": value.concurrency_limit,
        "candidate_selection_policy_id": value.candidate_selection_policy_id,
        "candidate_selection_policy_hash": value.candidate_selection_policy_hash,
        "mlflow_mirror_policy_id": value.mlflow_mirror_policy_id,
        "mlflow_mirror_policy_hash": value.mlflow_mirror_policy_hash,
        "state": value.state.value,
        "created_at": value.created_at,
        "opened_at": value.opened_at,
        "completed_at": value.completed_at,
        "canceled_at": value.canceled_at,
        "operator_action_id": value.operator_action_id,
        "operator_reason": value.operator_reason,
        "idempotency_key": value.idempotency_key,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _plan(row: RowMapping) -> AutomatedCandidateSearchPlan:
    return AutomatedCandidateSearchPlan(
        search_plan_id=cast(str, row["search_plan_id"]),
        experiment_id=cast(str, row["experiment_id"]),
        experiment_hash=cast(str, row["experiment_hash"]),
        display_name=cast(str, row["display_name"]),
        objective_identity=ObjectiveIdentity(cast(str, row["objective_identity"])),
        objective_version=cast(str, row["objective_version"]),
        objective_direction=ObjectiveDirection(cast(str, row["objective_direction"])),
        permitted_model_families=tuple(cast(list[str], row["permitted_model_families"])),
        search_space_payload=_sequence(cast(str, row["parameter_space_payload"])),
        search_space_hash=cast(str, row["parameter_space_hash"]),
        theoretical_combinations=cast(int, row["theoretical_combinations"]),
        calibration_choices=tuple(cast(list[str], row["calibration_choices"])),
        feature_schema_id=cast(str, row["feature_schema_id"]),
        feature_schema_hash=cast(str, row["feature_schema_hash"]),
        dataset_id=cast(str, row["dataset_id"]),
        dataset_hash=cast(str, row["dataset_hash"]),
        fold_policy_id=cast(str, row["fold_policy_id"]),
        fold_policy_hash=cast(str, row["fold_policy_hash"]),
        random_seed=cast(int, row["random_seed"]),
        sampler_identity=cast(str, row["sampler_identity"]),
        sampler_version=cast(str, row["sampler_version"]),
        sampler_configuration=_mapping(cast(str, row["sampler_configuration"])),
        sampler_hash=cast(str, row["sampler_hash"]),
        pruner_identity=cast(str, row["pruner_identity"]),
        pruner_version=cast(str, row["pruner_version"]),
        pruner_configuration=_mapping(cast(str, row["pruner_configuration"])),
        pruner_hash=cast(str, row["pruner_hash"]),
        maximum_trials=cast(int, row["maximum_trials"]),
        maximum_failed_trials=cast(int, row["maximum_failed_trials"]),
        wall_clock_budget_seconds=cast(int, row["wall_clock_budget_seconds"]),
        per_trial_budget_seconds=cast(int, row["per_trial_budget_seconds"]),
        artifact_byte_budget=cast(int, row["artifact_byte_budget"]),
        concurrency_limit=cast(int, row["concurrency_limit"]),
        candidate_selection_policy_id=cast(str, row["candidate_selection_policy_id"]),
        candidate_selection_policy_hash=cast(str, row["candidate_selection_policy_hash"]),
        mlflow_mirror_policy_id=cast(str, row["mlflow_mirror_policy_id"]),
        mlflow_mirror_policy_hash=cast(str, row["mlflow_mirror_policy_hash"]),
        state=SearchPlanState(cast(str, row["state"])),
        created_at=cast(datetime, row["created_at"]),
        opened_at=cast(datetime, row["opened_at"]),
        completed_at=cast(datetime | None, row["completed_at"]),
        canceled_at=cast(datetime | None, row["canceled_at"]),
        operator_action_id=cast(str, row["operator_action_id"]),
        operator_reason=cast(str, row["operator_reason"]),
        idempotency_key=cast(str, row["idempotency_key"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
    )


def _run_values(value: AutomatedCandidateSearchRun) -> dict[str, object]:
    return {
        "search_run_id": value.search_run_id,
        "search_plan_id": value.search_plan_id,
        "search_plan_hash": value.search_plan_hash,
        "experiment_id": value.experiment_id,
        "experiment_hash": value.experiment_hash,
        "optuna_study_identity": value.optuna_study_identity,
        "optuna_version": value.optuna_version,
        "mlflow_experiment_identity": value.mlflow_experiment_identity,
        "requested_trial_count": value.requested_trial_count,
        "reserved_trial_count": value.reserved_trial_count,
        "completed_trial_count": value.completed_trial_count,
        "pruned_trial_count": value.pruned_trial_count,
        "failed_trial_count": value.failed_trial_count,
        "integrity_rejected_trial_count": value.integrity_rejected_trial_count,
        "elapsed_seconds": value.elapsed_seconds,
        "artifact_bytes_written": value.artifact_bytes_written,
        "state": value.state.value,
        "best_raw_trial_id": value.best_raw_trial_id,
        "best_integrity_eligible_trial_id": value.best_integrity_eligible_trial_id,
        "multiple_testing_evaluation_id": value.multiple_testing_evaluation_id,
        "final_recommendation": (
            value.final_recommendation.value if value.final_recommendation is not None else None
        ),
        "reason_codes": list(value.reason_codes),
        "started_at": value.started_at,
        "completed_at": value.completed_at,
        "idempotency_key": value.idempotency_key,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _run(row: RowMapping) -> AutomatedCandidateSearchRun:
    recommendation = cast(str | None, row["final_recommendation"])
    return AutomatedCandidateSearchRun(
        search_run_id=cast(str, row["search_run_id"]),
        search_plan_id=cast(str, row["search_plan_id"]),
        search_plan_hash=cast(str, row["search_plan_hash"]),
        experiment_id=cast(str, row["experiment_id"]),
        experiment_hash=cast(str, row["experiment_hash"]),
        optuna_study_identity=cast(str, row["optuna_study_identity"]),
        optuna_version=cast(str, row["optuna_version"]),
        mlflow_experiment_identity=cast(str | None, row["mlflow_experiment_identity"]),
        requested_trial_count=cast(int, row["requested_trial_count"]),
        reserved_trial_count=cast(int, row["reserved_trial_count"]),
        completed_trial_count=cast(int, row["completed_trial_count"]),
        pruned_trial_count=cast(int, row["pruned_trial_count"]),
        failed_trial_count=cast(int, row["failed_trial_count"]),
        integrity_rejected_trial_count=cast(int, row["integrity_rejected_trial_count"]),
        elapsed_seconds=cast(Decimal, row["elapsed_seconds"]),
        artifact_bytes_written=cast(int, row["artifact_bytes_written"]),
        state=SearchRunState(cast(str, row["state"])),
        best_raw_trial_id=cast(str | None, row["best_raw_trial_id"]),
        best_integrity_eligible_trial_id=cast(str | None, row["best_integrity_eligible_trial_id"]),
        multiple_testing_evaluation_id=cast(str | None, row["multiple_testing_evaluation_id"]),
        final_recommendation=(
            CandidateGenerationRecommendation(recommendation)
            if recommendation is not None
            else None
        ),
        reason_codes=tuple(cast(list[str], row["reason_codes"])),
        started_at=cast(datetime, row["started_at"]),
        completed_at=cast(datetime | None, row["completed_at"]),
        idempotency_key=cast(str, row["idempotency_key"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str | None, row["canonical_result_hash"]),
    )


def _binding_values(value: AutomatedCandidateTrialBinding) -> dict[str, object]:
    return {
        "binding_id": value.binding_id,
        "search_run_id": value.search_run_id,
        "optuna_trial_number": value.optuna_trial_number,
        "optimizer_token": value.optimizer_token,
        "research_trial_id": value.research_trial_id,
        "training_run_id": value.training_run_id,
        "evaluation_id": value.evaluation_id,
        "candidate_id": value.candidate_id,
        "dataset_id": value.dataset_id,
        "feature_schema_id": value.feature_schema_id,
        "parameter_payload": _json(value.parameter_payload),
        "parameter_hash": value.parameter_hash,
        "sampler_metadata": _json(value.sampler_metadata),
        "objective_value": value.objective_value,
        "objective_state": value.objective_state.value,
        "artifact_identities": list(value.artifact_identities),
        "artifact_hashes": list(value.artifact_hashes),
        "mlflow_mirror_record_id": value.mlflow_mirror_record_id,
        "failure_reason": value.failure_reason,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _binding(row: RowMapping) -> AutomatedCandidateTrialBinding:
    return AutomatedCandidateTrialBinding(
        binding_id=cast(str, row["binding_id"]),
        search_run_id=cast(str, row["search_run_id"]),
        optuna_trial_number=cast(int, row["optuna_trial_number"]),
        optimizer_token=cast(str, row["optimizer_token"]),
        research_trial_id=cast(str, row["research_trial_id"]),
        training_run_id=cast(str | None, row["training_run_id"]),
        evaluation_id=cast(str | None, row["evaluation_id"]),
        candidate_id=cast(str | None, row["candidate_id"]),
        dataset_id=cast(str, row["dataset_id"]),
        feature_schema_id=cast(str, row["feature_schema_id"]),
        parameter_payload=_mapping(cast(str, row["parameter_payload"])),
        parameter_hash=cast(str, row["parameter_hash"]),
        sampler_metadata=_mapping(cast(str, row["sampler_metadata"])),
        objective_value=cast(Decimal | None, row["objective_value"]),
        objective_state=ObjectiveState(cast(str, row["objective_state"])),
        artifact_identities=tuple(cast(list[str], row["artifact_identities"])),
        artifact_hashes=tuple(cast(list[str], row["artifact_hashes"])),
        mlflow_mirror_record_id=cast(str | None, row["mlflow_mirror_record_id"]),
        failure_reason=cast(str | None, row["failure_reason"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str | None, row["canonical_result_hash"]),
    )


def _workspace_values(value: MLflowMirrorWorkspace) -> dict[str, object]:
    return {
        "workspace_id": value.workspace_id,
        "workspace_version": value.workspace_version,
        "tracking_mode": value.tracking_mode,
        "loopback_bind_host": value.loopback_bind_host,
        "backend_relative_path": value.backend_relative_path,
        "artifact_relative_root": value.artifact_relative_root,
        "experiment_naming_policy": value.experiment_naming_policy,
        "mlflow_version": value.mlflow_version,
        "state": value.state.value,
        "canonical_configuration_hash": value.canonical_configuration_hash,
        "created_at": value.created_at,
    }


def _workspace(row: RowMapping) -> MLflowMirrorWorkspace:
    return MLflowMirrorWorkspace(
        workspace_id=cast(str, row["workspace_id"]),
        workspace_version=cast(str, row["workspace_version"]),
        tracking_mode=cast(str, row["tracking_mode"]),
        loopback_bind_host=cast(str, row["loopback_bind_host"]),
        backend_relative_path=cast(str, row["backend_relative_path"]),
        artifact_relative_root=cast(str, row["artifact_relative_root"]),
        experiment_naming_policy=cast(str, row["experiment_naming_policy"]),
        mlflow_version=cast(str, row["mlflow_version"]),
        state=MirrorWorkspaceState(cast(str, row["state"])),
        canonical_configuration_hash=cast(str, row["canonical_configuration_hash"]),
        created_at=cast(datetime, row["created_at"]),
    )


def _mirror_values(value: MLflowMirrorRecord) -> dict[str, object]:
    return {
        "mirror_record_id": value.mirror_record_id,
        "source_type": value.source_type,
        "source_identity": value.source_identity,
        "source_hash": value.source_hash,
        "workspace_id": value.workspace_id,
        "mlflow_experiment_id": value.mlflow_experiment_id,
        "mlflow_experiment_name": value.mlflow_experiment_name,
        "mlflow_run_id": value.mlflow_run_id,
        "mirrored_parameter_hash": value.mirrored_parameter_hash,
        "mirrored_metric_hash": value.mirrored_metric_hash,
        "mirrored_artifact_set_hash": value.mirrored_artifact_set_hash,
        "state": value.state.value,
        "attempted_at": value.attempted_at,
        "completed_at": value.completed_at,
        "failure_code": value.failure_code,
        "failure_message": value.failure_message,
        "retry_count": value.retry_count,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _mirror(row: RowMapping) -> MLflowMirrorRecord:
    return MLflowMirrorRecord(
        mirror_record_id=cast(str, row["mirror_record_id"]),
        source_type=cast(str, row["source_type"]),
        source_identity=cast(str, row["source_identity"]),
        source_hash=cast(str, row["source_hash"]),
        workspace_id=cast(str, row["workspace_id"]),
        mlflow_experiment_id=cast(str | None, row["mlflow_experiment_id"]),
        mlflow_experiment_name=cast(str, row["mlflow_experiment_name"]),
        mlflow_run_id=cast(str | None, row["mlflow_run_id"]),
        mirrored_parameter_hash=cast(str, row["mirrored_parameter_hash"]),
        mirrored_metric_hash=cast(str, row["mirrored_metric_hash"]),
        mirrored_artifact_set_hash=cast(str, row["mirrored_artifact_set_hash"]),
        state=MirrorRecordState(cast(str, row["state"])),
        attempted_at=cast(datetime, row["attempted_at"]),
        completed_at=cast(datetime | None, row["completed_at"]),
        failure_code=cast(str | None, row["failure_code"]),
        failure_message=cast(str | None, row["failure_message"]),
        retry_count=cast(int, row["retry_count"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
    )


def _decision_values(value: CandidateGenerationDecision) -> dict[str, object]:
    return {
        "decision_id": value.decision_id,
        "search_run_id": value.search_run_id,
        "candidate_trial_ids": list(value.candidate_trial_ids),
        "candidate_trial_set_hash": value.candidate_trial_set_hash,
        "raw_best_trial_id": value.raw_best_trial_id,
        "integrity_eligible_trial_ids": list(value.integrity_eligible_trial_ids),
        "selected_candidate_trial_id": value.selected_candidate_trial_id,
        "selection_metric": value.selection_metric,
        "multiple_testing_evaluation_id": value.multiple_testing_evaluation_id,
        "conformance_state": value.conformance_state,
        "integrity_policy_state": value.integrity_policy_state,
        "recommendation": value.recommendation.value,
        "hard_gate_reason_codes": list(value.hard_gate_reason_codes),
        "operator_action_id": value.operator_action_id,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
        "created_at": value.created_at,
    }


def _decision(row: RowMapping) -> CandidateGenerationDecision:
    return CandidateGenerationDecision(
        decision_id=cast(str, row["decision_id"]),
        search_run_id=cast(str, row["search_run_id"]),
        candidate_trial_ids=tuple(cast(list[str], row["candidate_trial_ids"])),
        candidate_trial_set_hash=cast(str, row["candidate_trial_set_hash"]),
        raw_best_trial_id=cast(str | None, row["raw_best_trial_id"]),
        integrity_eligible_trial_ids=tuple(cast(list[str], row["integrity_eligible_trial_ids"])),
        selected_candidate_trial_id=cast(str | None, row["selected_candidate_trial_id"]),
        selection_metric=cast(str, row["selection_metric"]),
        multiple_testing_evaluation_id=cast(str | None, row["multiple_testing_evaluation_id"]),
        conformance_state=cast(str, row["conformance_state"]),
        integrity_policy_state=cast(str, row["integrity_policy_state"]),
        recommendation=CandidateGenerationRecommendation(cast(str, row["recommendation"])),
        hard_gate_reason_codes=tuple(cast(list[str], row["hard_gate_reason_codes"])),
        operator_action_id=cast(str | None, row["operator_action_id"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
        created_at=cast(datetime, row["created_at"]),
    )
