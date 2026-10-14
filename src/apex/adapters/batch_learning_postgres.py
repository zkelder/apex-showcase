"""PostgreSQL repository for bounded batch-learning orchestration."""

from __future__ import annotations

import json
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.postgres import (
    batch_learning_cycle,
    batch_learning_cycle_binding,
    batch_learning_decision,
    batch_learning_evidence_window,
    batch_learning_plan,
    forward_shadow_evaluation_window,
)
from apex.application.batch_learning import (
    BatchLearningConflict,
    BatchLearningCycle,
    BatchLearningCycleBinding,
    BatchLearningCycleState,
    BatchLearningDecision,
    BatchLearningDecisionStage,
    BatchLearningEvidenceWindow,
    BatchLearningPlan,
    BatchLearningPlanState,
    BatchLearningRecommendation,
    BatchLearningRepository,
    BatchLearningTriggerType,
    EvidenceWindowState,
    ForwardShadowEvaluationWindow,
    ForwardWindowState,
)
from apex.domain.values import canonical_json_bytes


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _mapping(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise BatchLearningConflict("BATCH_LEARNING_JSON_OBJECT_REQUIRED")
    return cast(dict[str, object], parsed)


class PostgresBatchLearningRepository(BatchLearningRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def plans(self) -> tuple[BatchLearningPlan, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(batch_learning_plan).order_by(
                        batch_learning_plan.c.created_at,
                        batch_learning_plan.c.plan_id,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_plan(row) for row in rows)

    def plan(self, plan_id: str) -> BatchLearningPlan | None:
        return self._one_plan(batch_learning_plan.c.plan_id == plan_id)

    def plan_by_idempotency(self, key: str) -> BatchLearningPlan | None:
        return self._one_plan(batch_learning_plan.c.idempotency_key == key)

    def _one_plan(self, clause: Any) -> BatchLearningPlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(sa.select(batch_learning_plan).where(clause))
                .mappings()
                .one_or_none()
            )
        return _plan(row) if row is not None else None

    def save_plan(self, value: BatchLearningPlan) -> BatchLearningPlan:
        prior = self.plan_by_idempotency(value.idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != value.canonical_input_hash:
                raise BatchLearningConflict("BATCH_LEARNING_PLAN_IDEMPOTENCY_CONFLICT")
            return prior
        try:
            with self._engine.begin() as connection:
                connection.execute(batch_learning_plan.insert().values(**_plan_values(value)))
        except sa.exc.IntegrityError as error:
            raise BatchLearningConflict("BATCH_LEARNING_PLAN_CONFLICT") from error
        return value

    def update_plan(self, value: BatchLearningPlan) -> BatchLearningPlan:
        updates = _plan_values(value)
        for immutable in _PLAN_IMMUTABLE:
            updates.pop(immutable)
        with self._engine.begin() as connection:
            result = connection.execute(
                batch_learning_plan.update()
                .where(
                    batch_learning_plan.c.plan_id == value.plan_id,
                    batch_learning_plan.c.canonical_input_hash == value.canonical_input_hash,
                )
                .values(**updates)
            )
            if result.rowcount != 1:
                raise BatchLearningConflict("BATCH_LEARNING_PLAN_STATE_CONFLICT")
        return value

    def save_evidence_window(
        self, value: BatchLearningEvidenceWindow
    ) -> BatchLearningEvidenceWindow:
        prior = self.evidence_window(value.window_id)
        if prior is not None:
            if prior.canonical_input_hash != value.canonical_input_hash:
                raise BatchLearningConflict("BATCH_EVIDENCE_WINDOW_CONFLICT")
            return prior
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    batch_learning_evidence_window.insert().values(**_evidence_values(value))
                )
        except sa.exc.IntegrityError as error:
            raise BatchLearningConflict("BATCH_EVIDENCE_WATERMARK_CONFLICT") from error
        return value

    def evidence_window(self, window_id: str) -> BatchLearningEvidenceWindow | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(batch_learning_evidence_window).where(
                        batch_learning_evidence_window.c.window_id == window_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _evidence(row) if row is not None else None

    def evidence_windows(self, plan_id: str) -> tuple[BatchLearningEvidenceWindow, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(batch_learning_evidence_window)
                    .where(batch_learning_evidence_window.c.plan_id == plan_id)
                    .order_by(
                        batch_learning_evidence_window.c.cutoff_at,
                        batch_learning_evidence_window.c.window_id,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_evidence(row) for row in rows)

    def cycle_by_idempotency(self, plan_id: str, key: str) -> BatchLearningCycle | None:
        return self._one_cycle(
            sa.and_(
                batch_learning_cycle.c.plan_id == plan_id,
                batch_learning_cycle.c.idempotency_key == key,
            )
        )

    def save_cycle(self, value: BatchLearningCycle) -> BatchLearningCycle:
        prior = self.cycle_by_idempotency(value.plan_id, value.idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != value.canonical_input_hash:
                raise BatchLearningConflict("BATCH_LEARNING_CYCLE_IDEMPOTENCY_CONFLICT")
            return prior
        try:
            with self._engine.begin() as connection:
                connection.execute(batch_learning_cycle.insert().values(**_cycle_values(value)))
        except sa.exc.IntegrityError as error:
            raise BatchLearningConflict("BATCH_LEARNING_CYCLE_CONFLICT") from error
        return value

    def update_cycle(self, value: BatchLearningCycle) -> BatchLearningCycle:
        updates = _cycle_values(value)
        for immutable in _CYCLE_IMMUTABLE:
            updates.pop(immutable)
        with self._engine.begin() as connection:
            result = connection.execute(
                batch_learning_cycle.update()
                .where(
                    batch_learning_cycle.c.cycle_id == value.cycle_id,
                    batch_learning_cycle.c.canonical_input_hash == value.canonical_input_hash,
                )
                .values(**updates)
            )
            if result.rowcount != 1:
                raise BatchLearningConflict("BATCH_LEARNING_CYCLE_STATE_CONFLICT")
        return value

    def cycle(self, cycle_id: str) -> BatchLearningCycle | None:
        return self._one_cycle(batch_learning_cycle.c.cycle_id == cycle_id)

    def _one_cycle(self, clause: Any) -> BatchLearningCycle | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(sa.select(batch_learning_cycle).where(clause))
                .mappings()
                .one_or_none()
            )
        return _cycle(row) if row is not None else None

    def cycles(self, plan_id: str) -> tuple[BatchLearningCycle, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(batch_learning_cycle)
                    .where(batch_learning_cycle.c.plan_id == plan_id)
                    .order_by(batch_learning_cycle.c.ordinal)
                )
                .mappings()
                .all()
            )
        return tuple(_cycle(row) for row in rows)

    def save_binding(self, value: BatchLearningCycleBinding) -> BatchLearningCycleBinding:
        prior = self.binding(value.cycle_id)
        if prior is None:
            with self._engine.begin() as connection:
                connection.execute(
                    batch_learning_cycle_binding.insert().values(**_binding_values(value))
                )
            return value
        if (
            prior.binding_id != value.binding_id
            or prior.evidence_window_id != value.evidence_window_id
            or prior.dataset_id != value.dataset_id
            or prior.experiment_id != value.experiment_id
            or prior.research_trial_ids != value.research_trial_ids
            or prior.search_plan_id != value.search_plan_id
            or prior.search_run_id != value.search_run_id
            or prior.candidate_ids != value.candidate_ids
            or prior.candidate_decision_id != value.candidate_decision_id
        ):
            raise BatchLearningConflict("BATCH_LEARNING_BINDING_IMMUTABLE")
        with self._engine.begin() as connection:
            connection.execute(
                batch_learning_cycle_binding.update()
                .where(batch_learning_cycle_binding.c.cycle_id == value.cycle_id)
                .values(
                    shadow_review_id=value.shadow_review_id,
                    approved_deployment_id=value.approved_deployment_id,
                    inference_batch_ids=list(value.inference_batch_ids),
                    forward_evaluation_ids=list(value.forward_evaluation_ids),
                    challenger_evaluation_id=value.challenger_evaluation_id,
                    mlflow_record_ids=list(value.mlflow_record_ids),
                    canonical_binding_hash=value.canonical_binding_hash,
                )
            )
        return value

    def binding(self, cycle_id: str) -> BatchLearningCycleBinding | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(batch_learning_cycle_binding).where(
                        batch_learning_cycle_binding.c.cycle_id == cycle_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _binding(row) if row is not None else None

    def save_forward_window(
        self, value: ForwardShadowEvaluationWindow
    ) -> ForwardShadowEvaluationWindow:
        prior = self.forward_window(value.window_id)
        if prior is not None:
            if prior.canonical_input_hash != value.canonical_input_hash:
                raise BatchLearningConflict("FORWARD_WINDOW_CONFLICT")
            return prior
        with self._engine.begin() as connection:
            connection.execute(
                forward_shadow_evaluation_window.insert().values(**_forward_values(value))
            )
        return value

    def update_forward_window(
        self, value: ForwardShadowEvaluationWindow
    ) -> ForwardShadowEvaluationWindow:
        updates = _forward_values(value)
        for immutable in _FORWARD_IMMUTABLE:
            updates.pop(immutable)
        with self._engine.begin() as connection:
            result = connection.execute(
                forward_shadow_evaluation_window.update()
                .where(
                    forward_shadow_evaluation_window.c.window_id == value.window_id,
                    forward_shadow_evaluation_window.c.canonical_input_hash
                    == value.canonical_input_hash,
                )
                .values(**updates)
            )
            if result.rowcount != 1:
                raise BatchLearningConflict("FORWARD_WINDOW_STATE_CONFLICT")
        return value

    def forward_window(self, window_id: str) -> ForwardShadowEvaluationWindow | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(forward_shadow_evaluation_window).where(
                        forward_shadow_evaluation_window.c.window_id == window_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _forward(row) if row is not None else None

    def save_decision(self, value: BatchLearningDecision) -> BatchLearningDecision:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(batch_learning_decision).where(
                        batch_learning_decision.c.decision_id == value.decision_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is not None:
            prior = _decision(row)
            if prior.canonical_result_hash != value.canonical_result_hash:
                raise BatchLearningConflict("BATCH_LEARNING_DECISION_IMMUTABLE")
            return prior
        with self._engine.begin() as connection:
            connection.execute(batch_learning_decision.insert().values(**_decision_values(value)))
        return value

    def decisions(self, cycle_id: str) -> tuple[BatchLearningDecision, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(batch_learning_decision)
                    .where(batch_learning_decision.c.cycle_id == cycle_id)
                    .order_by(
                        batch_learning_decision.c.created_at,
                        batch_learning_decision.c.decision_id,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_decision(row) for row in rows)


_PLAN_IMMUTABLE = (
    "plan_id",
    "name",
    "vertical",
    "research_family",
    "source_adapter_id",
    "source_adapter_version",
    "source_scope",
    "source_scope_hash",
    "feature_schema_id",
    "feature_schema_hash",
    "dataset_policy_id",
    "dataset_policy_hash",
    "experiment_template",
    "experiment_template_hash",
    "permitted_model_families",
    "search_template",
    "search_template_hash",
    "objective_policy_id",
    "objective_policy_hash",
    "fold_policy_id",
    "fold_policy_hash",
    "minimum_new_resolved_observations",
    "minimum_new_independent_groups",
    "maximum_observations_per_cycle",
    "maximum_trials_per_cycle",
    "maximum_failed_trials_per_cycle",
    "maximum_cycles",
    "minimum_cooldown_seconds",
    "drift_policy",
    "drift_policy_hash",
    "forward_policy",
    "forward_policy_hash",
    "champion_comparison_policy",
    "champion_comparison_policy_hash",
    "mlflow_mirror_policy_id",
    "mlflow_mirror_policy_hash",
    "concurrency_limit",
    "created_at",
    "operator_action_id",
    "idempotency_key",
    "canonical_input_hash",
)


def _plan_values(value: BatchLearningPlan) -> dict[str, object]:
    return {
        "plan_id": value.plan_id,
        "name": value.name,
        "vertical": value.vertical,
        "research_family": value.research_family,
        "source_adapter_id": value.source_adapter_id,
        "source_adapter_version": value.source_adapter_version,
        "source_scope": _json(value.source_scope),
        "source_scope_hash": value.source_scope_hash,
        "feature_schema_id": value.feature_schema_id,
        "feature_schema_hash": value.feature_schema_hash,
        "dataset_policy_id": value.dataset_policy_id,
        "dataset_policy_hash": value.dataset_policy_hash,
        "experiment_template": _json(value.experiment_template),
        "experiment_template_hash": value.experiment_template_hash,
        "permitted_model_families": list(value.permitted_model_families),
        "search_template": _json(value.search_template),
        "search_template_hash": value.search_template_hash,
        "objective_policy_id": value.objective_policy_id,
        "objective_policy_hash": value.objective_policy_hash,
        "fold_policy_id": value.fold_policy_id,
        "fold_policy_hash": value.fold_policy_hash,
        "minimum_new_resolved_observations": value.minimum_new_resolved_observations,
        "minimum_new_independent_groups": value.minimum_new_independent_groups,
        "maximum_observations_per_cycle": value.maximum_observations_per_cycle,
        "maximum_trials_per_cycle": value.maximum_trials_per_cycle,
        "maximum_failed_trials_per_cycle": value.maximum_failed_trials_per_cycle,
        "maximum_cycles": value.maximum_cycles,
        "minimum_cooldown_seconds": value.minimum_cooldown_seconds,
        "drift_policy": _json(value.drift_policy),
        "drift_policy_hash": value.drift_policy_hash,
        "forward_policy": _json(value.forward_policy),
        "forward_policy_hash": value.forward_policy_hash,
        "champion_comparison_policy": _json(value.champion_comparison_policy),
        "champion_comparison_policy_hash": value.champion_comparison_policy_hash,
        "mlflow_mirror_policy_id": value.mlflow_mirror_policy_id,
        "mlflow_mirror_policy_hash": value.mlflow_mirror_policy_hash,
        "concurrency_limit": value.concurrency_limit,
        "state": value.state.value,
        "created_at": value.created_at,
        "opened_at": value.opened_at,
        "paused_at": value.paused_at,
        "revoked_at": value.revoked_at,
        "completed_at": value.completed_at,
        "operator_action_id": value.operator_action_id,
        "idempotency_key": value.idempotency_key,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _plan(row: RowMapping) -> BatchLearningPlan:
    return BatchLearningPlan(
        plan_id=str(row["plan_id"]),
        name=str(row["name"]),
        vertical=str(row["vertical"]),
        research_family=str(row["research_family"]),
        source_adapter_id=str(row["source_adapter_id"]),
        source_adapter_version=str(row["source_adapter_version"]),
        source_scope=_mapping(str(row["source_scope"])),
        source_scope_hash=str(row["source_scope_hash"]),
        feature_schema_id=str(row["feature_schema_id"]),
        feature_schema_hash=str(row["feature_schema_hash"]),
        dataset_policy_id=str(row["dataset_policy_id"]),
        dataset_policy_hash=str(row["dataset_policy_hash"]),
        experiment_template=_mapping(str(row["experiment_template"])),
        experiment_template_hash=str(row["experiment_template_hash"]),
        permitted_model_families=tuple(row["permitted_model_families"]),
        search_template=_mapping(str(row["search_template"])),
        search_template_hash=str(row["search_template_hash"]),
        objective_policy_id=str(row["objective_policy_id"]),
        objective_policy_hash=str(row["objective_policy_hash"]),
        fold_policy_id=str(row["fold_policy_id"]),
        fold_policy_hash=str(row["fold_policy_hash"]),
        minimum_new_resolved_observations=int(row["minimum_new_resolved_observations"]),
        minimum_new_independent_groups=int(row["minimum_new_independent_groups"]),
        maximum_observations_per_cycle=int(row["maximum_observations_per_cycle"]),
        maximum_trials_per_cycle=int(row["maximum_trials_per_cycle"]),
        maximum_failed_trials_per_cycle=int(row["maximum_failed_trials_per_cycle"]),
        maximum_cycles=int(row["maximum_cycles"]),
        minimum_cooldown_seconds=int(row["minimum_cooldown_seconds"]),
        drift_policy=_mapping(str(row["drift_policy"])),
        drift_policy_hash=str(row["drift_policy_hash"]),
        forward_policy=_mapping(str(row["forward_policy"])),
        forward_policy_hash=str(row["forward_policy_hash"]),
        champion_comparison_policy=_mapping(str(row["champion_comparison_policy"])),
        champion_comparison_policy_hash=str(row["champion_comparison_policy_hash"]),
        mlflow_mirror_policy_id=str(row["mlflow_mirror_policy_id"]),
        mlflow_mirror_policy_hash=str(row["mlflow_mirror_policy_hash"]),
        concurrency_limit=int(row["concurrency_limit"]),
        state=BatchLearningPlanState(str(row["state"])),
        created_at=row["created_at"],
        opened_at=row["opened_at"],
        paused_at=row["paused_at"],
        revoked_at=row["revoked_at"],
        completed_at=row["completed_at"],
        operator_action_id=str(row["operator_action_id"]),
        idempotency_key=str(row["idempotency_key"]),
        canonical_input_hash=str(row["canonical_input_hash"]),
        canonical_result_hash=str(row["canonical_result_hash"]),
    )


def _evidence_values(value: BatchLearningEvidenceWindow) -> dict[str, object]:
    return {
        "window_id": value.window_id,
        "plan_id": value.plan_id,
        "prior_watermark": value.prior_watermark,
        "new_watermark": value.new_watermark,
        "cutoff_at": value.cutoff_at,
        "source_artifact_id": value.source_artifact_id,
        "source_artifact_hash": value.source_artifact_hash,
        "included_observation_count": value.included_observation_count,
        "included_independent_group_count": value.included_independent_group_count,
        "excluded_unresolved_count": value.excluded_unresolved_count,
        "excluded_duplicate_count": value.excluded_duplicate_count,
        "excluded_unavailable_count": value.excluded_unavailable_count,
        "excluded_integrity_count": value.excluded_integrity_count,
        "earliest_evidence_at": value.earliest_evidence_at,
        "latest_evidence_at": value.latest_evidence_at,
        "group_set_hash": value.group_set_hash,
        "observation_set_hash": value.observation_set_hash,
        "state": value.state.value,
        "reason_codes": list(value.reason_codes),
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _evidence(row: RowMapping) -> BatchLearningEvidenceWindow:
    return BatchLearningEvidenceWindow(
        str(row["window_id"]),
        str(row["plan_id"]),
        str(row["prior_watermark"]) if row["prior_watermark"] is not None else None,
        str(row["new_watermark"]),
        row["cutoff_at"],
        str(row["source_artifact_id"]),
        str(row["source_artifact_hash"]),
        int(row["included_observation_count"]),
        int(row["included_independent_group_count"]),
        int(row["excluded_unresolved_count"]),
        int(row["excluded_duplicate_count"]),
        int(row["excluded_unavailable_count"]),
        int(row["excluded_integrity_count"]),
        row["earliest_evidence_at"],
        row["latest_evidence_at"],
        str(row["group_set_hash"]),
        str(row["observation_set_hash"]),
        EvidenceWindowState(str(row["state"])),
        tuple(row["reason_codes"]),
        str(row["canonical_input_hash"]),
        str(row["canonical_result_hash"]),
    )


_CYCLE_IMMUTABLE = (
    "cycle_id",
    "plan_id",
    "ordinal",
    "trigger_type",
    "trigger_evidence",
    "trigger_evidence_hash",
    "evidence_window_id",
    "evidence_window_hash",
    "idempotency_key",
    "canonical_input_hash",
)


def _cycle_values(value: BatchLearningCycle) -> dict[str, object]:
    return {
        "cycle_id": value.cycle_id,
        "plan_id": value.plan_id,
        "ordinal": value.ordinal,
        "trigger_type": value.trigger_type.value,
        "trigger_evidence": _json(value.trigger_evidence),
        "trigger_evidence_hash": value.trigger_evidence_hash,
        "evidence_window_id": value.evidence_window_id,
        "evidence_window_hash": value.evidence_window_hash,
        "dataset_id": value.dataset_id,
        "dataset_hash": value.dataset_hash,
        "experiment_id": value.experiment_id,
        "experiment_hash": value.experiment_hash,
        "search_plan_id": value.search_plan_id,
        "search_plan_hash": value.search_plan_hash,
        "search_run_id": value.search_run_id,
        "candidate_decision_id": value.candidate_decision_id,
        "eligible_candidate_id": value.eligible_candidate_id,
        "approved_challenger_deployment_id": value.approved_challenger_deployment_id,
        "forward_window_id": value.forward_window_id,
        "state": value.state.value,
        "lease_owner": value.lease_owner,
        "lease_token": value.lease_token,
        "lease_expires_at": value.lease_expires_at,
        "started_at": value.started_at,
        "completed_at": value.completed_at,
        "canceled_at": value.canceled_at,
        "failure_code": value.failure_code,
        "reason_codes": list(value.reason_codes),
        "idempotency_key": value.idempotency_key,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _cycle(row: RowMapping) -> BatchLearningCycle:
    return BatchLearningCycle(
        str(row["cycle_id"]),
        str(row["plan_id"]),
        int(row["ordinal"]),
        BatchLearningTriggerType(str(row["trigger_type"])),
        _mapping(str(row["trigger_evidence"])),
        str(row["trigger_evidence_hash"]),
        str(row["evidence_window_id"]),
        str(row["evidence_window_hash"]),
        _optional(row["dataset_id"]),
        _optional(row["dataset_hash"]),
        _optional(row["experiment_id"]),
        _optional(row["experiment_hash"]),
        _optional(row["search_plan_id"]),
        _optional(row["search_plan_hash"]),
        _optional(row["search_run_id"]),
        _optional(row["candidate_decision_id"]),
        _optional(row["eligible_candidate_id"]),
        _optional(row["approved_challenger_deployment_id"]),
        _optional(row["forward_window_id"]),
        BatchLearningCycleState(str(row["state"])),
        _optional(row["lease_owner"]),
        _optional(row["lease_token"]),
        row["lease_expires_at"],
        row["started_at"],
        row["completed_at"],
        row["canceled_at"],
        _optional(row["failure_code"]),
        tuple(row["reason_codes"]),
        str(row["idempotency_key"]),
        str(row["canonical_input_hash"]),
        _optional(row["canonical_result_hash"]),
    )


def _optional(value: object) -> str | None:
    return str(value) if value is not None else None


def _binding_values(value: BatchLearningCycleBinding) -> dict[str, object]:
    return {
        "binding_id": value.binding_id,
        "cycle_id": value.cycle_id,
        "evidence_window_id": value.evidence_window_id,
        "dataset_id": value.dataset_id,
        "experiment_id": value.experiment_id,
        "research_trial_ids": list(value.research_trial_ids),
        "search_plan_id": value.search_plan_id,
        "search_run_id": value.search_run_id,
        "candidate_ids": list(value.candidate_ids),
        "candidate_decision_id": value.candidate_decision_id,
        "shadow_review_id": value.shadow_review_id,
        "approved_deployment_id": value.approved_deployment_id,
        "inference_batch_ids": list(value.inference_batch_ids),
        "forward_evaluation_ids": list(value.forward_evaluation_ids),
        "challenger_evaluation_id": value.challenger_evaluation_id,
        "mlflow_record_ids": list(value.mlflow_record_ids),
        "canonical_binding_hash": value.canonical_binding_hash,
    }


def _binding(row: RowMapping) -> BatchLearningCycleBinding:
    return BatchLearningCycleBinding(
        str(row["binding_id"]),
        str(row["cycle_id"]),
        str(row["evidence_window_id"]),
        str(row["dataset_id"]),
        str(row["experiment_id"]),
        tuple(row["research_trial_ids"]),
        str(row["search_plan_id"]),
        str(row["search_run_id"]),
        tuple(row["candidate_ids"]),
        str(row["candidate_decision_id"]),
        _optional(row["shadow_review_id"]),
        _optional(row["approved_deployment_id"]),
        tuple(row["inference_batch_ids"]),
        tuple(row["forward_evaluation_ids"]),
        _optional(row["challenger_evaluation_id"]),
        tuple(row["mlflow_record_ids"]),
        str(row["canonical_binding_hash"]),
    )


_FORWARD_IMMUTABLE = (
    "window_id",
    "cycle_id",
    "champion_deployment_id",
    "champion_deployment_hash",
    "challenger_deployment_id",
    "challenger_deployment_hash",
    "source_scope",
    "source_scope_hash",
    "window_start_at",
    "window_end_at",
    "minimum_predictions",
    "minimum_resolved_outcomes",
    "maximum_duration_seconds",
    "canonical_input_hash",
    "opened_at",
)


def _forward_values(value: ForwardShadowEvaluationWindow) -> dict[str, object]:
    return {
        "window_id": value.window_id,
        "cycle_id": value.cycle_id,
        "champion_deployment_id": value.champion_deployment_id,
        "champion_deployment_hash": value.champion_deployment_hash,
        "challenger_deployment_id": value.challenger_deployment_id,
        "challenger_deployment_hash": value.challenger_deployment_hash,
        "source_scope": _json(value.source_scope),
        "source_scope_hash": value.source_scope_hash,
        "window_start_at": value.window_start_at,
        "cutoff_at": value.cutoff_at,
        "window_end_at": value.window_end_at,
        "minimum_predictions": value.minimum_predictions,
        "minimum_resolved_outcomes": value.minimum_resolved_outcomes,
        "maximum_duration_seconds": value.maximum_duration_seconds,
        "champion_prediction_count": value.champion_prediction_count,
        "challenger_prediction_count": value.challenger_prediction_count,
        "champion_resolved_count": value.champion_resolved_count,
        "challenger_resolved_count": value.challenger_resolved_count,
        "unmatched_count": value.unmatched_count,
        "excluded_count": value.excluded_count,
        "outcome_set_hash": value.outcome_set_hash,
        "state": value.state.value,
        "evaluation_ids": list(value.evaluation_ids),
        "drift_diagnostic_ids": list(value.drift_diagnostic_ids),
        "challenger_evaluation_id": value.challenger_evaluation_id,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
        "opened_at": value.opened_at,
        "completed_at": value.completed_at,
    }


def _forward(row: RowMapping) -> ForwardShadowEvaluationWindow:
    return ForwardShadowEvaluationWindow(
        str(row["window_id"]),
        str(row["cycle_id"]),
        _optional(row["champion_deployment_id"]),
        _optional(row["champion_deployment_hash"]),
        str(row["challenger_deployment_id"]),
        str(row["challenger_deployment_hash"]),
        _mapping(str(row["source_scope"])),
        str(row["source_scope_hash"]),
        row["window_start_at"],
        row["cutoff_at"],
        row["window_end_at"],
        int(row["minimum_predictions"]),
        int(row["minimum_resolved_outcomes"]),
        int(row["maximum_duration_seconds"]),
        int(row["champion_prediction_count"]),
        int(row["challenger_prediction_count"]),
        int(row["champion_resolved_count"]),
        int(row["challenger_resolved_count"]),
        int(row["unmatched_count"]),
        int(row["excluded_count"]),
        _optional(row["outcome_set_hash"]),
        ForwardWindowState(str(row["state"])),
        tuple(row["evaluation_ids"]),
        tuple(row["drift_diagnostic_ids"]),
        _optional(row["challenger_evaluation_id"]),
        str(row["canonical_input_hash"]),
        _optional(row["canonical_result_hash"]),
        row["opened_at"],
        row["completed_at"],
    )


def _decision_values(value: BatchLearningDecision) -> dict[str, object]:
    return {
        "decision_id": value.decision_id,
        "cycle_id": value.cycle_id,
        "decision_stage": value.decision_stage.value,
        "evidence_ids": list(value.evidence_ids),
        "evidence_set_hash": value.evidence_set_hash,
        "candidate_generation_decision_id": value.candidate_generation_decision_id,
        "challenger_evaluation_id": value.challenger_evaluation_id,
        "champion_deployment_id": value.champion_deployment_id,
        "challenger_deployment_id": value.challenger_deployment_id,
        "research_integrity_state": value.research_integrity_state,
        "drift_state": value.drift_state,
        "recommendation": value.recommendation.value,
        "reason_codes": list(value.reason_codes),
        "operator_action_id": value.operator_action_id,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
        "created_at": value.created_at,
    }


def _decision(row: RowMapping) -> BatchLearningDecision:
    return BatchLearningDecision(
        str(row["decision_id"]),
        str(row["cycle_id"]),
        BatchLearningDecisionStage(str(row["decision_stage"])),
        tuple(row["evidence_ids"]),
        str(row["evidence_set_hash"]),
        _optional(row["candidate_generation_decision_id"]),
        _optional(row["challenger_evaluation_id"]),
        _optional(row["champion_deployment_id"]),
        _optional(row["challenger_deployment_id"]),
        str(row["research_integrity_state"]),
        _optional(row["drift_state"]),
        BatchLearningRecommendation(str(row["recommendation"])),
        tuple(row["reason_codes"]),
        _optional(row["operator_action_id"]),
        str(row["canonical_input_hash"]),
        str(row["canonical_result_hash"]),
        row["created_at"],
    )
