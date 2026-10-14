"""PostgreSQL metadata adapter for research-only Strategy Lab records.

This adapter persists reproducibility metadata only.  It has no access to a
campaign, orders, reservations, positions, ledger journals, or any authority
service.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.postgres import (
    strategy_lab_challenger_evaluation,
    strategy_lab_champion_assignment,
    strategy_lab_dataset_snapshot,
    strategy_lab_feature_schema,
    strategy_lab_forward_evaluation,
    strategy_lab_forward_prediction,
    strategy_lab_inference_batch,
    strategy_lab_label_definition,
    strategy_lab_model_candidate,
    strategy_lab_model_deployment,
    strategy_lab_operator_action,
    strategy_lab_shadow_review,
    strategy_lab_training_run,
    strategy_lab_walk_forward_evaluation,
)
from apex.application.strategy_lab import (
    CalibrationMethod,
    CandidateState,
    DatasetState,
    EvaluationState,
    ModelFamily,
    StrategyLabArtifact,
    StrategyLabDatasetSnapshot,
    StrategyLabError,
    StrategyLabFeatureDefinition,
    StrategyLabFeatureSchema,
    StrategyLabFold,
    StrategyLabLabelDefinition,
    StrategyLabModelCandidate,
    StrategyLabTrainingRun,
    StrategyLabWalkForwardEvaluation,
    TrainingRunState,
)
from apex.application.strategy_lab_shadow import (
    BatchState,
    ChallengerEvaluationState,
    ChallengerRecommendation,
    ChampionAssignmentState,
    DeploymentRole,
    DeploymentState,
    ForwardOutcomeState,
    ShadowReviewState,
    StrategyLabChallengerEvaluation,
    StrategyLabChampionAssignment,
    StrategyLabForwardEvaluation,
    StrategyLabForwardPrediction,
    StrategyLabInferenceBatch,
    StrategyLabModelDeployment,
    StrategyLabShadowOperatorAction,
    StrategyLabShadowReview,
)
from apex.domain.values import canonical_json_bytes, parse_decimal


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decode(value: str) -> Any:
    return json.loads(value)


class PostgresStrategyLabStore:
    """Narrow durable store for immutable research records."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def feature_schema(self, schema_id: str) -> StrategyLabFeatureSchema | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_feature_schema).where(
                        strategy_lab_feature_schema.c.schema_id == schema_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _schema(row) if row is not None else None

    def save_feature_schema(self, schema: StrategyLabFeatureSchema) -> StrategyLabFeatureSchema:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(strategy_lab_feature_schema.c.canonical_hash).where(
                    strategy_lab_feature_schema.c.schema_id == schema.schema_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing != schema.canonical_hash:
                    raise StrategyLabError("FEATURE_SCHEMA_IMMUTABLE")
                return schema
            connection.execute(
                strategy_lab_feature_schema.insert().values(
                    schema_id=schema.schema_id,
                    vertical=schema.vertical,
                    research_family=schema.research_family,
                    schema_version=schema.version,
                    feature_definitions_payload=_json(schema.features),
                    feature_names=list(schema.feature_names),
                    created_at=schema.created_at,
                    superseded_at=schema.superseded_at,
                    canonical_payload=_json(schema.canonical_payload),
                    canonical_hash=schema.canonical_hash,
                )
            )
        return schema

    def label_definition(self, label_id: str) -> StrategyLabLabelDefinition | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_label_definition).where(
                        strategy_lab_label_definition.c.label_id == label_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _label(row) if row is not None else None

    def save_label_definition(
        self, label: StrategyLabLabelDefinition
    ) -> StrategyLabLabelDefinition:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(strategy_lab_label_definition.c.canonical_hash).where(
                    strategy_lab_label_definition.c.label_id == label.label_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing != label.canonical_hash:
                    raise StrategyLabError("LABEL_DEFINITION_IMMUTABLE")
                return label
            connection.execute(
                strategy_lab_label_definition.insert().values(
                    label_id=label.label_id,
                    vertical=label.vertical,
                    research_family=label.research_family,
                    label_version=label.version,
                    target_type=label.target_type,
                    positive_semantics=label.positive_semantics,
                    negative_semantics=label.negative_semantics,
                    resolution_rule=label.resolution_rule,
                    feature_cutoff_rule=label.feature_cutoff_rule,
                    availability_rule=label.availability_rule,
                    created_at=label.created_at,
                    superseded_at=label.superseded_at,
                    canonical_payload=_json(label.canonical_payload),
                    canonical_hash=label.canonical_hash,
                )
            )
        return label

    def dataset_by_idempotency(
        self, adapter_id: str, key: str
    ) -> StrategyLabDatasetSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_dataset_snapshot).where(
                        strategy_lab_dataset_snapshot.c.adapter_id == adapter_id,
                        strategy_lab_dataset_snapshot.c.idempotency_key == key,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._dataset(row) if row is not None else None

    def save_dataset(self, dataset: StrategyLabDatasetSnapshot) -> StrategyLabDatasetSnapshot:
        with self._engine.begin() as connection:
            existing = (
                connection.execute(
                    sa.select(
                        strategy_lab_dataset_snapshot.c.canonical_input_hash,
                        strategy_lab_dataset_snapshot.c.state,
                    ).where(strategy_lab_dataset_snapshot.c.dataset_id == dataset.dataset_id)
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["canonical_input_hash"] != dataset.canonical_input_hash:
                    raise StrategyLabError("DATASET_IDEMPOTENCY_CONFLICT")
                if existing["state"] != dataset.state.value:
                    connection.execute(
                        strategy_lab_dataset_snapshot.update()
                        .where(strategy_lab_dataset_snapshot.c.dataset_id == dataset.dataset_id)
                        .values(**_dataset_values(dataset))
                    )
                return dataset
            connection.execute(
                strategy_lab_dataset_snapshot.insert().values(**_dataset_values(dataset))
            )
        return dataset

    def dataset(self, dataset_id: str) -> StrategyLabDatasetSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_dataset_snapshot).where(
                        strategy_lab_dataset_snapshot.c.dataset_id == dataset_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._dataset(row) if row is not None else None

    def training_by_idempotency(self, dataset_id: str, key: str) -> StrategyLabTrainingRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_training_run).where(
                        strategy_lab_training_run.c.dataset_id == dataset_id,
                        strategy_lab_training_run.c.idempotency_key == key,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _training(row) if row is not None else None

    def save_training(self, training: StrategyLabTrainingRun) -> StrategyLabTrainingRun:
        with self._engine.begin() as connection:
            existing = (
                connection.execute(
                    sa.select(
                        strategy_lab_training_run.c.canonical_input_hash,
                        strategy_lab_training_run.c.state,
                    ).where(strategy_lab_training_run.c.training_run_id == training.training_run_id)
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["canonical_input_hash"] != training.canonical_input_hash:
                    raise StrategyLabError("TRAINING_IDEMPOTENCY_CONFLICT")
                if existing["state"] != training.state.value:
                    connection.execute(
                        strategy_lab_training_run.update()
                        .where(
                            strategy_lab_training_run.c.training_run_id == training.training_run_id
                        )
                        .values(**_training_values(training))
                    )
                return training
            connection.execute(
                strategy_lab_training_run.insert().values(**_training_values(training))
            )
        return training

    def training(self, training_run_id: str) -> StrategyLabTrainingRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_training_run).where(
                        strategy_lab_training_run.c.training_run_id == training_run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _training(row) if row is not None else None

    def save_evaluation(
        self, evaluation: StrategyLabWalkForwardEvaluation
    ) -> StrategyLabWalkForwardEvaluation:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(strategy_lab_walk_forward_evaluation.c.canonical_input_hash).where(
                    strategy_lab_walk_forward_evaluation.c.evaluation_id == evaluation.evaluation_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing != evaluation.canonical_input_hash:
                    raise StrategyLabError("EVALUATION_IDEMPOTENCY_CONFLICT")
                return evaluation
            connection.execute(
                strategy_lab_walk_forward_evaluation.insert().values(
                    **_evaluation_values(evaluation)
                )
            )
        return evaluation

    def evaluation(self, evaluation_id: str) -> StrategyLabWalkForwardEvaluation | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_walk_forward_evaluation).where(
                        strategy_lab_walk_forward_evaluation.c.evaluation_id == evaluation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _evaluation(row) if row is not None else None

    def save_candidate(self, candidate: StrategyLabModelCandidate) -> StrategyLabModelCandidate:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(strategy_lab_model_candidate.c.canonical_candidate_hash).where(
                    strategy_lab_model_candidate.c.candidate_id == candidate.candidate_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing != candidate.canonical_candidate_hash:
                    raise StrategyLabError("CANDIDATE_IDEMPOTENCY_CONFLICT")
                return candidate
            connection.execute(
                strategy_lab_model_candidate.insert().values(**_candidate_values(candidate))
            )
        return candidate

    def candidate(self, candidate_id: str) -> StrategyLabModelCandidate | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_model_candidate).where(
                        strategy_lab_model_candidate.c.candidate_id == candidate_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _candidate(row) if row is not None else None

    def feature_schemas(self) -> tuple[StrategyLabFeatureSchema, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(strategy_lab_feature_schema).order_by(
                    strategy_lab_feature_schema.c.created_at
                )
            ).mappings()
            return tuple(_schema(row) for row in rows)

    def label_definitions(self) -> tuple[StrategyLabLabelDefinition, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(strategy_lab_label_definition).order_by(
                    strategy_lab_label_definition.c.created_at
                )
            ).mappings()
            return tuple(_label(row) for row in rows)

    def datasets(self) -> tuple[StrategyLabDatasetSnapshot, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(strategy_lab_dataset_snapshot).order_by(
                    strategy_lab_dataset_snapshot.c.created_at
                )
            ).mappings()
            return tuple(self._dataset(row) for row in rows)

    def training_runs(self) -> tuple[StrategyLabTrainingRun, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(strategy_lab_training_run).order_by(
                    strategy_lab_training_run.c.started_at
                )
            ).mappings()
            return tuple(_training(row) for row in rows)

    def candidates(self) -> tuple[StrategyLabModelCandidate, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(strategy_lab_model_candidate).order_by(
                    strategy_lab_model_candidate.c.created_at
                )
            ).mappings()
            return tuple(_candidate(row) for row in rows)

    def _dataset(self, row: RowMapping) -> StrategyLabDatasetSnapshot:
        schema = self.feature_schema(str(row["feature_schema_id"]))
        label = self.label_definition(str(row["label_definition_id"]))
        if schema is None or label is None:
            raise StrategyLabError("DATASET_DEFINITION_MISSING")
        return StrategyLabDatasetSnapshot(
            dataset_id=str(row["dataset_id"]),
            name=str(row["name"]),
            schema=schema,
            label_definition=label,
            adapter_id=str(row["adapter_id"]),
            adapter_version=str(row["adapter_version"]),
            cutoff_at=row["cutoff_at"],
            source_identity=str(row["source_identity"]),
            artifact=(
                StrategyLabArtifact(
                    str(row["artifact_relative_location"]),
                    str(row["artifact_sha256"]),
                    int(row["artifact_byte_size"]),
                )
                if row["artifact_relative_location"] is not None
                else None
            ),
            row_count=int(row["row_count"]),
            entity_count=int(row["entity_count"]),
            grouping_count=int(row["grouping_count"]),
            feature_first_at=row["feature_first_at"],
            feature_last_at=row["feature_last_at"],
            label_first_at=row["label_first_at"],
            label_last_at=row["label_last_at"],
            state=DatasetState(str(row["state"])),
            integrity_warnings=tuple(row["integrity_warning_codes"]),
            idempotency_key=str(row["idempotency_key"]),
            canonical_input_hash=str(row["canonical_input_hash"]),
            canonical_result_hash=(
                str(row["canonical_result_hash"])
                if row["canonical_result_hash"] is not None
                else None
            ),
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            failure_code=row["failure_code"],
        )


def _schema(row: RowMapping) -> StrategyLabFeatureSchema:
    return StrategyLabFeatureSchema(
        schema_id=str(row["schema_id"]),
        vertical=str(row["vertical"]),
        research_family=str(row["research_family"]),
        version=str(row["schema_version"]),
        features=tuple(
            StrategyLabFeatureDefinition(**item)
            for item in _decode(str(row["feature_definitions_payload"]))
        ),
        created_at=row["created_at"],
        superseded_at=row["superseded_at"],
    )


def _label(row: RowMapping) -> StrategyLabLabelDefinition:
    return StrategyLabLabelDefinition(
        label_id=str(row["label_id"]),
        vertical=str(row["vertical"]),
        research_family=str(row["research_family"]),
        version=str(row["label_version"]),
        target_type=str(row["target_type"]),
        positive_semantics=str(row["positive_semantics"]),
        negative_semantics=str(row["negative_semantics"]),
        resolution_rule=str(row["resolution_rule"]),
        feature_cutoff_rule=str(row["feature_cutoff_rule"]),
        availability_rule=str(row["availability_rule"]),
        created_at=row["created_at"],
        superseded_at=row["superseded_at"],
    )


def _dataset_values(item: StrategyLabDatasetSnapshot) -> dict[str, object]:
    return {
        "dataset_id": item.dataset_id,
        "name": item.name,
        "vertical": item.schema.vertical,
        "feature_schema_id": item.schema.schema_id,
        "feature_schema_hash": item.schema.canonical_hash,
        "label_definition_id": item.label_definition.label_id,
        "label_definition_hash": item.label_definition.canonical_hash,
        "adapter_id": item.adapter_id,
        "adapter_version": item.adapter_version,
        "cutoff_at": item.cutoff_at,
        "source_identity": item.source_identity,
        "artifact_provider": "LOCAL_MANAGED",
        "artifact_relative_location": (
            item.artifact.relative_location if item.artifact is not None else None
        ),
        "artifact_sha256": item.artifact.sha256 if item.artifact is not None else None,
        "artifact_byte_size": item.artifact.byte_size if item.artifact is not None else None,
        "row_count": item.row_count,
        "entity_count": item.entity_count,
        "grouping_count": item.grouping_count,
        "feature_first_at": item.feature_first_at,
        "feature_last_at": item.feature_last_at,
        "label_first_at": item.label_first_at,
        "label_last_at": item.label_last_at,
        "state": item.state.value,
        "integrity_warning_codes": list(item.integrity_warnings),
        "idempotency_key": item.idempotency_key,
        "canonical_input_hash": item.canonical_input_hash,
        "canonical_result_hash": item.canonical_result_hash,
        "created_at": item.created_at,
        "completed_at": item.completed_at,
        "failure_code": item.failure_code,
    }


def _training_values(item: StrategyLabTrainingRun) -> dict[str, object]:
    artifact = item.model_artifact
    return {
        "training_run_id": item.training_run_id,
        "dataset_id": item.dataset_id,
        "dataset_hash": item.dataset_hash,
        "model_family": item.model_family.value,
        "model_specification_version": item.model_specification_version,
        "hyperparameters_payload": _json(item.hyperparameters),
        "calibration_method": item.calibration.value,
        "random_seed": item.seed,
        "python_version": item.python_version,
        "sklearn_version": item.sklearn_version,
        "numpy_version": item.numpy_version,
        "runtime_identity": item.runtime_identity,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
        "state": item.state.value,
        "model_artifact_relative_location": artifact.relative_location if artifact else None,
        "model_artifact_sha256": artifact.sha256 if artifact else None,
        "model_artifact_byte_size": artifact.byte_size if artifact else None,
        "idempotency_key": item.idempotency_key,
        "canonical_input_hash": item.canonical_input_hash,
        "canonical_result_hash": item.canonical_result_hash,
        "failure_code": item.failure_code,
    }


def _training(row: RowMapping) -> StrategyLabTrainingRun:
    artifact = None
    if row["model_artifact_sha256"] is not None:
        artifact = StrategyLabArtifact(
            str(row["model_artifact_relative_location"]),
            str(row["model_artifact_sha256"]),
            int(row["model_artifact_byte_size"]),
        )
    return StrategyLabTrainingRun(
        training_run_id=str(row["training_run_id"]),
        dataset_id=str(row["dataset_id"]),
        dataset_hash=str(row["dataset_hash"]),
        model_family=ModelFamily(str(row["model_family"])),
        model_specification_version=str(row["model_specification_version"]),
        hyperparameters=_decode(str(row["hyperparameters_payload"])),
        calibration=CalibrationMethod(str(row["calibration_method"])),
        seed=int(row["random_seed"]),
        python_version=str(row["python_version"]),
        sklearn_version=str(row["sklearn_version"]),
        numpy_version=str(row["numpy_version"]),
        runtime_identity=str(row["runtime_identity"]),
        state=TrainingRunState(str(row["state"])),
        model_artifact=artifact,
        idempotency_key=str(row["idempotency_key"]),
        canonical_input_hash=str(row["canonical_input_hash"]),
        canonical_result_hash=row["canonical_result_hash"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        failure_code=row["failure_code"],
    )


def _evaluation_values(item: StrategyLabWalkForwardEvaluation) -> dict[str, object]:
    folds = tuple(
        {
            "ordinal": fold.ordinal,
            "training": fold.training_group_ids,
            "validation": fold.validation_group_ids,
            "test": fold.test_group_ids,
            "hash": fold.canonical_hash,
        }
        for fold in item.folds
    )
    return {
        "evaluation_id": item.evaluation_id,
        "training_run_id": item.training_run_id,
        "fold_policy_id": "GROUPED_WALK_FORWARD_V1",
        "fold_policy_version": "V1",
        "fold_definitions_payload": _json(folds),
        "fold_count_requested": item.requested_fold_count,
        "fold_count_completed": item.completed_fold_count,
        "grouping_identity": "EXACT_EVENT_IDENTITY",
        "baseline_identity": "MARKET_CONSENSUS",
        "prediction_count": item.prediction_count,
        "distinct_group_count": item.distinct_group_count,
        "point_brier_score": item.brier_score,
        "point_log_loss": item.log_loss,
        "baseline_brier_score": item.baseline_brier_score,
        "baseline_log_loss": item.baseline_log_loss,
        "brier_difference": item.brier_difference,
        "log_loss_difference": item.log_loss_difference,
        "calibration_summary": _json(item.calibration_summary),
        "probability_range_coverage": int(str(item.calibration_summary.get("bucket_count", 0))),
        "recent_fold_metrics": _json(item.recent_fold_metrics),
        "full_fold_metrics": _json(item.full_fold_metrics),
        "counterfactual_return_summary": None,
        "integrity_warning_codes": list(item.integrity_warnings),
        "skipped_fold_reasons": list(item.skipped_fold_reasons),
        "state": item.state.value,
        "canonical_input_hash": item.canonical_input_hash,
        "canonical_result_hash": item.canonical_result_hash,
        "created_at": item.created_at,
        "completed_at": item.completed_at,
    }


def _evaluation(row: RowMapping) -> StrategyLabWalkForwardEvaluation:
    folds = tuple(
        StrategyLabFold(
            ordinal=int(item["ordinal"]),
            training_group_ids=tuple(item["training"]),
            validation_group_ids=tuple(item["validation"]),
            test_group_ids=tuple(item["test"]),
            canonical_hash=str(item["hash"]),
        )
        for item in _decode(str(row["fold_definitions_payload"]))
    )
    return StrategyLabWalkForwardEvaluation(
        evaluation_id=str(row["evaluation_id"]),
        training_run_id=str(row["training_run_id"]),
        folds=folds,
        requested_fold_count=int(row["fold_count_requested"]),
        completed_fold_count=int(row["fold_count_completed"]),
        prediction_count=int(row["prediction_count"]),
        distinct_group_count=int(row["distinct_group_count"]),
        brier_score=_decimal(row["point_brier_score"]),
        log_loss=_decimal(row["point_log_loss"]),
        baseline_brier_score=_decimal(row["baseline_brier_score"]),
        baseline_log_loss=_decimal(row["baseline_log_loss"]),
        brier_difference=_decimal(row["brier_difference"]),
        log_loss_difference=_decimal(row["log_loss_difference"]),
        calibration_summary=_decode(str(row["calibration_summary"])),
        recent_fold_metrics=_decode(str(row["recent_fold_metrics"])),
        full_fold_metrics=_decode(str(row["full_fold_metrics"])),
        integrity_warnings=tuple(row["integrity_warning_codes"]),
        skipped_fold_reasons=tuple(row["skipped_fold_reasons"]),
        state=EvaluationState(str(row["state"])),
        canonical_input_hash=str(row["canonical_input_hash"]),
        canonical_result_hash=row["canonical_result_hash"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _candidate_values(item: StrategyLabModelCandidate) -> dict[str, object]:
    return {
        "candidate_id": item.candidate_id,
        "training_run_id": item.training_run_id,
        "evaluation_id": item.evaluation_id,
        "model_family": item.model_family.value,
        "model_specification_version": "V1",
        "feature_schema_hash": item.feature_schema_hash,
        "label_definition_hash": item.label_definition_hash,
        "dataset_hash": item.dataset_hash,
        "model_artifact_hash": item.model_artifact_hash,
        "state": item.state.value,
        "rejection_reasons": list(item.rejection_reasons),
        "eligible_at": item.eligible_at,
        "reviewed_at": item.reviewed_at,
        "canonical_candidate_hash": item.canonical_candidate_hash,
        "created_at": item.created_at,
    }


def _candidate(row: RowMapping) -> StrategyLabModelCandidate:
    return StrategyLabModelCandidate(
        candidate_id=str(row["candidate_id"]),
        training_run_id=str(row["training_run_id"]),
        evaluation_id=str(row["evaluation_id"]),
        model_family=ModelFamily(str(row["model_family"])),
        feature_schema_hash=str(row["feature_schema_hash"]),
        label_definition_hash=str(row["label_definition_hash"]),
        dataset_hash=str(row["dataset_hash"]),
        model_artifact_hash=str(row["model_artifact_hash"]),
        state=CandidateState(str(row["state"])),
        rejection_reasons=tuple(row["rejection_reasons"]),
        canonical_candidate_hash=str(row["canonical_candidate_hash"]),
        created_at=row["created_at"],
        eligible_at=row["eligible_at"],
        reviewed_at=row["reviewed_at"],
    )


def _decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


class PostgresStrategyLabShadowStore:
    """Durable, research-only SHADOW evidence store.

    It deliberately owns only the migration-0024 tables.  The store has no
    campaign or financial table import, so a model review cannot become a
    financial strategy through this persistence boundary.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save_operator_action(
        self, item: StrategyLabShadowOperatorAction
    ) -> StrategyLabShadowOperatorAction:
        with self._engine.begin() as connection:
            row = connection.execute(
                sa.select(strategy_lab_operator_action.c.canonical_input_hash).where(
                    strategy_lab_operator_action.c.action_id == item.action_id
                )
            ).scalar_one_or_none()
            if row is None:
                connection.execute(
                    strategy_lab_operator_action.insert().values(**_action_values(item))
                )
            elif str(row) != item.canonical_input_hash:
                raise StrategyLabError("SHADOW_ACTION_IDEMPOTENCY_CONFLICT")
        return item

    def review_by_idempotency(self, candidate_id: str, key: str) -> StrategyLabShadowReview | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_shadow_review).where(
                        strategy_lab_shadow_review.c.candidate_id == candidate_id,
                        strategy_lab_shadow_review.c.idempotency_key == key,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _shadow_review(row) if row is not None else None

    def save_review(self, item: StrategyLabShadowReview) -> StrategyLabShadowReview:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(strategy_lab_shadow_review.c.canonical_input_hash).where(
                    strategy_lab_shadow_review.c.review_id == item.review_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    strategy_lab_shadow_review.insert().values(**_review_values(item))
                )
            elif str(existing) != item.canonical_input_hash:
                raise StrategyLabError("SHADOW_REVIEW_IDEMPOTENCY_CONFLICT")
            else:
                connection.execute(
                    strategy_lab_shadow_review.update()
                    .where(strategy_lab_shadow_review.c.review_id == item.review_id)
                    .values(**_review_values(item))
                )
        return item

    def review(self, review_id: str) -> StrategyLabShadowReview | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_shadow_review).where(
                        strategy_lab_shadow_review.c.review_id == review_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _shadow_review(row) if row is not None else None

    def deployment(self, deployment_id: str) -> StrategyLabModelDeployment | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_model_deployment).where(
                        strategy_lab_model_deployment.c.deployment_id == deployment_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _deployment(row) if row is not None else None

    def deployment_for_candidate(self, candidate_id: str) -> StrategyLabModelDeployment | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_model_deployment)
                    .where(
                        strategy_lab_model_deployment.c.candidate_id == candidate_id,
                        strategy_lab_model_deployment.c.state != DeploymentState.REVOKED.value,
                    )
                    .order_by(strategy_lab_model_deployment.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _deployment(row) if row is not None else None

    def save_deployment(self, item: StrategyLabModelDeployment) -> StrategyLabModelDeployment:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(strategy_lab_model_deployment.c.canonical_hash).where(
                    strategy_lab_model_deployment.c.deployment_id == item.deployment_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    strategy_lab_model_deployment.insert().values(**_deployment_values(item))
                )
            elif str(existing) != item.canonical_hash:
                raise StrategyLabError("SHADOW_DEPLOYMENT_IDEMPOTENCY_CONFLICT")
            else:
                connection.execute(
                    strategy_lab_model_deployment.update()
                    .where(strategy_lab_model_deployment.c.deployment_id == item.deployment_id)
                    .values(**_deployment_values(item))
                )
        return item

    def batch_by_idempotency(
        self, deployment_id: str, key: str
    ) -> StrategyLabInferenceBatch | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_inference_batch).where(
                        strategy_lab_inference_batch.c.deployment_id == deployment_id,
                        strategy_lab_inference_batch.c.idempotency_key == key,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _batch(row) if row is not None else None

    def save_batch(self, item: StrategyLabInferenceBatch) -> StrategyLabInferenceBatch:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(strategy_lab_inference_batch.c.canonical_input_hash).where(
                    strategy_lab_inference_batch.c.batch_id == item.batch_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    strategy_lab_inference_batch.insert().values(**_batch_values(item))
                )
            elif str(existing) != item.canonical_input_hash:
                raise StrategyLabError("SHADOW_BATCH_IDEMPOTENCY_CONFLICT")
            else:
                connection.execute(
                    strategy_lab_inference_batch.update()
                    .where(strategy_lab_inference_batch.c.batch_id == item.batch_id)
                    .values(**_batch_values(item))
                )
        return item

    def save_prediction(self, item: StrategyLabForwardPrediction) -> StrategyLabForwardPrediction:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(strategy_lab_forward_prediction.c.canonical_prediction_hash).where(
                    strategy_lab_forward_prediction.c.prediction_id == item.prediction_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    strategy_lab_forward_prediction.insert().values(**_prediction_values(item))
                )
            elif str(existing) != item.canonical_prediction_hash:
                raise StrategyLabError("SHADOW_PREDICTION_IDEMPOTENCY_CONFLICT")
        return item

    def predictions(self, deployment_id: str) -> tuple[StrategyLabForwardPrediction, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(strategy_lab_forward_prediction)
                .where(strategy_lab_forward_prediction.c.deployment_id == deployment_id)
                .order_by(
                    strategy_lab_forward_prediction.c.feature_at,
                    strategy_lab_forward_prediction.c.prediction_id,
                )
            ).mappings()
            return tuple(_prediction(row) for row in rows)

    def update_prediction(self, item: StrategyLabForwardPrediction) -> StrategyLabForwardPrediction:
        with self._engine.begin() as connection:
            connection.execute(
                strategy_lab_forward_prediction.update()
                .where(strategy_lab_forward_prediction.c.prediction_id == item.prediction_id)
                .values(**_prediction_values(item))
            )
        return item

    def save_forward_evaluation(
        self, item: StrategyLabForwardEvaluation
    ) -> StrategyLabForwardEvaluation:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(strategy_lab_forward_evaluation.c.canonical_input_hash).where(
                    strategy_lab_forward_evaluation.c.evaluation_id == item.evaluation_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    strategy_lab_forward_evaluation.insert().values(
                        **_forward_evaluation_values(item)
                    )
                )
            elif str(existing) != item.canonical_input_hash:
                raise StrategyLabError("SHADOW_FORWARD_EVALUATION_IDEMPOTENCY_CONFLICT")
        return item

    def forward_evaluations(self, deployment_id: str) -> tuple[StrategyLabForwardEvaluation, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(strategy_lab_forward_evaluation)
                .join(
                    strategy_lab_forward_prediction,
                    strategy_lab_forward_prediction.c.prediction_id
                    == strategy_lab_forward_evaluation.c.prediction_id,
                )
                .where(strategy_lab_forward_prediction.c.deployment_id == deployment_id)
            ).mappings()
            return tuple(_forward_evaluation(row) for row in rows)

    def active_champion(
        self, vertical: str, research_family: str
    ) -> StrategyLabChampionAssignment | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_champion_assignment).where(
                        strategy_lab_champion_assignment.c.vertical == vertical,
                        strategy_lab_champion_assignment.c.research_family == research_family,
                        strategy_lab_champion_assignment.c.state
                        == ChampionAssignmentState.ACTIVE.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _champion(row) if row is not None else None

    def assign_champion(self, item: StrategyLabChampionAssignment) -> StrategyLabChampionAssignment:
        with self._engine.begin() as connection:
            connection.execute(
                sa.select(strategy_lab_champion_assignment.c.assignment_id)
                .where(
                    strategy_lab_champion_assignment.c.vertical == item.vertical,
                    strategy_lab_champion_assignment.c.research_family == item.research_family,
                    strategy_lab_champion_assignment.c.state
                    == ChampionAssignmentState.ACTIVE.value,
                )
                .with_for_update()
            )
            connection.execute(
                strategy_lab_champion_assignment.update()
                .where(
                    strategy_lab_champion_assignment.c.vertical == item.vertical,
                    strategy_lab_champion_assignment.c.research_family == item.research_family,
                    strategy_lab_champion_assignment.c.state
                    == ChampionAssignmentState.ACTIVE.value,
                )
                .values(
                    state=ChampionAssignmentState.SUPERSEDED.value, superseded_at=item.assigned_at
                )
            )
            existing = connection.execute(
                sa.select(strategy_lab_champion_assignment.c.canonical_input_hash).where(
                    strategy_lab_champion_assignment.c.assignment_id == item.assignment_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    strategy_lab_champion_assignment.insert().values(**_champion_values(item))
                )
            elif str(existing) != item.canonical_input_hash:
                raise StrategyLabError("CHAMPION_ASSIGNMENT_IDEMPOTENCY_CONFLICT")
        return item

    def revoke_champion(
        self, assignment_id: str, *, revoked_at: datetime
    ) -> StrategyLabChampionAssignment:
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_champion_assignment)
                    .where(strategy_lab_champion_assignment.c.assignment_id == assignment_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None or str(row["state"]) != ChampionAssignmentState.ACTIVE.value:
                raise StrategyLabError("CHAMPION_ASSIGNMENT_NOT_ACTIVE")
            connection.execute(
                strategy_lab_champion_assignment.update()
                .where(strategy_lab_champion_assignment.c.assignment_id == assignment_id)
                .values(state=ChampionAssignmentState.REVOKED.value, superseded_at=revoked_at)
            )
            item = _champion(row)
        return replace(
            item,
            state=ChampionAssignmentState.REVOKED,
            superseded_at=revoked_at,
        )

    def save_challenger_evaluation(
        self, item: StrategyLabChallengerEvaluation
    ) -> StrategyLabChallengerEvaluation:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(strategy_lab_challenger_evaluation.c.canonical_input_hash).where(
                    strategy_lab_challenger_evaluation.c.challenger_evaluation_id
                    == item.challenger_evaluation_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    strategy_lab_challenger_evaluation.insert().values(**_challenger_values(item))
                )
            elif str(existing) != item.canonical_input_hash:
                raise StrategyLabError("CHALLENGER_EVALUATION_IDEMPOTENCY_CONFLICT")
        return item

    def challenger_evaluation(self, deployment_id: str) -> StrategyLabChallengerEvaluation | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(strategy_lab_challenger_evaluation).where(
                        strategy_lab_challenger_evaluation.c.challenger_deployment_id
                        == deployment_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _challenger(row) if row is not None else None

    def reviews(self) -> tuple[StrategyLabShadowReview, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(strategy_lab_shadow_review).order_by(
                    strategy_lab_shadow_review.c.requested_at.desc()
                )
            ).mappings()
            return tuple(_shadow_review(row) for row in rows)

    def deployments(self) -> tuple[StrategyLabModelDeployment, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(strategy_lab_model_deployment).order_by(
                    strategy_lab_model_deployment.c.created_at.desc()
                )
            ).mappings()
            return tuple(_deployment(row) for row in rows)

    def champions(self) -> tuple[StrategyLabChampionAssignment, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(strategy_lab_champion_assignment).order_by(
                    strategy_lab_champion_assignment.c.assigned_at.desc()
                )
            ).mappings()
            return tuple(_champion(row) for row in rows)


def _action_values(item: StrategyLabShadowOperatorAction) -> dict[str, object]:
    return {
        "action_id": item.action_id,
        "action_type": item.action_type,
        "target_id": item.target_id,
        "idempotency_key": item.idempotency_key,
        "reason": item.reason,
        "note": item.note,
        "created_at": item.created_at,
        "canonical_input_hash": item.canonical_input_hash,
        "canonical_result_hash": item.canonical_result_hash,
    }


def _review_values(item: StrategyLabShadowReview) -> dict[str, object]:
    return {
        "review_id": item.review_id,
        "candidate_id": item.candidate_id,
        "requested_deployment_family": item.requested_deployment_family,
        "requested_vertical": item.requested_vertical,
        "state": item.state.value,
        "requested_at": item.requested_at,
        "started_at": item.started_at,
        "decided_at": item.decided_at,
        "expires_at": item.expires_at,
        "operator_reason": item.operator_reason,
        "reviewer_note": item.reviewer_note,
        "operator_action_id": item.operator_action_id,
        "idempotency_key": item.idempotency_key,
        "canonical_input_hash": item.canonical_input_hash,
        "canonical_result_hash": item.canonical_result_hash,
        "decision_reason_codes": list(item.reason_codes),
    }


def _shadow_review(row: RowMapping) -> StrategyLabShadowReview:
    return StrategyLabShadowReview(
        str(row["review_id"]),
        str(row["candidate_id"]),
        str(row["requested_deployment_family"]),
        str(row["requested_vertical"]),
        ShadowReviewState(str(row["state"])),
        row["requested_at"],
        row["started_at"],
        row["decided_at"],
        row["expires_at"],
        str(row["operator_reason"]),
        str(row["reviewer_note"]),
        str(row["operator_action_id"]) if row["operator_action_id"] else None,
        str(row["idempotency_key"]),
        str(row["canonical_input_hash"]),
        str(row["canonical_result_hash"]) if row["canonical_result_hash"] else None,
        tuple(row["decision_reason_codes"]),
    )


def _deployment_values(item: StrategyLabModelDeployment) -> dict[str, object]:
    return {
        "deployment_id": item.deployment_id,
        "review_id": item.review_id,
        "candidate_id": item.candidate_id,
        "deployment_role": item.role.value,
        "vertical": item.vertical,
        "research_family": item.research_family,
        "model_family": item.model_family.value,
        "feature_schema_id": item.feature_schema_id,
        "feature_schema_hash": item.feature_schema_hash,
        "label_definition_id": item.label_definition_id,
        "label_definition_hash": item.label_definition_hash,
        "dataset_snapshot_id": item.dataset_id,
        "dataset_hash": item.dataset_hash,
        "training_run_id": item.training_run_id,
        "training_hash": item.training_hash,
        "evaluation_id": item.evaluation_id,
        "evaluation_hash": item.evaluation_hash,
        "artifact_relative_location": item.artifact.relative_location,
        "artifact_sha256": item.artifact.sha256,
        "artifact_byte_size": item.artifact.byte_size,
        "calibration_identity": item.calibration_identity,
        "runtime_identity": item.runtime_identity,
        "state": item.state.value,
        "created_at": item.created_at,
        "activated_at": item.activated_at,
        "paused_at": item.paused_at,
        "retired_at": item.retired_at,
        "invalidated_at": item.invalidated_at,
        "operator_action_id": item.operator_action_id,
        "canonical_payload": _json(item.canonical_payload),
        "canonical_hash": item.canonical_hash,
        "last_integrity_check_at": item.last_integrity_check_at,
        "last_error_code": item.last_error_code,
    }


def _deployment(row: RowMapping) -> StrategyLabModelDeployment:
    return StrategyLabModelDeployment(
        str(row["deployment_id"]),
        str(row["review_id"]),
        str(row["candidate_id"]),
        DeploymentRole(str(row["deployment_role"])),
        str(row["vertical"]),
        str(row["research_family"]),
        ModelFamily(str(row["model_family"])),
        str(row["feature_schema_id"]),
        str(row["feature_schema_hash"]),
        str(row["label_definition_id"]),
        str(row["label_definition_hash"]),
        str(row["dataset_snapshot_id"]),
        str(row["dataset_hash"]),
        str(row["training_run_id"]),
        str(row["training_hash"]),
        str(row["evaluation_id"]),
        str(row["evaluation_hash"]),
        StrategyLabArtifact(
            str(row["artifact_relative_location"]),
            str(row["artifact_sha256"]),
            int(row["artifact_byte_size"]),
        ),
        str(row["calibration_identity"]),
        str(row["runtime_identity"]),
        DeploymentState(str(row["state"])),
        row["created_at"],
        row["activated_at"],
        row["paused_at"],
        row["retired_at"],
        row["invalidated_at"],
        str(row["operator_action_id"]),
        _decode(str(row["canonical_payload"])),
        str(row["canonical_hash"]),
        row["last_integrity_check_at"],
        row["last_error_code"],
    )


def _batch_values(item: StrategyLabInferenceBatch) -> dict[str, object]:
    return {
        "batch_id": item.batch_id,
        "deployment_id": item.deployment_id,
        "adapter_id": item.adapter_id,
        "adapter_version": item.adapter_version,
        "feature_schema_hash": item.feature_schema_hash,
        "source_cutoff": item.source_cutoff,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
        "state": item.state.value,
        "requested_row_count": item.requested_row_count,
        "accepted_row_count": item.accepted_row_count,
        "rejected_row_count": item.rejected_row_count,
        "prediction_count": item.prediction_count,
        "baseline_prediction_count": item.baseline_prediction_count,
        "champion_prediction_count": item.champion_prediction_count,
        "integrity_warnings": list(item.integrity_warnings),
        "source_batch_hash": item.source_batch_hash,
        "idempotency_key": item.idempotency_key,
        "canonical_input_hash": item.canonical_input_hash,
        "canonical_result_hash": item.canonical_result_hash,
        "last_error_code": item.last_error_code,
    }


def _batch(row: RowMapping) -> StrategyLabInferenceBatch:
    return StrategyLabInferenceBatch(
        str(row["batch_id"]),
        str(row["deployment_id"]),
        str(row["adapter_id"]),
        str(row["adapter_version"]),
        str(row["feature_schema_hash"]),
        row["source_cutoff"],
        row["started_at"],
        row["completed_at"],
        BatchState(str(row["state"])),
        int(row["requested_row_count"]),
        int(row["accepted_row_count"]),
        int(row["rejected_row_count"]),
        int(row["prediction_count"]),
        int(row["baseline_prediction_count"]),
        int(row["champion_prediction_count"]),
        tuple(row["integrity_warnings"]),
        str(row["source_batch_hash"]),
        str(row["idempotency_key"]),
        str(row["canonical_input_hash"]),
        str(row["canonical_result_hash"]) if row["canonical_result_hash"] else None,
        row["last_error_code"],
    )


def _prediction_values(item: StrategyLabForwardPrediction) -> dict[str, object]:
    return {
        "prediction_id": item.prediction_id,
        "inference_batch_id": item.batch_id,
        "deployment_id": item.deployment_id,
        "candidate_id": item.candidate_id,
        "entity_id": item.entity_id,
        "grouping_id": item.grouping_id,
        "feature_at": item.feature_at,
        "predicted_at": item.predicted_at,
        "model_probability": item.model_probability,
        "calibrated_probability": item.calibrated_probability,
        "baseline_probability": item.baseline_probability,
        "champion_probability": item.champion_probability,
        "feature_values_payload": _json(item.feature_values),
        "feature_row_hash": item.feature_row_hash,
        "feature_schema_hash": item.feature_schema_hash,
        "model_artifact_hash": item.model_artifact_hash,
        "source_lineage_hash": item.source_lineage_hash,
        "source_configuration_hash": item.source_configuration_hash,
        "positive_outcome_identity": item.positive_outcome_identity,
        "outcome_state": item.outcome_state.value,
        "canonical_prediction_hash": item.canonical_prediction_hash,
        "created_at": item.created_at,
    }


def _prediction(row: RowMapping) -> StrategyLabForwardPrediction:
    return StrategyLabForwardPrediction(
        str(row["prediction_id"]),
        str(row["inference_batch_id"]),
        str(row["deployment_id"]),
        str(row["candidate_id"]),
        str(row["entity_id"]),
        str(row["grouping_id"]),
        row["feature_at"],
        row["predicted_at"],
        _decimal(row["model_probability"]) or Decimal(),
        _decimal(row["calibrated_probability"]) or Decimal(),
        _decimal(row["baseline_probability"]) or Decimal(),
        _decimal(row["champion_probability"]),
        tuple(
            parse_decimal(value) if value is not None else None
            for value in _decode(str(row["feature_values_payload"]))
        ),
        str(row["feature_row_hash"]),
        str(row["feature_schema_hash"]),
        str(row["model_artifact_hash"]),
        str(row["source_lineage_hash"]),
        str(row["source_configuration_hash"]),
        str(row["positive_outcome_identity"]),
        ForwardOutcomeState(str(row["outcome_state"])),
        str(row["canonical_prediction_hash"]),
        row["created_at"],
    )


def _forward_evaluation_values(item: StrategyLabForwardEvaluation) -> dict[str, object]:
    return {
        "evaluation_id": item.evaluation_id,
        "prediction_id": item.prediction_id,
        "outcome_evidence_id": item.outcome_evidence_id,
        "resolved_at": item.resolved_at,
        "model_brier": item.model_brier,
        "calibrated_brier": item.calibrated_brier,
        "baseline_brier": item.baseline_brier,
        "champion_brier": item.champion_brier,
        "model_log_loss": item.model_log_loss,
        "calibrated_log_loss": item.calibrated_log_loss,
        "baseline_log_loss": item.baseline_log_loss,
        "champion_log_loss": item.champion_log_loss,
        "direction_correct": item.direction_correct,
        "canonical_input_hash": item.canonical_input_hash,
        "canonical_result_hash": item.canonical_result_hash,
    }


def _forward_evaluation(row: RowMapping) -> StrategyLabForwardEvaluation:
    return StrategyLabForwardEvaluation(
        str(row["evaluation_id"]),
        str(row["prediction_id"]),
        str(row["outcome_evidence_id"]),
        row["resolved_at"],
        _decimal(row["model_brier"]) or Decimal(),
        _decimal(row["calibrated_brier"]) or Decimal(),
        _decimal(row["baseline_brier"]) or Decimal(),
        _decimal(row["champion_brier"]),
        _decimal(row["model_log_loss"]) or Decimal(),
        _decimal(row["calibrated_log_loss"]) or Decimal(),
        _decimal(row["baseline_log_loss"]) or Decimal(),
        _decimal(row["champion_log_loss"]),
        bool(row["direction_correct"]),
        str(row["canonical_input_hash"]),
        str(row["canonical_result_hash"]),
    )


def _champion_values(item: StrategyLabChampionAssignment) -> dict[str, object]:
    return {
        "assignment_id": item.assignment_id,
        "vertical": item.vertical,
        "research_family": item.research_family,
        "deployment_id": item.deployment_id,
        "state": item.state.value,
        "assigned_at": item.assigned_at,
        "superseded_at": item.superseded_at,
        "operator_action_id": item.operator_action_id,
        "reason": item.reason,
        "canonical_input_hash": item.canonical_input_hash,
        "canonical_result_hash": item.canonical_result_hash,
    }


def _champion(row: RowMapping) -> StrategyLabChampionAssignment:
    return StrategyLabChampionAssignment(
        str(row["assignment_id"]),
        str(row["vertical"]),
        str(row["research_family"]),
        str(row["deployment_id"]),
        ChampionAssignmentState(str(row["state"])),
        row["assigned_at"],
        row["superseded_at"],
        str(row["operator_action_id"]),
        str(row["reason"]),
        str(row["canonical_input_hash"]),
        str(row["canonical_result_hash"]),
    )


def _challenger_values(item: StrategyLabChallengerEvaluation) -> dict[str, object]:
    return {
        "challenger_evaluation_id": item.challenger_evaluation_id,
        "challenger_deployment_id": item.challenger_deployment_id,
        "champion_deployment_id": item.champion_deployment_id,
        "cutoff_at": item.cutoff_at,
        "prediction_count": item.prediction_count,
        "resolved_prediction_count": item.resolved_prediction_count,
        "distinct_group_count": item.distinct_group_count,
        "calibrated_brier": item.calibrated_brier,
        "baseline_brier": item.baseline_brier,
        "champion_brier": item.champion_brier,
        "calibrated_log_loss": item.calibrated_log_loss,
        "baseline_log_loss": item.baseline_log_loss,
        "champion_log_loss": item.champion_log_loss,
        "calibration_coverage": item.calibration_coverage,
        "recent_metrics": _json(item.recent_metrics),
        "degradation_metrics": _json(item.degradation_metrics),
        "drift_metrics": _json(item.drift_metrics),
        "integrity_warnings": list(item.integrity_warnings),
        "state": item.state.value,
        "recommendation": item.recommendation.value,
        "reason_codes": list(item.reason_codes),
        "canonical_input_hash": item.canonical_input_hash,
        "canonical_result_hash": item.canonical_result_hash,
        "created_at": item.created_at,
    }


def _challenger(row: RowMapping) -> StrategyLabChallengerEvaluation:
    return StrategyLabChallengerEvaluation(
        str(row["challenger_evaluation_id"]),
        str(row["challenger_deployment_id"]),
        str(row["champion_deployment_id"]) if row["champion_deployment_id"] else None,
        row["cutoff_at"],
        int(row["prediction_count"]),
        int(row["resolved_prediction_count"]),
        int(row["distinct_group_count"]),
        _decimal(row["calibrated_brier"]),
        _decimal(row["baseline_brier"]),
        _decimal(row["champion_brier"]),
        _decimal(row["calibrated_log_loss"]),
        _decimal(row["baseline_log_loss"]),
        _decimal(row["champion_log_loss"]),
        int(row["calibration_coverage"]),
        _decode(str(row["recent_metrics"])),
        _decode(str(row["degradation_metrics"])),
        _decode(str(row["drift_metrics"])),
        tuple(row["integrity_warnings"]),
        ChallengerEvaluationState(str(row["state"])),
        ChallengerRecommendation(str(row["recommendation"])),
        tuple(row["reason_codes"]),
        str(row["canonical_input_hash"]),
        str(row["canonical_result_hash"]),
        row["created_at"],
    )
