"""PostgreSQL adapter for research-only experiment and overfitting evidence."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.postgres import (
    research_cross_validation_partition,
    research_experiment_family,
    research_metric_snapshot,
    research_multiple_testing_evaluation,
    research_return_series_artifact,
    research_selection_event,
    research_trial_attempt,
)
from apex.application.research_integrity import (
    MultipleTestingState,
    ResearchCrossValidationPartition,
    ResearchExperimentFamily,
    ResearchFamilyState,
    ResearchIdempotencyConflict,
    ResearchIntegrityApplicationError,
    ResearchIntegrityRepository,
    ResearchMetricSnapshot,
    ResearchMultipleTestingEvaluation,
    ResearchRecommendation,
    ResearchReturnSeriesArtifact,
    ResearchSelectionEvent,
    ResearchSeriesType,
    ResearchTrialAttempt,
    ResearchTrialSourceType,
    ResearchTrialState,
)
from apex.domain.research_integrity import ResearchFrequency
from apex.domain.values import canonical_json_bytes


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _restore_canonical_value(value: object) -> object:
    if isinstance(value, list):
        return tuple(_restore_canonical_value(item) for item in value)
    if isinstance(value, dict):
        return {str(key): _restore_canonical_value(item) for key, item in value.items()}
    return value


def _restore_canonical_mapping(payload: str) -> dict[str, object]:
    restored = _restore_canonical_value(json.loads(payload))
    if not isinstance(restored, dict):
        raise ResearchIntegrityApplicationError("INVALID_RESEARCH_JSON_OBJECT")
    return restored


class PostgresResearchIntegrityRepository(ResearchIntegrityRepository):
    """No campaign, order, position, settlement, or ledger access is exposed."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def family_by_idempotency(self, key: str) -> ResearchExperimentFamily | None:
        return self._family_where(research_experiment_family.c.idempotency_key == key)

    def family(self, experiment_id: str) -> ResearchExperimentFamily | None:
        return self._family_where(research_experiment_family.c.experiment_id == experiment_id)

    def _family_where(self, clause: Any) -> ResearchExperimentFamily | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(sa.select(research_experiment_family).where(clause))
                .mappings()
                .one_or_none()
            )
        return _family(row) if row is not None else None

    def families(self) -> tuple[ResearchExperimentFamily, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(research_experiment_family).order_by(
                        research_experiment_family.c.created_at,
                        research_experiment_family.c.experiment_id,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_family(row) for row in rows)

    def save_family(self, family: ResearchExperimentFamily) -> ResearchExperimentFamily:
        replay = self.family_by_idempotency(family.idempotency_key)
        if replay is not None:
            if replay.canonical_input_hash != family.canonical_input_hash:
                raise ResearchIdempotencyConflict("RESEARCH_FAMILY_IDEMPOTENCY_CONFLICT")
            return replay
        values: dict[str, object] = {
            "experiment_id": family.experiment_id,
            "name": family.name,
            "vertical": family.vertical,
            "research_family": family.research_family,
            "research_question": family.research_question,
            "target_label_identity": family.target_label_identity,
            "benchmark_identities": list(family.benchmark_identities),
            "data_scope_identities": list(family.data_scope_identities),
            "data_scope_hashes": list(family.data_scope_hashes),
            "universe_hash": family.universe_hash,
            "period_start": family.period_start,
            "period_end": family.period_end,
            "cutoff_at": family.cutoff_at,
            "feature_schema_hashes": list(family.feature_schema_hashes),
            "permitted_families": list(family.permitted_families),
            "parameter_space_payload": _json(family.parameter_space),
            "parameter_space_hash": family.parameter_space_hash,
            "maximum_trials": family.maximum_trials,
            "maximum_feature_variants": family.maximum_feature_variants,
            "maximum_model_variants": family.maximum_model_variants,
            "maximum_simulator_variants": family.maximum_simulator_variants,
            "evaluation_policy_id": family.evaluation_policy_id,
            "evaluation_policy_hash": family.evaluation_policy_hash,
            "cost_execution_assumptions": _json(family.cost_execution_assumptions),
            "cost_execution_hash": family.cost_execution_hash,
            "frequency": family.frequency.value,
            "state": family.state.value,
            "created_at": family.created_at,
            "opened_at": family.opened_at,
            "closed_at": family.closed_at,
            "operator_action_id": family.operator_action_id,
            "operator_reason": family.operator_reason,
            "idempotency_key": family.idempotency_key,
            "canonical_input_hash": family.canonical_input_hash,
            "canonical_result_hash": family.canonical_result_hash,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(research_experiment_family.insert().values(**values))
        except sa.exc.IntegrityError as error:
            raise ResearchIdempotencyConflict("RESEARCH_FAMILY_IDEMPOTENCY_CONFLICT") from error
        return family

    def update_family_state(
        self,
        experiment_id: str,
        *,
        expected: ResearchFamilyState,
        replacement: ResearchExperimentFamily,
    ) -> ResearchExperimentFamily:
        if replacement.experiment_id != experiment_id:
            raise ResearchIntegrityApplicationError("RESEARCH_FAMILY_SCOPE_IMMUTABLE")
        with self._engine.begin() as connection:
            result = connection.execute(
                research_experiment_family.update()
                .where(
                    research_experiment_family.c.experiment_id == experiment_id,
                    research_experiment_family.c.state == expected.value,
                    research_experiment_family.c.canonical_input_hash
                    == replacement.canonical_input_hash,
                )
                .values(
                    state=replacement.state.value,
                    closed_at=replacement.closed_at,
                    canonical_result_hash=replacement.canonical_result_hash,
                )
            )
            if result.rowcount != 1:
                raise ResearchIntegrityApplicationError("RESEARCH_FAMILY_STATE_CONFLICT")
        return replacement

    def trial_by_idempotency(self, experiment_id: str, key: str) -> ResearchTrialAttempt | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_trial_attempt).where(
                        research_trial_attempt.c.experiment_id == experiment_id,
                        research_trial_attempt.c.idempotency_key == key,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _trial(row) if row is not None else None

    def reserve_trial(self, trial: ResearchTrialAttempt) -> ResearchTrialAttempt:
        try:
            with self._engine.begin() as connection:
                family_row = (
                    connection.execute(
                        sa.select(
                            research_experiment_family.c.state,
                            research_experiment_family.c.maximum_trials,
                        )
                        .where(research_experiment_family.c.experiment_id == trial.experiment_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if family_row is None or family_row["state"] != ResearchFamilyState.OPEN.value:
                    raise ResearchIntegrityApplicationError("RESEARCH_FAMILY_NOT_OPEN")
                count = cast(
                    int,
                    connection.scalar(
                        sa.select(sa.func.count())
                        .select_from(research_trial_attempt)
                        .where(research_trial_attempt.c.experiment_id == trial.experiment_id)
                    ),
                )
                if count >= cast(int, family_row["maximum_trials"]):
                    raise ResearchIntegrityApplicationError("TRIAL_BUDGET_EXHAUSTED")
                if trial.ordinal != count + 1:
                    raise ResearchIntegrityApplicationError("TRIAL_ORDINAL_CONFLICT")
                connection.execute(research_trial_attempt.insert().values(**_trial_values(trial)))
        except sa.exc.IntegrityError as error:
            raise ResearchIdempotencyConflict("TRIAL_IDEMPOTENCY_CONFLICT") from error
        return trial

    def finish_trial(self, trial: ResearchTrialAttempt) -> ResearchTrialAttempt:
        with self._engine.begin() as connection:
            result = connection.execute(
                research_trial_attempt.update()
                .where(
                    research_trial_attempt.c.trial_id == trial.trial_id,
                    research_trial_attempt.c.state == ResearchTrialState.REQUESTED.value,
                    research_trial_attempt.c.canonical_input_hash == trial.canonical_input_hash,
                )
                .values(
                    state=trial.state.value,
                    completed_at=trial.completed_at,
                    result_identity=trial.result_identity,
                    result_hash=trial.result_hash,
                    failure_reason=trial.failure_reason,
                    integrity_blocked=trial.integrity_blocked,
                    canonical_result_hash=trial.canonical_result_hash,
                )
            )
            if result.rowcount != 1:
                retained = (
                    connection.execute(
                        sa.select(research_trial_attempt).where(
                            research_trial_attempt.c.trial_id == trial.trial_id
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if retained is not None:
                    restored = _trial(retained)
                    if restored.canonical_result_hash == trial.canonical_result_hash:
                        return restored
                raise ResearchIdempotencyConflict("TRIAL_RESULT_CONFLICT")
        return trial

    def trial(self, trial_id: str) -> ResearchTrialAttempt | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_trial_attempt).where(
                        research_trial_attempt.c.trial_id == trial_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _trial(row) if row is not None else None

    def trials(self, experiment_id: str) -> tuple[ResearchTrialAttempt, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(research_trial_attempt)
                    .where(research_trial_attempt.c.experiment_id == experiment_id)
                    .order_by(research_trial_attempt.c.trial_ordinal)
                )
                .mappings()
                .all()
            )
        return tuple(_trial(row) for row in rows)

    def save_artifact(self, artifact: ResearchReturnSeriesArtifact) -> ResearchReturnSeriesArtifact:
        values: dict[str, object] = {
            "artifact_id": artifact.artifact_id,
            "trial_id": artifact.trial_id,
            "series_type": artifact.series_type.value,
            "frequency": artifact.frequency.value,
            "start_period": artifact.start_period,
            "end_period": artifact.end_period,
            "observation_count": artifact.observation_count,
            "managed_relative_path": artifact.relative_path,
            "sha256": artifact.sha256,
            "byte_size": artifact.byte_size,
            "return_policy_hash": artifact.return_policy_hash,
            "missing_period_policy": artifact.missing_period_policy,
            "canonical_result_hash": artifact.canonical_result_hash,
        }
        self._save_immutable(
            research_return_series_artifact,
            research_return_series_artifact.c.artifact_id,
            artifact.artifact_id,
            artifact.canonical_result_hash,
            values,
        )
        return artifact

    def artifacts(self, trial_id: str) -> tuple[ResearchReturnSeriesArtifact, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(research_return_series_artifact)
                    .where(research_return_series_artifact.c.trial_id == trial_id)
                    .order_by(research_return_series_artifact.c.series_type)
                )
                .mappings()
                .all()
            )
        return tuple(_artifact(row) for row in rows)

    def save_metric(self, metric: ResearchMetricSnapshot) -> ResearchMetricSnapshot:
        values = {
            "metric_id": metric.metric_id,
            "trial_id": metric.trial_id,
            "observation_count": metric.observation_count,
            "independent_group_count": metric.independent_group_count,
            "gross_return": metric.gross_return,
            "net_return": metric.net_return,
            "mean_periodic_return": metric.mean_periodic_return,
            "periodic_volatility": metric.periodic_volatility,
            "sharpe_ratio": metric.sharpe_ratio,
            "sortino_ratio": metric.sortino_ratio,
            "maximum_drawdown": metric.maximum_drawdown,
            "turnover": metric.turnover,
            "hit_rate": metric.hit_rate,
            "brier_score": metric.brier_score,
            "log_loss": metric.log_loss,
            "benchmark_differences": _json(metric.benchmark_differences),
            "skewness": metric.skewness,
            "excess_kurtosis": metric.excess_kurtosis,
            "autocorrelations": list(metric.autocorrelations),
            "effective_sample_size": metric.effective_sample_size,
            "concentration": _json(metric.concentration),
            "integrity_warnings": list(metric.integrity_warnings),
            "calculation_policy_id": metric.calculation_policy_id,
            "calculation_policy_version": metric.calculation_policy_version,
            "canonical_input_hash": metric.canonical_input_hash,
            "canonical_result_hash": metric.canonical_result_hash,
        }
        self._save_immutable(
            research_metric_snapshot,
            research_metric_snapshot.c.metric_id,
            metric.metric_id,
            metric.canonical_result_hash,
            values,
        )
        return metric

    def metric(self, trial_id: str) -> ResearchMetricSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_metric_snapshot).where(
                        research_metric_snapshot.c.trial_id == trial_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _metric(row) if row is not None else None

    def save_selection(self, event: ResearchSelectionEvent) -> ResearchSelectionEvent:
        values = {
            "selection_id": event.selection_id,
            "experiment_id": event.experiment_id,
            "selected_trial_id": event.selected_trial_id,
            "candidate_trial_ids": list(event.candidate_trial_ids),
            "candidate_set_hash": event.candidate_set_hash,
            "selection_criterion": event.criterion,
            "criterion_direction": event.direction,
            "selected_at": event.selected_at,
            "action_identity": event.action_identity,
            "reason": event.reason,
            "canonical_input_hash": event.canonical_input_hash,
            "canonical_result_hash": event.canonical_result_hash,
        }
        self._save_immutable(
            research_selection_event,
            research_selection_event.c.selection_id,
            event.selection_id,
            event.canonical_result_hash,
            values,
        )
        return event

    def selections(self, experiment_id: str) -> tuple[ResearchSelectionEvent, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(research_selection_event)
                    .where(research_selection_event.c.experiment_id == experiment_id)
                    .order_by(research_selection_event.c.selected_at)
                )
                .mappings()
                .all()
            )
        return tuple(_selection(row) for row in rows)

    def save_partitions(
        self, values: Sequence[ResearchCrossValidationPartition]
    ) -> tuple[ResearchCrossValidationPartition, ...]:
        for value in values:
            row = {
                "partition_id": value.partition_id,
                "experiment_id": value.experiment_id,
                "partition_policy_id": value.policy_id,
                "partition_policy_version": value.policy_version,
                "partition_ordinal": value.ordinal,
                "training_group_ids": list(value.training_group_ids),
                "training_group_hash": value.training_group_hash,
                "validation_group_ids": list(value.validation_group_ids),
                "validation_group_hash": value.validation_group_hash,
                "test_group_ids": list(value.test_group_ids),
                "test_group_hash": value.test_group_hash,
                "selected_trial_id": value.selected_trial_id,
                "validation_result_id": value.validation_result_id,
                "test_result_id": value.test_result_id,
                "selected_test_rank_percentile": value.selected_test_rank_percentile,
                "below_median": value.below_median,
                "canonical_hash": value.canonical_hash,
            }
            self._save_immutable(
                research_cross_validation_partition,
                research_cross_validation_partition.c.partition_id,
                value.partition_id,
                value.canonical_hash,
                row,
                hash_column=research_cross_validation_partition.c.canonical_hash,
            )
        return tuple(values)

    def save_evaluation(
        self, evaluation: ResearchMultipleTestingEvaluation
    ) -> ResearchMultipleTestingEvaluation:
        values = {
            "evaluation_id": evaluation.evaluation_id,
            "experiment_id": evaluation.experiment_id,
            "cutoff_at": evaluation.cutoff_at,
            "included_trial_ids": list(evaluation.included_trial_ids),
            "included_trial_set_hash": evaluation.included_trial_set_hash,
            "completed_trial_count": evaluation.completed_trial_count,
            "failed_rejected_trial_count": evaluation.failed_rejected_trial_count,
            "unique_configuration_count": evaluation.unique_configuration_count,
            "feature_variant_count": evaluation.feature_variant_count,
            "model_variant_count": evaluation.model_variant_count,
            "simulator_variant_count": evaluation.simulator_variant_count,
            "raw_comparable_trial_count": evaluation.raw_comparable_trial_count,
            "effective_independent_trials": evaluation.effective_independent_trials,
            "overlap_coverage": evaluation.overlap_coverage,
            "best_observed_trial_id": evaluation.best_observed_trial_id,
            "best_observed_metric": evaluation.best_observed_metric,
            "benchmark_metric": evaluation.benchmark_metric,
            "probabilistic_sharpe_ratio": evaluation.probabilistic_sharpe_ratio,
            "deflated_sharpe_ratio": evaluation.deflated_sharpe_ratio,
            "expected_maximum_sharpe": evaluation.expected_maximum_sharpe,
            "minimum_track_record_length": evaluation.minimum_track_record_length,
            "probability_backtest_overfitting": evaluation.probability_backtest_overfitting,
            "rank_stability": _json(evaluation.rank_stability),
            "best_fold_excluded": _json(evaluation.best_fold_excluded),
            "best_period_excluded": _json(evaluation.best_period_excluded),
            "concentration": _json(evaluation.concentration),
            "state": evaluation.state.value,
            "recommendation": evaluation.recommendation.value,
            "reason_codes": list(evaluation.reason_codes),
            "calculation_policy_id": evaluation.calculation_policy_id,
            "calculation_policy_version": evaluation.calculation_policy_version,
            "canonical_input_hash": evaluation.canonical_input_hash,
            "canonical_result_hash": evaluation.canonical_result_hash,
            "created_at": evaluation.created_at,
        }
        self._save_immutable(
            research_multiple_testing_evaluation,
            research_multiple_testing_evaluation.c.evaluation_id,
            evaluation.evaluation_id,
            evaluation.canonical_result_hash,
            values,
        )
        return evaluation

    def evaluations(self, experiment_id: str) -> tuple[ResearchMultipleTestingEvaluation, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(research_multiple_testing_evaluation)
                    .where(research_multiple_testing_evaluation.c.experiment_id == experiment_id)
                    .order_by(research_multiple_testing_evaluation.c.created_at)
                )
                .mappings()
                .all()
            )
        return tuple(_evaluation(row) for row in rows)

    def _save_immutable(
        self,
        table: sa.Table,
        identity_column: Any,
        identity: str,
        expected_hash: str,
        values: dict[str, object],
        *,
        hash_column: Any | None = None,
    ) -> None:
        column = table.c.canonical_result_hash if hash_column is None else hash_column
        with self._engine.begin() as connection:
            retained = connection.scalar(sa.select(column).where(identity_column == identity))
            if retained is not None:
                if retained != expected_hash:
                    raise ResearchIdempotencyConflict(f"{table.name.upper()}_IMMUTABILITY_CONFLICT")
                return
            connection.execute(table.insert().values(**values))


def _trial_values(value: ResearchTrialAttempt) -> dict[str, object]:
    return {
        "trial_id": value.trial_id,
        "experiment_id": value.experiment_id,
        "parent_trial_id": value.parent_trial_id,
        "trial_ordinal": value.ordinal,
        "source_type": value.source_type.value,
        "source_identity": value.source_identity,
        "source_hash": value.source_hash,
        "strategy_model_family": value.strategy_model_family,
        "configuration_payload": _json(value.configuration),
        "configuration_hash": value.configuration_hash,
        "feature_set_hash": value.feature_set_hash,
        "dataset_hash": value.dataset_hash,
        "simulator_identity": value.simulator_identity,
        "simulator_version": value.simulator_version,
        "execution_cost_policy_hash": value.execution_cost_policy_hash,
        "random_seed": value.random_seed,
        "state": value.state.value,
        "started_at": value.started_at,
        "completed_at": value.completed_at,
        "result_identity": value.result_identity,
        "result_hash": value.result_hash,
        "failure_reason": value.failure_reason,
        "selected_for_comparison": value.selected_for_comparison,
        "integrity_blocked": value.integrity_blocked,
        "idempotency_key": value.idempotency_key,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _family(row: RowMapping) -> ResearchExperimentFamily:
    return ResearchExperimentFamily(
        experiment_id=cast(str, row["experiment_id"]),
        name=cast(str, row["name"]),
        vertical=cast(str, row["vertical"]),
        research_family=cast(str, row["research_family"]),
        research_question=cast(str, row["research_question"]),
        target_label_identity=cast(str, row["target_label_identity"]),
        benchmark_identities=tuple(cast(Sequence[str], row["benchmark_identities"])),
        data_scope_identities=tuple(cast(Sequence[str], row["data_scope_identities"])),
        data_scope_hashes=tuple(cast(Sequence[str], row["data_scope_hashes"])),
        universe_hash=cast(str, row["universe_hash"]),
        period_start=cast(str, row["period_start"]),
        period_end=cast(str, row["period_end"]),
        cutoff_at=cast(datetime, row["cutoff_at"]),
        feature_schema_hashes=tuple(cast(Sequence[str], row["feature_schema_hashes"])),
        permitted_families=tuple(cast(Sequence[str], row["permitted_families"])),
        parameter_space=_restore_canonical_mapping(cast(str, row["parameter_space_payload"])),
        parameter_space_hash=cast(str, row["parameter_space_hash"]),
        maximum_trials=cast(int, row["maximum_trials"]),
        maximum_feature_variants=cast(int, row["maximum_feature_variants"]),
        maximum_model_variants=cast(int, row["maximum_model_variants"]),
        maximum_simulator_variants=cast(int, row["maximum_simulator_variants"]),
        evaluation_policy_id=cast(str, row["evaluation_policy_id"]),
        evaluation_policy_hash=cast(str, row["evaluation_policy_hash"]),
        cost_execution_assumptions=_restore_canonical_mapping(
            cast(str, row["cost_execution_assumptions"])
        ),
        cost_execution_hash=cast(str, row["cost_execution_hash"]),
        frequency=ResearchFrequency(cast(str, row["frequency"])),
        state=ResearchFamilyState(cast(str, row["state"])),
        created_at=cast(datetime, row["created_at"]),
        opened_at=cast(datetime | None, row["opened_at"]),
        closed_at=cast(datetime | None, row["closed_at"]),
        operator_action_id=cast(str, row["operator_action_id"]),
        operator_reason=cast(str, row["operator_reason"]),
        idempotency_key=cast(str, row["idempotency_key"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
    )


def _trial(row: RowMapping) -> ResearchTrialAttempt:
    return ResearchTrialAttempt(
        trial_id=cast(str, row["trial_id"]),
        experiment_id=cast(str, row["experiment_id"]),
        parent_trial_id=cast(str | None, row["parent_trial_id"]),
        ordinal=cast(int, row["trial_ordinal"]),
        source_type=ResearchTrialSourceType(cast(str, row["source_type"])),
        source_identity=cast(str, row["source_identity"]),
        source_hash=cast(str, row["source_hash"]),
        strategy_model_family=cast(str, row["strategy_model_family"]),
        configuration=_restore_canonical_mapping(cast(str, row["configuration_payload"])),
        configuration_hash=cast(str, row["configuration_hash"]),
        feature_set_hash=cast(str, row["feature_set_hash"]),
        dataset_hash=cast(str, row["dataset_hash"]),
        simulator_identity=cast(str, row["simulator_identity"]),
        simulator_version=cast(str, row["simulator_version"]),
        execution_cost_policy_hash=cast(str, row["execution_cost_policy_hash"]),
        random_seed=cast(int | None, row["random_seed"]),
        state=ResearchTrialState(cast(str, row["state"])),
        started_at=cast(datetime, row["started_at"]),
        completed_at=cast(datetime | None, row["completed_at"]),
        result_identity=cast(str | None, row["result_identity"]),
        result_hash=cast(str | None, row["result_hash"]),
        failure_reason=cast(str | None, row["failure_reason"]),
        selected_for_comparison=cast(bool, row["selected_for_comparison"]),
        integrity_blocked=cast(bool, row["integrity_blocked"]),
        idempotency_key=cast(str, row["idempotency_key"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str | None, row["canonical_result_hash"]),
    )


def _artifact(row: RowMapping) -> ResearchReturnSeriesArtifact:
    return ResearchReturnSeriesArtifact(
        artifact_id=cast(str, row["artifact_id"]),
        trial_id=cast(str, row["trial_id"]),
        series_type=ResearchSeriesType(cast(str, row["series_type"])),
        frequency=ResearchFrequency(cast(str, row["frequency"])),
        start_period=cast(str, row["start_period"]),
        end_period=cast(str, row["end_period"]),
        observation_count=cast(int, row["observation_count"]),
        relative_path=cast(str, row["managed_relative_path"]),
        sha256=cast(str, row["sha256"]),
        byte_size=cast(int, row["byte_size"]),
        return_policy_hash=cast(str, row["return_policy_hash"]),
        missing_period_policy=cast(str, row["missing_period_policy"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
    )


def _metric(row: RowMapping) -> ResearchMetricSnapshot:
    return ResearchMetricSnapshot(
        metric_id=cast(str, row["metric_id"]),
        trial_id=cast(str, row["trial_id"]),
        observation_count=cast(int, row["observation_count"]),
        independent_group_count=cast(int, row["independent_group_count"]),
        gross_return=cast(Decimal, row["gross_return"]),
        net_return=cast(Decimal, row["net_return"]),
        mean_periodic_return=cast(Decimal, row["mean_periodic_return"]),
        periodic_volatility=cast(Decimal, row["periodic_volatility"]),
        sharpe_ratio=cast(Decimal | None, row["sharpe_ratio"]),
        sortino_ratio=cast(Decimal | None, row["sortino_ratio"]),
        maximum_drawdown=cast(Decimal, row["maximum_drawdown"]),
        turnover=cast(Decimal, row["turnover"]),
        hit_rate=cast(Decimal, row["hit_rate"]),
        brier_score=cast(Decimal | None, row["brier_score"]),
        log_loss=cast(Decimal | None, row["log_loss"]),
        benchmark_differences=cast(
            dict[str, object], json.loads(cast(str, row["benchmark_differences"]))
        ),
        skewness=cast(Decimal | None, row["skewness"]),
        excess_kurtosis=cast(Decimal | None, row["excess_kurtosis"]),
        autocorrelations=tuple(cast(Sequence[Decimal], row["autocorrelations"])),
        effective_sample_size=cast(Decimal, row["effective_sample_size"]),
        concentration=cast(dict[str, object], json.loads(cast(str, row["concentration"]))),
        integrity_warnings=tuple(cast(Sequence[str], row["integrity_warnings"])),
        calculation_policy_id=cast(str, row["calculation_policy_id"]),
        calculation_policy_version=cast(str, row["calculation_policy_version"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
    )


def _selection(row: RowMapping) -> ResearchSelectionEvent:
    return ResearchSelectionEvent(
        selection_id=cast(str, row["selection_id"]),
        experiment_id=cast(str, row["experiment_id"]),
        selected_trial_id=cast(str, row["selected_trial_id"]),
        candidate_trial_ids=tuple(cast(Sequence[str], row["candidate_trial_ids"])),
        candidate_set_hash=cast(str, row["candidate_set_hash"]),
        criterion=cast(str, row["selection_criterion"]),
        direction=cast(str, row["criterion_direction"]),
        selected_at=cast(datetime, row["selected_at"]),
        action_identity=cast(str, row["action_identity"]),
        reason=cast(str, row["reason"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
    )


def _evaluation(row: RowMapping) -> ResearchMultipleTestingEvaluation:
    return ResearchMultipleTestingEvaluation(
        evaluation_id=cast(str, row["evaluation_id"]),
        experiment_id=cast(str, row["experiment_id"]),
        cutoff_at=cast(datetime, row["cutoff_at"]),
        included_trial_ids=tuple(cast(Sequence[str], row["included_trial_ids"])),
        included_trial_set_hash=cast(str, row["included_trial_set_hash"]),
        completed_trial_count=cast(int, row["completed_trial_count"]),
        failed_rejected_trial_count=cast(int, row["failed_rejected_trial_count"]),
        unique_configuration_count=cast(int, row["unique_configuration_count"]),
        feature_variant_count=cast(int, row["feature_variant_count"]),
        model_variant_count=cast(int, row["model_variant_count"]),
        simulator_variant_count=cast(int, row["simulator_variant_count"]),
        raw_comparable_trial_count=cast(int, row["raw_comparable_trial_count"]),
        effective_independent_trials=cast(Decimal, row["effective_independent_trials"]),
        overlap_coverage=cast(Decimal, row["overlap_coverage"]),
        best_observed_trial_id=cast(str | None, row["best_observed_trial_id"]),
        best_observed_metric=cast(Decimal | None, row["best_observed_metric"]),
        benchmark_metric=cast(Decimal, row["benchmark_metric"]),
        probabilistic_sharpe_ratio=cast(Decimal | None, row["probabilistic_sharpe_ratio"]),
        deflated_sharpe_ratio=cast(Decimal | None, row["deflated_sharpe_ratio"]),
        expected_maximum_sharpe=cast(Decimal | None, row["expected_maximum_sharpe"]),
        minimum_track_record_length=cast(Decimal | None, row["minimum_track_record_length"]),
        probability_backtest_overfitting=cast(
            Decimal | None, row["probability_backtest_overfitting"]
        ),
        rank_stability=cast(dict[str, object], json.loads(cast(str, row["rank_stability"]))),
        best_fold_excluded=cast(
            dict[str, object], json.loads(cast(str, row["best_fold_excluded"]))
        ),
        best_period_excluded=cast(
            dict[str, object], json.loads(cast(str, row["best_period_excluded"]))
        ),
        concentration=cast(dict[str, object], json.loads(cast(str, row["concentration"]))),
        state=MultipleTestingState(cast(str, row["state"])),
        recommendation=ResearchRecommendation(cast(str, row["recommendation"])),
        reason_codes=tuple(cast(Sequence[str], row["reason_codes"])),
        calculation_policy_id=cast(str, row["calculation_policy_id"]),
        calculation_policy_version=cast(str, row["calculation_policy_version"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
        created_at=cast(datetime, row["created_at"]),
    )
