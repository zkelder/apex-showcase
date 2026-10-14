"""Frozen genuine-evidence candidate execution without deployment authority."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from apex.application.candidate_generation import (
    AutomatedCandidateSearchPlan,
    TrialExecutionResult,
)
from apex.application.research_integrity import ResearchTrialAttempt
from apex.application.securities_strategy_lab_dataset import SecuritiesDailyDirectionDataset
from apex.application.strategy_lab import (
    CalibrationMethod,
    ModelFamily,
    StrategyLabArtifactStore,
    StrategyLabDatasetSnapshot,
    StrategyLabFold,
    StrategyLabService,
    load_dataset_artifact,
    out_of_fold_predictions,
)
from apex.domain.research_integrity import ReturnObservation
from apex.domain.values import parse_decimal


class GenuineCandidateEvaluationError(RuntimeError):
    """The candidate evaluation cannot prove its retained-evidence lineage."""


@dataclass(frozen=True, slots=True)
class FrozenGroupedWalkForwardPolicy:
    """Use exactly the dataset's predeclared non-holdout folds."""

    folds: tuple[StrategyLabFold, ...]
    policy_id: str = "SECURITIES_DAILY_DIRECTION_GROUPED_HOLDOUT_V1"

    @property
    def fold_count(self) -> int:
        return len(self.folds)

    def build_folds(self, rows: Sequence[object]) -> tuple[StrategyLabFold, ...]:
        groups = {getattr(row, "grouping_id", None) for row in rows}
        required = {
            group
            for fold in self.folds
            for group in (
                *fold.training_group_ids,
                *fold.validation_group_ids,
                *fold.test_group_ids,
            )
        }
        if len(self.folds) != 3 or None in groups or not required.issubset(groups):
            raise GenuineCandidateEvaluationError("FROZEN_WALK_FORWARD_LINEAGE_INVALID")
        return self.folds


class GenuineSecuritiesCandidateTrialExecutor:
    """Evaluate only the frozen folds and retain signal-conditioned outcome evidence.

    A probability at or above 0.50 represents an equal-weighted long-only
    research signal for that row; lower probabilities retain a zero return.
    This is a declared research metric construction, not a PAPER or LIVE order.
    """

    def __init__(
        self,
        *,
        strategy_service: StrategyLabService,
        artifacts: StrategyLabArtifactStore,
        dataset: SecuritiesDailyDirectionDataset,
        snapshot: StrategyLabDatasetSnapshot,
    ) -> None:
        self._service = strategy_service
        self._artifacts = artifacts
        self._dataset = dataset
        self._snapshot = snapshot
        if snapshot.dataset_id != dataset.strategy_lab_dataset_id or snapshot.artifact is None:
            raise GenuineCandidateEvaluationError("GENUINE_CANDIDATE_DATASET_UNAVAILABLE")
        self._policy = FrozenGroupedWalkForwardPolicy(dataset.folds)

    def execute(
        self,
        *,
        plan: AutomatedCandidateSearchPlan,
        proposal: dict[str, str],
        research_trial: ResearchTrialAttempt,
    ) -> TrialExecutionResult:
        family = ModelFamily(proposal["model_family"])
        calibration = CalibrationMethod[proposal["calibration"]]
        if family is ModelFamily.LOGISTIC_REGRESSION:
            parameters: dict[str, object] = {
                "C": proposal["regularization_c"],
                "penalty": proposal["penalty"],
                "class_weight": proposal["class_weight"],
                "max_iter": proposal["logistic_max_iter"],
            }
        else:
            parameters = {
                "learning_rate": proposal["learning_rate"],
                "max_iter": proposal["histogram_max_iter"],
                "max_leaf_nodes": proposal["max_leaf_nodes"],
                "max_bins": proposal["max_bins"],
                "min_samples_leaf": proposal["min_samples_leaf"],
                "l2_regularization": proposal["l2_regularization"],
            }
        dataset_id = plan.dataset_id
        seed = plan.random_seed + research_trial.ordinal
        training = self._service.train_model(
            dataset_id=dataset_id,
            family=family,
            hyperparameters=parameters,
            calibration=calibration,
            seed=seed,
            idempotency_key=f"candidate-search-training-{research_trial.trial_id}",
            policy=self._policy,
        )
        evaluation, candidate = self._service.evaluate_candidate(
            training_run_id=training.training_run_id,
            policy=self._policy,
        )
        if evaluation.brier_score is None or training.model_artifact is None:
            raise GenuineCandidateEvaluationError("GENUINE_CANDIDATE_BRIER_UNAVAILABLE")
        assert self._snapshot.artifact is not None
        rows = load_dataset_artifact(
            artifact=self._snapshot.artifact,
            store=self._artifacts,
            schema=self._snapshot.schema,
        )
        returns = self._returns_by_instrument_and_group()
        evidence = tuple(
            ReturnObservation(
                period_id=returns[(item.entity_id, item.grouping_id)][0],
                group_id=item.grouping_id,
                instrument_id=item.entity_id,
                value=(
                    returns[(item.entity_id, item.grouping_id)][1]
                    if item.probability >= Decimal("0.5")
                    else Decimal()
                ),
            )
            for item in out_of_fold_predictions(
                rows=rows,
                family=family,
                hyperparameters=training.hyperparameters,
                seed=seed,
                calibration=calibration,
                policy=self._policy,
            )
        )
        if len(evidence) != evaluation.prediction_count:
            raise GenuineCandidateEvaluationError("GENUINE_CANDIDATE_RETURN_ALIGNMENT_INVALID")
        return TrialExecutionResult(
            training_run=training,
            evaluation=evaluation,
            candidate=candidate,
            objective_value=evaluation.brier_score,
            artifact_identities=(training.model_artifact.relative_location,),
            artifact_hashes=(training.model_artifact.sha256,),
            artifact_bytes=training.model_artifact.byte_size,
            gross_returns=evidence,
            net_returns=evidence,
        )

    def _returns_by_instrument_and_group(self) -> dict[tuple[str, str], tuple[str, Decimal]]:
        payload = self._artifacts.read_bytes(self._dataset.returns_artifact)
        feature_payload = self._artifacts.read_bytes(self._dataset.feature_rows_artifact)
        try:
            values = tuple(json.loads(line) for line in payload.splitlines())
            decision_dates = {
                (str(item["instrument_id"]), str(item["decision_session_id"])): str(
                    item["decision_session_date"]
                )
                for line in feature_payload.splitlines()
                for item in (json.loads(line),)
            }
            result = {
                (str(item["instrument_id"]), str(item["decision_session_id"])): (
                    decision_dates[(str(item["instrument_id"]), str(item["decision_session_id"]))],
                    parse_decimal(item["realized_total_return"], allow_negative=True),
                )
                for item in values
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GenuineCandidateEvaluationError(
                "GENUINE_CANDIDATE_RETURN_ARTIFACT_INVALID"
            ) from error
        if len(result) != self._dataset.row_count:
            raise GenuineCandidateEvaluationError("GENUINE_CANDIDATE_RETURN_ARTIFACT_INCOMPLETE")
        return result
