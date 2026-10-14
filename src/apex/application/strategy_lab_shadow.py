"""Research-only Strategy Lab SHADOW deployment and challenger evaluation.

This runtime is deliberately separate from normal strategy governance and every
financial service.  A deployment can emit retained probabilities only; it has
no route to proposals, allocation, orders, positions, settlement, or a ledger.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Protocol, cast

from apex.application.strategy_lab import (
    CandidateState,
    DatasetState,
    ModelFamily,
    StrategyLabArtifact,
    StrategyLabArtifactStore,
    StrategyLabDatasetSnapshot,
    StrategyLabError,
    StrategyLabModelCandidate,
    StrategyLabRepository,
    StrategyLabTrainingRun,
    TrainingRunState,
    load_dataset_artifact,
)
from apex.domain.values import content_digest, deterministic_id, parse_decimal

WARNING = "SHADOW MODEL — NO CAPITAL AUTHORITY"


class ShadowReviewState(StrEnum):
    REQUESTED = "REQUESTED"
    IN_REVIEW = "IN_REVIEW"
    BLOCKED = "BLOCKED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"


class DeploymentRole(StrEnum):
    CHALLENGER = "CHALLENGER"
    CHAMPION = "CHAMPION"


class DeploymentState(StrEnum):
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    RETIRED = "RETIRED"
    INVALIDATED = "INVALIDATED"
    REVOKED = "REVOKED"


class BatchState(StrEnum):
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    REJECTED_INTEGRITY = "REJECTED_INTEGRITY"


class ForwardOutcomeState(StrEnum):
    UNRESOLVED = "UNRESOLVED"
    RESOLVED_POSITIVE = "RESOLVED_POSITIVE"
    RESOLVED_NEGATIVE = "RESOLVED_NEGATIVE"
    INVALID_OUTCOME = "INVALID_OUTCOME"


class ChallengerEvaluationState(StrEnum):
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    COMPLETED = "COMPLETED"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"
    FAILED = "FAILED"


class ChallengerRecommendation(StrEnum):
    CONTINUE_SHADOW = "CONTINUE_SHADOW"
    ELIGIBLE_FOR_CHAMPION_REVIEW = "ELIGIBLE_FOR_CHAMPION_REVIEW"
    REJECT_CHALLENGER = "REJECT_CHALLENGER"
    PAUSE_FOR_DEGRADATION = "PAUSE_FOR_DEGRADATION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ChampionAssignmentState(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


@dataclass(frozen=True, slots=True)
class StrategyLabForwardRow:
    row_id: str
    entity_id: str
    grouping_id: str
    feature_at: datetime
    feature_values: tuple[Decimal | None, ...]
    baseline_probability: Decimal
    positive_outcome_identity: str
    source_lineage_hash: str
    source_configuration_hash: str
    integrity_state: str = "VALID"
    reason_codes: tuple[str, ...] = ()

    @property
    def canonical_hash(self) -> str:
        return content_digest(
            {
                "row_id": self.row_id,
                "entity_id": self.entity_id,
                "grouping_id": self.grouping_id,
                "feature_at": self.feature_at,
                "feature_values": self.feature_values,
                "baseline_probability": self.baseline_probability,
                "positive_outcome_identity": self.positive_outcome_identity,
                "source_lineage_hash": self.source_lineage_hash,
                "source_configuration_hash": self.source_configuration_hash,
                "integrity_state": self.integrity_state,
                "reason_codes": self.reason_codes,
            }
        )


@dataclass(frozen=True, slots=True)
class StrategyLabResolvedOutcome:
    outcome_evidence_id: str
    resolved_at: datetime
    positive: bool


class StrategyLabForwardFeatureAdapter(Protocol):
    adapter_id: str
    adapter_version: str
    feature_schema_hash: str

    def rows(
        self, *, cutoff_at: datetime, maximum_rows: int
    ) -> tuple[StrategyLabForwardRow, ...]: ...

    def source_identity(self, *, cutoff_at: datetime) -> str: ...

    def resolve(
        self, prediction: StrategyLabForwardPrediction, *, cutoff_at: datetime
    ) -> StrategyLabResolvedOutcome | None: ...


class StrategyLabVerifiedModel(Protocol):
    def predict(
        self, rows: Sequence[StrategyLabForwardRow]
    ) -> tuple[tuple[Decimal, Decimal], ...]: ...


class StrategyLabShadowModelLoader(Protocol):
    """Adapter-owned trusted model loader; requests never supply a path."""

    def verify(
        self, *, payload: bytes, deployment: StrategyLabModelDeployment
    ) -> StrategyLabVerifiedModel: ...


@dataclass(frozen=True, slots=True)
class StrategyLabShadowReview:
    review_id: str
    candidate_id: str
    requested_deployment_family: str
    requested_vertical: str
    state: ShadowReviewState
    requested_at: datetime
    started_at: datetime | None
    decided_at: datetime | None
    expires_at: datetime | None
    operator_reason: str
    reviewer_note: str
    operator_action_id: str | None
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StrategyLabShadowOperatorAction:
    """Immutable, research-only operator audit record.

    This intentionally does not reuse campaign-scoped financial operator
    actions: a model review is not a campaign action and has no capital scope.
    """

    action_id: str
    action_type: str
    target_id: str
    idempotency_key: str
    reason: str
    note: str
    created_at: datetime
    canonical_input_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class StrategyLabModelDeployment:
    deployment_id: str
    review_id: str
    candidate_id: str
    role: DeploymentRole
    vertical: str
    research_family: str
    model_family: ModelFamily
    feature_schema_id: str
    feature_schema_hash: str
    label_definition_id: str
    label_definition_hash: str
    dataset_id: str
    dataset_hash: str
    training_run_id: str
    training_hash: str
    evaluation_id: str
    evaluation_hash: str
    artifact: StrategyLabArtifact
    calibration_identity: str
    runtime_identity: str
    state: DeploymentState
    created_at: datetime
    activated_at: datetime | None
    paused_at: datetime | None
    retired_at: datetime | None
    invalidated_at: datetime | None
    operator_action_id: str
    canonical_payload: dict[str, object]
    canonical_hash: str
    last_integrity_check_at: datetime | None
    last_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyLabInferenceBatch:
    batch_id: str
    deployment_id: str
    adapter_id: str
    adapter_version: str
    feature_schema_hash: str
    source_cutoff: datetime
    started_at: datetime
    completed_at: datetime | None
    state: BatchState
    requested_row_count: int
    accepted_row_count: int
    rejected_row_count: int
    prediction_count: int
    baseline_prediction_count: int
    champion_prediction_count: int
    integrity_warnings: tuple[str, ...]
    source_batch_hash: str
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str | None
    last_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyLabForwardPrediction:
    prediction_id: str
    batch_id: str
    deployment_id: str
    candidate_id: str
    entity_id: str
    grouping_id: str
    feature_at: datetime
    predicted_at: datetime
    model_probability: Decimal
    calibrated_probability: Decimal
    baseline_probability: Decimal
    champion_probability: Decimal | None
    feature_values: tuple[Decimal | None, ...]
    feature_row_hash: str
    feature_schema_hash: str
    model_artifact_hash: str
    source_lineage_hash: str
    source_configuration_hash: str
    positive_outcome_identity: str
    outcome_state: ForwardOutcomeState
    canonical_prediction_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StrategyLabForwardEvaluation:
    evaluation_id: str
    prediction_id: str
    outcome_evidence_id: str
    resolved_at: datetime
    model_brier: Decimal
    calibrated_brier: Decimal
    baseline_brier: Decimal
    champion_brier: Decimal | None
    model_log_loss: Decimal
    calibrated_log_loss: Decimal
    baseline_log_loss: Decimal
    champion_log_loss: Decimal | None
    direction_correct: bool
    canonical_input_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class StrategyLabChampionAssignment:
    assignment_id: str
    vertical: str
    research_family: str
    deployment_id: str
    state: ChampionAssignmentState
    assigned_at: datetime
    superseded_at: datetime | None
    operator_action_id: str
    reason: str
    canonical_input_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class StrategyLabChallengerEvaluation:
    challenger_evaluation_id: str
    challenger_deployment_id: str
    champion_deployment_id: str | None
    cutoff_at: datetime
    prediction_count: int
    resolved_prediction_count: int
    distinct_group_count: int
    calibrated_brier: Decimal | None
    baseline_brier: Decimal | None
    champion_brier: Decimal | None
    calibrated_log_loss: Decimal | None
    baseline_log_loss: Decimal | None
    champion_log_loss: Decimal | None
    calibration_coverage: int
    recent_metrics: dict[str, object]
    degradation_metrics: dict[str, object]
    drift_metrics: dict[str, object]
    integrity_warnings: tuple[str, ...]
    state: ChallengerEvaluationState
    recommendation: ChallengerRecommendation
    reason_codes: tuple[str, ...]
    canonical_input_hash: str
    canonical_result_hash: str
    created_at: datetime


class StrategyLabShadowRepository(Protocol):
    def save_operator_action(
        self, action: StrategyLabShadowOperatorAction
    ) -> StrategyLabShadowOperatorAction: ...
    def review_by_idempotency(
        self, candidate_id: str, key: str
    ) -> StrategyLabShadowReview | None: ...
    def save_review(self, review: StrategyLabShadowReview) -> StrategyLabShadowReview: ...
    def review(self, review_id: str) -> StrategyLabShadowReview | None: ...
    def deployment(self, deployment_id: str) -> StrategyLabModelDeployment | None: ...
    def deployment_for_candidate(self, candidate_id: str) -> StrategyLabModelDeployment | None: ...
    def save_deployment(self, value: StrategyLabModelDeployment) -> StrategyLabModelDeployment: ...
    def batch_by_idempotency(
        self, deployment_id: str, key: str
    ) -> StrategyLabInferenceBatch | None: ...
    def save_batch(self, value: StrategyLabInferenceBatch) -> StrategyLabInferenceBatch: ...
    def save_prediction(
        self, value: StrategyLabForwardPrediction
    ) -> StrategyLabForwardPrediction: ...
    def predictions(self, deployment_id: str) -> tuple[StrategyLabForwardPrediction, ...]: ...
    def update_prediction(
        self, value: StrategyLabForwardPrediction
    ) -> StrategyLabForwardPrediction: ...
    def save_forward_evaluation(
        self, value: StrategyLabForwardEvaluation
    ) -> StrategyLabForwardEvaluation: ...
    def forward_evaluations(
        self, deployment_id: str
    ) -> tuple[StrategyLabForwardEvaluation, ...]: ...
    def active_champion(
        self, vertical: str, research_family: str
    ) -> StrategyLabChampionAssignment | None: ...
    def assign_champion(
        self, value: StrategyLabChampionAssignment
    ) -> StrategyLabChampionAssignment: ...
    def revoke_champion(
        self, assignment_id: str, *, revoked_at: datetime
    ) -> StrategyLabChampionAssignment: ...
    def save_challenger_evaluation(
        self, value: StrategyLabChallengerEvaluation
    ) -> StrategyLabChallengerEvaluation: ...
    def challenger_evaluation(
        self, deployment_id: str
    ) -> StrategyLabChallengerEvaluation | None: ...
    def reviews(self) -> tuple[StrategyLabShadowReview, ...]: ...
    def deployments(self) -> tuple[StrategyLabModelDeployment, ...]: ...
    def champions(self) -> tuple[StrategyLabChampionAssignment, ...]: ...


class InMemoryStrategyLabShadowRepository:
    def __init__(self) -> None:
        self._actions: dict[str, StrategyLabShadowOperatorAction] = {}
        self._reviews: dict[str, StrategyLabShadowReview] = {}
        self._deployments: dict[str, StrategyLabModelDeployment] = {}
        self._batches: dict[str, StrategyLabInferenceBatch] = {}
        self._predictions: dict[str, StrategyLabForwardPrediction] = {}
        self._evaluations: dict[str, StrategyLabForwardEvaluation] = {}
        self._champions: dict[str, StrategyLabChampionAssignment] = {}
        self._challengers: dict[str, StrategyLabChallengerEvaluation] = {}

    def save_operator_action(
        self, value: StrategyLabShadowOperatorAction
    ) -> StrategyLabShadowOperatorAction:
        existing = self._actions.get(value.action_id)
        if existing is not None and existing.canonical_input_hash != value.canonical_input_hash:
            raise StrategyLabError("SHADOW_ACTION_IDEMPOTENCY_CONFLICT")
        self._actions.setdefault(value.action_id, value)
        return self._actions[value.action_id]

    def review_by_idempotency(self, candidate_id: str, key: str) -> StrategyLabShadowReview | None:
        return next(
            (
                x
                for x in self._reviews.values()
                if x.candidate_id == candidate_id and x.idempotency_key == key
            ),
            None,
        )

    def save_review(self, value: StrategyLabShadowReview) -> StrategyLabShadowReview:
        self._reviews[value.review_id] = value
        return value

    def review(self, review_id: str) -> StrategyLabShadowReview | None:
        return self._reviews.get(review_id)

    def deployment(self, deployment_id: str) -> StrategyLabModelDeployment | None:
        return self._deployments.get(deployment_id)

    def deployment_for_candidate(self, candidate_id: str) -> StrategyLabModelDeployment | None:
        return next(
            (
                x
                for x in self._deployments.values()
                if x.candidate_id == candidate_id and x.state is not DeploymentState.REVOKED
            ),
            None,
        )

    def save_deployment(self, value: StrategyLabModelDeployment) -> StrategyLabModelDeployment:
        self._deployments[value.deployment_id] = value
        return value

    def batch_by_idempotency(
        self, deployment_id: str, key: str
    ) -> StrategyLabInferenceBatch | None:
        return next(
            (
                x
                for x in self._batches.values()
                if x.deployment_id == deployment_id and x.idempotency_key == key
            ),
            None,
        )

    def save_batch(self, value: StrategyLabInferenceBatch) -> StrategyLabInferenceBatch:
        self._batches[value.batch_id] = value
        return value

    def save_prediction(self, value: StrategyLabForwardPrediction) -> StrategyLabForwardPrediction:
        self._predictions.setdefault(value.prediction_id, value)
        return self._predictions[value.prediction_id]

    def predictions(self, deployment_id: str) -> tuple[StrategyLabForwardPrediction, ...]:
        return tuple(x for x in self._predictions.values() if x.deployment_id == deployment_id)

    def update_prediction(
        self, value: StrategyLabForwardPrediction
    ) -> StrategyLabForwardPrediction:
        self._predictions[value.prediction_id] = value
        return value

    def save_forward_evaluation(
        self, value: StrategyLabForwardEvaluation
    ) -> StrategyLabForwardEvaluation:
        self._evaluations.setdefault(value.evaluation_id, value)
        return self._evaluations[value.evaluation_id]

    def forward_evaluations(self, deployment_id: str) -> tuple[StrategyLabForwardEvaluation, ...]:
        ids = {x.prediction_id for x in self.predictions(deployment_id)}
        return tuple(x for x in self._evaluations.values() if x.prediction_id in ids)

    def active_champion(
        self, vertical: str, research_family: str
    ) -> StrategyLabChampionAssignment | None:
        return next(
            (
                x
                for x in self._champions.values()
                if x.vertical == vertical
                and x.research_family == research_family
                and x.state is ChampionAssignmentState.ACTIVE
            ),
            None,
        )

    def assign_champion(
        self, value: StrategyLabChampionAssignment
    ) -> StrategyLabChampionAssignment:
        for key, existing in tuple(self._champions.items()):
            if (
                existing.vertical == value.vertical
                and existing.research_family == value.research_family
                and existing.state is ChampionAssignmentState.ACTIVE
            ):
                self._champions[key] = replace(
                    existing,
                    state=ChampionAssignmentState.SUPERSEDED,
                    superseded_at=value.assigned_at,
                )
        self._champions[value.assignment_id] = value
        return value

    def revoke_champion(
        self, assignment_id: str, *, revoked_at: datetime
    ) -> StrategyLabChampionAssignment:
        item = self._champions.get(assignment_id)
        if item is None or item.state is not ChampionAssignmentState.ACTIVE:
            raise StrategyLabError("CHAMPION_ASSIGNMENT_NOT_ACTIVE")
        value = replace(item, state=ChampionAssignmentState.REVOKED, superseded_at=revoked_at)
        self._champions[assignment_id] = value
        return value

    def save_challenger_evaluation(
        self, value: StrategyLabChallengerEvaluation
    ) -> StrategyLabChallengerEvaluation:
        self._challengers[value.challenger_deployment_id] = value
        return value

    def challenger_evaluation(self, deployment_id: str) -> StrategyLabChallengerEvaluation | None:
        return self._challengers.get(deployment_id)

    def reviews(self) -> tuple[StrategyLabShadowReview, ...]:
        return tuple(self._reviews.values())

    def deployments(self) -> tuple[StrategyLabModelDeployment, ...]:
        return tuple(self._deployments.values())

    def champions(self) -> tuple[StrategyLabChampionAssignment, ...]:
        return tuple(self._champions.values())


def _loss(label: int, probability: Decimal) -> Decimal:
    bounded = min(max(probability, Decimal("0.000000000001")), Decimal("0.999999999999"))
    with localcontext() as context:
        context.prec = 28
        return parse_decimal(
            format(
                (
                    -(
                        Decimal(label) * bounded.ln()
                        + Decimal(1 - label) * (Decimal(1) - bounded).ln()
                    )
                ).quantize(Decimal("0.00000001")),
                "f",
            )
        )


class StrategyLabShadowService:
    def __init__(
        self,
        research: StrategyLabRepository,
        repository: StrategyLabShadowRepository,
        artifacts: StrategyLabArtifactStore,
        model_loader: StrategyLabShadowModelLoader,
    ) -> None:
        self._research, self._repository, self._artifacts, self._models = (
            research,
            repository,
            artifacts,
            model_loader,
        )

    def request_review(
        self,
        *,
        candidate_id: str,
        reason: str,
        idempotency_key: str,
        expires_at: datetime | None = None,
    ) -> StrategyLabShadowReview:
        candidate = self._candidate(candidate_id)
        if not reason or len(reason) > 500 or not idempotency_key:
            raise StrategyLabError("SHADOW_REVIEW_INPUT_INVALID")
        payload = {
            "candidate": candidate_id,
            "reason": reason,
            "key": idempotency_key,
            "expires": expires_at,
        }
        prior = self._repository.review_by_idempotency(candidate_id, idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != content_digest(payload):
                raise StrategyLabError("SHADOW_REVIEW_IDEMPOTENCY_CONFLICT")
            return prior
        now = datetime.now(tz=UTC)
        action = self._record_action(
            action_type="REQUEST_SHADOW_REVIEW",
            target_id=candidate_id,
            reason=reason,
            note="",
            idempotency_key=idempotency_key,
        )
        return self._repository.save_review(
            StrategyLabShadowReview(
                deterministic_id("strategy_lab_shadow_review", payload),
                candidate_id,
                candidate.model_family.value,
                self._dataset(candidate).schema.vertical,
                ShadowReviewState.REQUESTED,
                now,
                None,
                None,
                expires_at,
                reason,
                "",
                action.action_id,
                idempotency_key,
                content_digest(payload),
                None,
                ("RESEARCH_ONLY",),
            )
        )

    def start_review(
        self, *, review_id: str, reason: str, idempotency_key: str
    ) -> StrategyLabShadowReview:
        review = self._require_review(review_id)
        if review.state is not ShadowReviewState.REQUESTED:
            raise StrategyLabError("SHADOW_REVIEW_NOT_REQUESTED")
        action = self._record_action(
            action_type="START_SHADOW_REVIEW",
            target_id=review_id,
            reason=reason,
            note="",
            idempotency_key=idempotency_key,
        )
        return self._repository.save_review(
            replace(
                review,
                state=ShadowReviewState.IN_REVIEW,
                started_at=datetime.now(tz=UTC),
                reviewer_note=reason,
                operator_action_id=action.action_id,
            )
        )

    def approve_review(
        self,
        *,
        review_id: str,
        role: DeploymentRole,
        candidate_id: str,
        dataset_id: str,
        artifact_sha256: str,
        evaluation_id: str,
        reason: str,
        idempotency_key: str,
    ) -> StrategyLabModelDeployment:
        review = self._require_review(review_id)
        if review.state is not ShadowReviewState.IN_REVIEW:
            raise StrategyLabError("SHADOW_REVIEW_NOT_IN_REVIEW")
        candidate = self._candidate(review.candidate_id)
        dataset = self._dataset(candidate)
        run = self._run(candidate)
        evaluation = self._research.evaluation(candidate.evaluation_id)
        if (candidate_id, dataset_id, artifact_sha256, evaluation_id) != (
            candidate.candidate_id,
            dataset.dataset_id,
            candidate.model_artifact_hash,
            candidate.evaluation_id,
        ):
            raise StrategyLabError("SHADOW_APPROVAL_CONFIRMATION_MISMATCH")
        if (
            dataset.state is not DatasetState.READY
            or run.state is not TrainingRunState.COMPLETED
            or run.model_artifact is None
            or evaluation is None
            or evaluation.state.value != "COMPLETED"
        ):
            raise StrategyLabError("SHADOW_DEPLOYMENT_LINEAGE_INVALID")
        if (
            dataset.integrity_warnings
            or evaluation.integrity_warnings
            or not self._artifacts.exists(run.model_artifact)
        ):
            raise StrategyLabError("SHADOW_DEPLOYMENT_INTEGRITY_INVALID")
        if self._repository.deployment_for_candidate(candidate.candidate_id) is not None:
            raise StrategyLabError("SHADOW_DEPLOYMENT_CONFLICT")
        now = datetime.now(tz=UTC)
        action = self._record_action(
            action_type="APPROVE_SHADOW_DEPLOYMENT",
            target_id=review_id,
            reason=reason,
            note="",
            idempotency_key=idempotency_key,
        )
        payload: dict[str, object] = {
            "review": review_id,
            "candidate": candidate.candidate_id,
            "role": role.value,
            "feature_names": dataset.schema.feature_names,
            "artifact": run.model_artifact.sha256,
            "runtime": run.runtime_identity,
        }
        deployment = StrategyLabModelDeployment(
            deterministic_id("strategy_lab_shadow_deployment", payload),
            review_id,
            candidate.candidate_id,
            role,
            dataset.schema.vertical,
            dataset.schema.research_family,
            run.model_family,
            dataset.schema.schema_id,
            dataset.schema.canonical_hash,
            dataset.label_definition.label_id,
            dataset.label_definition.canonical_hash,
            dataset.dataset_id,
            dataset.canonical_result_hash or "",
            run.training_run_id,
            run.canonical_result_hash or "",
            candidate.evaluation_id,
            evaluation.canonical_result_hash or "",
            run.model_artifact,
            run.calibration.value,
            run.runtime_identity,
            DeploymentState.ACTIVE,
            now,
            now,
            None,
            None,
            None,
            action.action_id,
            payload,
            content_digest(payload),
            now,
        )
        try:
            self._models.verify(
                payload=self._artifacts.read_bytes(run.model_artifact), deployment=deployment
            )
        except StrategyLabError:
            raise
        except Exception as error:
            raise StrategyLabError("SHADOW_DEPLOYMENT_ARTIFACT_INVALID") from error
        self._repository.save_review(
            replace(
                review,
                state=ShadowReviewState.APPROVED,
                decided_at=now,
                reviewer_note=reason,
                operator_action_id=action.action_id,
                canonical_result_hash=content_digest(payload),
            )
        )
        return self._repository.save_deployment(deployment)

    def decide_review(
        self,
        *,
        review_id: str,
        state: ShadowReviewState,
        reason: str,
        note: str,
        idempotency_key: str,
    ) -> StrategyLabShadowReview:
        """Record a non-approval review decision with a durable action."""
        if state not in {
            ShadowReviewState.REJECTED,
            ShadowReviewState.DEFERRED,
            ShadowReviewState.CANCELED,
        }:
            raise StrategyLabError("SHADOW_REVIEW_DECISION_INVALID")
        review = self._require_review(review_id)
        allowed = (
            review.state is ShadowReviewState.IN_REVIEW
            if state in {ShadowReviewState.REJECTED, ShadowReviewState.DEFERRED}
            else review.state in {ShadowReviewState.REQUESTED, ShadowReviewState.IN_REVIEW}
        )
        if not allowed:
            raise StrategyLabError("SHADOW_REVIEW_TRANSITION_INVALID")
        action = self._record_action(
            action_type=f"{state.value}_SHADOW_REVIEW",
            target_id=review_id,
            reason=reason,
            note=note,
            idempotency_key=idempotency_key,
        )
        return self._repository.save_review(
            replace(
                review,
                state=state,
                decided_at=datetime.now(tz=UTC),
                reviewer_note=note,
                operator_action_id=action.action_id,
                reason_codes=(state.value,),
                canonical_result_hash=content_digest({"state": state.value, "reason": reason}),
            )
        )

    def expire_reviews(
        self, *, now: datetime, idempotency_key: str
    ) -> tuple[StrategyLabShadowReview, ...]:
        if now.tzinfo is None or not idempotency_key:
            raise StrategyLabError("SHADOW_REVIEW_EXPIRY_INPUT_INVALID")
        expired: list[StrategyLabShadowReview] = []
        for review in self._repository.reviews():
            if (
                review.state in {ShadowReviewState.REQUESTED, ShadowReviewState.IN_REVIEW}
                and review.expires_at is not None
                and review.expires_at <= now
            ):
                action = self._record_action(
                    action_type="EXPIRE_SHADOW_REVIEW",
                    target_id=review.review_id,
                    reason="review expiry reached",
                    note="",
                    idempotency_key=f"{idempotency_key}:{review.review_id}",
                )
                expired.append(
                    self._repository.save_review(
                        replace(
                            review,
                            state=ShadowReviewState.EXPIRED,
                            decided_at=now,
                            operator_action_id=action.action_id,
                            reason_codes=("REVIEW_EXPIRED",),
                            canonical_result_hash=content_digest({"state": "EXPIRED", "at": now}),
                        )
                    )
                )
        return tuple(expired)

    def control_deployment(
        self, *, deployment_id: str, action: str, reason: str, idempotency_key: str
    ) -> StrategyLabModelDeployment:
        deployment = self._require_deployment(deployment_id)
        now = datetime.now(tz=UTC)
        allowed = {
            "pause": (DeploymentState.ACTIVE, DeploymentState.PAUSED),
            "resume": (DeploymentState.PAUSED, DeploymentState.ACTIVE),
            "revoke": (DeploymentState.ACTIVE, DeploymentState.REVOKED),
            "retire": (DeploymentState.PAUSED, DeploymentState.RETIRED),
        }
        if not reason or len(reason) > 500 or not idempotency_key:
            raise StrategyLabError("SHADOW_DEPLOYMENT_CONTROL_INPUT_INVALID")
        if action not in allowed or deployment.state is not allowed[action][0]:
            raise StrategyLabError("SHADOW_DEPLOYMENT_TRANSITION_INVALID")
        self._record_action(
            action_type=f"{action.upper()}_SHADOW_DEPLOYMENT",
            target_id=deployment_id,
            reason=reason,
            note="",
            idempotency_key=idempotency_key,
        )
        if action == "resume":
            try:
                self._models.verify(
                    payload=self._artifacts.read_bytes(deployment.artifact), deployment=deployment
                )
            except Exception:
                return self._repository.save_deployment(
                    replace(
                        deployment,
                        state=DeploymentState.INVALIDATED,
                        invalidated_at=now,
                        last_error_code="MODEL_ARTIFACT_HASH_MISMATCH",
                    )
                )
        target = allowed[action][1]
        return self._repository.save_deployment(
            replace(
                deployment,
                state=target,
                paused_at=now if target is DeploymentState.PAUSED else deployment.paused_at,
                retired_at=now if target is DeploymentState.RETIRED else deployment.retired_at,
                last_error_code=None,
            )
        )

    def run_inference(
        self,
        *,
        deployment_id: str,
        adapter: StrategyLabForwardFeatureAdapter,
        cutoff_at: datetime,
        maximum_rows: int,
        idempotency_key: str,
    ) -> StrategyLabInferenceBatch:
        if not 1 <= maximum_rows <= 10_000 or cutoff_at.tzinfo is None:
            raise StrategyLabError("SHADOW_BATCH_INPUT_INVALID")
        deployment = self._require_deployment(deployment_id)
        payload = {
            "deployment": deployment_id,
            "adapter": adapter.adapter_id,
            "version": adapter.adapter_version,
            "cutoff": cutoff_at,
            "maximum": maximum_rows,
            "key": idempotency_key,
        }
        prior = self._repository.batch_by_idempotency(deployment_id, idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != content_digest(payload):
                raise StrategyLabError("SHADOW_BATCH_IDEMPOTENCY_CONFLICT")
            return prior
        if (
            deployment.state is not DeploymentState.ACTIVE
            or adapter.feature_schema_hash != deployment.feature_schema_hash
        ):
            raise StrategyLabError("SHADOW_INFERENCE_NOT_AUTHORIZED")
        now = datetime.now(tz=UTC)
        try:
            model = self._models.verify(
                payload=self._artifacts.read_bytes(deployment.artifact), deployment=deployment
            )
        except Exception as error:
            self._repository.save_deployment(
                replace(
                    deployment,
                    state=DeploymentState.INVALIDATED,
                    invalidated_at=now,
                    last_error_code="MODEL_ARTIFACT_HASH_MISMATCH",
                )
            )
            raise StrategyLabError("SHADOW_ARTIFACT_INTEGRITY_FAILURE") from error
        rows = tuple(
            sorted(
                adapter.rows(cutoff_at=cutoff_at, maximum_rows=maximum_rows),
                key=lambda x: (x.feature_at, x.grouping_id, x.entity_id, x.row_id),
            )
        )
        feature_names = cast(Sequence[str], deployment.canonical_payload["feature_names"])
        accepted = tuple(
            x
            for x in rows
            if x.integrity_state == "VALID" and len(x.feature_values) == len(feature_names)
        )
        if len({(x.entity_id, x.feature_at) for x in accepted}) != len(accepted):
            raise StrategyLabError("SHADOW_FORWARD_DUPLICATE_SOURCE_ROW")
        if len({x.source_configuration_hash for x in accepted}) > 1:
            raise StrategyLabError("SHADOW_FORWARD_CONFIGURATION_MISMATCH")
        values = model.predict(accepted)
        source_hash = content_digest(tuple(x.canonical_hash for x in accepted))
        batch = StrategyLabInferenceBatch(
            deterministic_id("strategy_lab_shadow_batch", payload),
            deployment_id,
            adapter.adapter_id,
            adapter.adapter_version,
            deployment.feature_schema_hash,
            cutoff_at,
            now,
            None,
            BatchState.RUNNING,
            len(rows),
            len(accepted),
            len(rows) - len(accepted),
            0,
            0,
            0,
            (),
            source_hash,
            idempotency_key,
            content_digest(payload),
            None,
        )
        self._repository.save_batch(batch)
        champion = self._repository.active_champion(deployment.vertical, deployment.research_family)
        champion_model: StrategyLabVerifiedModel | None = None
        if champion is not None and champion.deployment_id != deployment_id:
            champion_deployment = self._require_deployment(champion.deployment_id)
            if (
                champion_deployment.state is DeploymentState.ACTIVE
                and champion_deployment.feature_schema_hash == deployment.feature_schema_hash
            ):
                champion_model = self._models.verify(
                    payload=self._artifacts.read_bytes(champion_deployment.artifact),
                    deployment=champion_deployment,
                )
        champion_values = champion_model.predict(accepted) if champion_model is not None else ()
        for index, (row, pair) in enumerate(zip(accepted, values, strict=True)):
            champion_probability = champion_values[index][1] if champion_model is not None else None
            p_payload = {
                "deployment": deployment_id,
                "entity": row.entity_id,
                "feature_at": row.feature_at,
                "row": row.canonical_hash,
            }
            prediction = StrategyLabForwardPrediction(
                deterministic_id("strategy_lab_forward_prediction", p_payload),
                batch.batch_id,
                deployment_id,
                deployment.candidate_id,
                row.entity_id,
                row.grouping_id,
                row.feature_at,
                now,
                pair[0],
                pair[1],
                row.baseline_probability,
                champion_probability,
                row.feature_values,
                row.canonical_hash,
                deployment.feature_schema_hash,
                deployment.artifact.sha256,
                row.source_lineage_hash,
                row.source_configuration_hash,
                row.positive_outcome_identity,
                ForwardOutcomeState.UNRESOLVED,
                content_digest(p_payload),
                now,
            )
            self._repository.save_prediction(prediction)
        result = replace(
            batch,
            state=BatchState.COMPLETED,
            completed_at=datetime.now(tz=UTC),
            prediction_count=len(accepted),
            baseline_prediction_count=len(accepted),
            champion_prediction_count=len(champion_values),
            canonical_result_hash=content_digest({"source": source_hash, "count": len(accepted)}),
        )
        return self._repository.save_batch(result)

    def resolve_predictions(
        self,
        *,
        deployment_id: str,
        adapter: StrategyLabForwardFeatureAdapter,
        cutoff_at: datetime,
        batch_ids: Sequence[str] | None = None,
    ) -> int:
        count = 0
        selected_batches = set(batch_ids) if batch_ids is not None else None
        for prediction in self._repository.predictions(deployment_id):
            if selected_batches is not None and prediction.batch_id not in selected_batches:
                continue
            if prediction.outcome_state is not ForwardOutcomeState.UNRESOLVED:
                continue
            outcome = adapter.resolve(prediction, cutoff_at=cutoff_at)
            if outcome is None:
                continue
            label = int(outcome.positive)
            state = (
                ForwardOutcomeState.RESOLVED_POSITIVE
                if outcome.positive
                else ForwardOutcomeState.RESOLVED_NEGATIVE
            )
            updated = self._repository.update_prediction(replace(prediction, outcome_state=state))
            payload = {
                "prediction": updated.canonical_prediction_hash,
                "outcome": outcome.outcome_evidence_id,
            }

            def square(probability: Decimal, outcome_label: int) -> Decimal:
                return parse_decimal(
                    format(
                        ((probability - Decimal(outcome_label)) ** 2).quantize(
                            Decimal("0.00000001")
                        ),
                        "f",
                    )
                )

            evaluation = StrategyLabForwardEvaluation(
                deterministic_id("strategy_lab_forward_evaluation", payload),
                updated.prediction_id,
                outcome.outcome_evidence_id,
                outcome.resolved_at,
                square(updated.model_probability, label),
                square(updated.calibrated_probability, label),
                square(updated.baseline_probability, label),
                square(updated.champion_probability, label)
                if updated.champion_probability is not None
                else None,
                _loss(label, updated.model_probability),
                _loss(label, updated.calibrated_probability),
                _loss(label, updated.baseline_probability),
                _loss(label, updated.champion_probability)
                if updated.champion_probability is not None
                else None,
                (updated.calibrated_probability >= Decimal("0.5")) == bool(label),
                content_digest(payload),
                content_digest({"label": label, "state": state.value}),
            )
            self._repository.save_forward_evaluation(evaluation)
            count += 1
        return count

    def evaluate_challenger(
        self,
        *,
        deployment_id: str,
        cutoff_at: datetime,
        batch_ids: Sequence[str] | None = None,
    ) -> StrategyLabChallengerEvaluation:
        deployment = self._require_deployment(deployment_id)
        selected_batches = set(batch_ids) if batch_ids is not None else None
        predictions = tuple(
            x
            for x in self._repository.predictions(deployment_id)
            if x.feature_at <= cutoff_at
            and (selected_batches is None or x.batch_id in selected_batches)
        )
        evaluations = self._repository.forward_evaluations(deployment_id)
        by_id = {x.prediction_id: x for x in evaluations}
        groups = {x.grouping_id for x in predictions if x.prediction_id in by_id}
        resolved = tuple(by_id[x.prediction_id] for x in predictions if x.prediction_id in by_id)

        def average(name: str) -> Decimal | None:
            values = [getattr(item, name) for item in resolved if getattr(item, name) is not None]
            if not values:
                return None
            return parse_decimal(
                format(
                    (sum(values, Decimal()) / Decimal(len(values))).quantize(Decimal("0.00000001")),
                    "f",
                )
            )

        calibrated_brier, baseline_brier, champion_brier = (
            average("calibrated_brier"),
            average("baseline_brier"),
            average("champion_brier"),
        )
        calibrated_loss, baseline_loss, champion_loss = (
            average("calibrated_log_loss"),
            average("baseline_log_loss"),
            average("champion_log_loss"),
        )
        coverage = len(
            {
                min(4, int(float(prediction.calibrated_probability) * 5))
                for prediction in predictions
                if prediction.prediction_id in by_id
            }
        )
        reasons: list[str] = []
        state = ChallengerEvaluationState.COMPLETED
        recommendation = ChallengerRecommendation.CONTINUE_SHADOW
        if len(resolved) < 100 or len(groups) < 30:
            state = ChallengerEvaluationState.INSUFFICIENT_EVIDENCE
            recommendation = ChallengerRecommendation.INSUFFICIENT_EVIDENCE
            reasons.append("MINIMUM_FORWARD_EVIDENCE_NOT_MET")
        elif (
            calibrated_brier is None
            or baseline_brier is None
            or calibrated_loss is None
            or baseline_loss is None
            or coverage < 3
        ):
            recommendation = ChallengerRecommendation.REJECT_CHALLENGER
            reasons.append("CALIBRATION_OR_BASELINE_INSUFFICIENT")
        elif calibrated_brier > baseline_brier or calibrated_loss > baseline_loss:
            recommendation = ChallengerRecommendation.REJECT_CHALLENGER
            reasons.append("WORSE_THAN_BASELINE")
        elif (
            champion_brier is not None
            and champion_loss is not None
            and ((calibrated_brier > champion_brier) or (calibrated_loss > champion_loss))
        ):
            recommendation = ChallengerRecommendation.REJECT_CHALLENGER
            reasons.append("NOT_CHAMPION_COMPETITIVE")
        elif (
            champion_brier is None
            or champion_loss is None
            or calibrated_brier < champion_brier
            or calibrated_loss < champion_loss
        ):
            recommendation = ChallengerRecommendation.ELIGIBLE_FOR_CHAMPION_REVIEW
        drift = self._drift_metrics(deployment, predictions, resolved)
        drift_state = str(drift["state"])
        if drift_state == "DEGRADED" and recommendation is ChallengerRecommendation.CONTINUE_SHADOW:
            recommendation = ChallengerRecommendation.PAUSE_FOR_DEGRADATION
            reasons.append("SEVERE_FORWARD_DRIFT")
        payload = {
            "deployment": deployment_id,
            "cutoff": cutoff_at,
            "prediction_count": len(predictions),
            "resolved": len(resolved),
            "recommendation": recommendation.value,
        }
        champion = self._repository.active_champion(deployment.vertical, deployment.research_family)
        result = StrategyLabChallengerEvaluation(
            deterministic_id("strategy_lab_challenger_evaluation", payload),
            deployment_id,
            champion.deployment_id if champion is not None else None,
            cutoff_at,
            len(predictions),
            len(resolved),
            len(groups),
            calibrated_brier,
            baseline_brier,
            champion_brier,
            calibrated_loss,
            baseline_loss,
            champion_loss,
            coverage,
            {"resolved": len(resolved)},
            {
                "brier_minus_baseline": str(calibrated_brier - baseline_brier)
                if calibrated_brier is not None and baseline_brier is not None
                else None
            },
            drift,
            (),
            state,
            recommendation,
            tuple(reasons),
            content_digest(payload),
            content_digest({"state": state.value, "reasons": reasons}),
            datetime.now(tz=UTC),
        )
        return self._repository.save_challenger_evaluation(result)

    def _drift_metrics(
        self,
        deployment: StrategyLabModelDeployment,
        predictions: Sequence[StrategyLabForwardPrediction],
        resolved: Sequence[StrategyLabForwardEvaluation],
    ) -> dict[str, object]:
        """Versioned transparent components; deliberately no opaque score."""
        if not predictions:
            return {
                "version": "V1",
                "state": "INSUFFICIENT_EVIDENCE",
                "reason_codes": ["NO_PREDICTIONS"],
            }
        candidate = self._research.candidate(deployment.candidate_id)
        if candidate is None:
            return {
                "version": "V1",
                "state": "BLOCKED_INTEGRITY",
                "reason_codes": ["CANDIDATE_MISSING"],
            }
        dataset = self._dataset(candidate)
        if dataset.artifact is None:
            return {
                "version": "V1",
                "state": "BLOCKED_INTEGRITY",
                "reason_codes": ["TRAINING_ARTIFACT_MISSING"],
            }
        training = load_dataset_artifact(
            artifact=dataset.artifact, store=self._artifacts, schema=dataset.schema
        )
        recent = tuple(
            sorted(predictions, key=lambda item: item.feature_at)[-max(1, len(predictions) // 5) :]
        )
        feature_metrics: dict[str, object] = {}
        severe = False
        watch = False
        for index, name in enumerate(dataset.schema.feature_names):
            train_values = [
                cast(Decimal, row.feature_values[index])
                for row in training
                if row.feature_values[index] is not None
            ]
            recent_values = [
                cast(Decimal, row.feature_values[index])
                for row in recent
                if row.feature_values[index] is not None
            ]
            train_missing = Decimal(len(training) - len(train_values)) / Decimal(
                max(1, len(training))
            )
            recent_missing = Decimal(len(recent) - len(recent_values)) / Decimal(
                max(1, len(recent))
            )
            if not train_values or not recent_values:
                feature_metrics[name] = {
                    "training_missingness": str(train_missing),
                    "recent_missingness": str(recent_missing),
                    "state": "INSUFFICIENT",
                }
                watch = True
                continue
            mean = sum(train_values, Decimal()) / Decimal(len(train_values))
            recent_mean = sum(recent_values, Decimal()) / Decimal(len(recent_values))
            variance = sum((value - mean) ** 2 for value in train_values) / Decimal(
                len(train_values)
            )
            standard_deviation = variance.sqrt() if variance else Decimal()
            standardized_shift = (
                abs(recent_mean - mean) / standard_deviation if standard_deviation else Decimal()
            )
            lower, upper = min(train_values), max(train_values)
            range_violations = sum(value < lower or value > upper for value in recent_values)
            missingness_delta = recent_missing - train_missing
            if standardized_shift > Decimal("3") or range_violations == len(recent_values):
                severe = True
            elif standardized_shift > Decimal("2") or abs(missingness_delta) > Decimal("0.2"):
                watch = True
            feature_metrics[name] = {
                "training_mean": str(mean),
                "recent_mean": str(recent_mean),
                "training_standard_deviation": str(standard_deviation),
                "standardized_mean_shift": str(standardized_shift),
                "range_violation_count": range_violations,
                "training_missingness": str(train_missing),
                "recent_missingness": str(recent_missing),
                "missingness_delta": str(missingness_delta),
            }

        def distribution(values: Sequence[Decimal]) -> tuple[Decimal, ...]:
            bins = [Decimal() for _ in range(5)]
            for value in values:
                bins[min(4, int(value * 5))] += 1
            return tuple(value / Decimal(max(1, len(values))) for value in bins)

        def psi(reference: Sequence[Decimal], observed: Sequence[Decimal]) -> Decimal:
            floor = Decimal("0.000001")
            return sum(
                (
                    (new - old) * (max(new, floor) / max(old, floor)).ln()
                    for old, new in zip(reference, observed, strict=True)
                ),
                Decimal(),
            )

        probability_metrics: dict[str, object] = {}
        probability_series: tuple[tuple[str, list[Decimal], list[Decimal]], ...] = (
            (
                "baseline",
                [item.baseline_probability for item in predictions],
                [item.baseline_probability for item in recent],
            ),
            (
                "model",
                [item.model_probability for item in predictions],
                [item.model_probability for item in recent],
            ),
            (
                "calibrated",
                [item.calibrated_probability for item in predictions],
                [item.calibrated_probability for item in recent],
            ),
        )
        for name, all_values, recent_values in probability_series:
            score = psi(distribution(all_values), distribution(recent_values))
            if score > Decimal("0.25"):
                severe = True
            elif score > Decimal("0.1"):
                watch = True
            probability_metrics[name] = {
                "training_or_full_bucket_distribution": [
                    str(value) for value in distribution(all_values)
                ],
                "recent_bucket_distribution": [str(value) for value in distribution(recent_values)],
                "population_stability_index": str(score),
            }
        group_counts: dict[str, int] = {}
        for item in recent:
            group_counts[item.grouping_id] = group_counts.get(item.grouping_id, 0) + 1
        concentration = Decimal(max(group_counts.values())) / Decimal(len(recent))
        if concentration > Decimal("0.7"):
            severe = True
        elif concentration > Decimal("0.4"):
            watch = True
        recent_scores = [
            item
            for item in resolved
            if item.prediction_id in {prediction.prediction_id for prediction in recent}
        ]
        full_brier = sum((item.calibrated_brier for item in resolved), Decimal()) / Decimal(
            max(1, len(resolved))
        )
        recent_brier = sum((item.calibrated_brier for item in recent_scores), Decimal()) / Decimal(
            max(1, len(recent_scores))
        )
        full_loss = sum((item.calibrated_log_loss for item in resolved), Decimal()) / Decimal(
            max(1, len(resolved))
        )
        recent_loss = sum(
            (item.calibrated_log_loss for item in recent_scores), Decimal()
        ) / Decimal(max(1, len(recent_scores)))
        if recent_scores and (
            recent_brier - full_brier > Decimal("0.05") or recent_loss - full_loss > Decimal("0.1")
        ):
            severe = True
        return {
            "version": "V1",
            "state": "DEGRADED" if severe else "WATCH" if watch else "STABLE",
            "feature_metrics": feature_metrics,
            "probability_metrics": probability_metrics,
            "grouping_mix": {
                "training_group_coverage": dataset.grouping_count,
                "recent_group_coverage": len(group_counts),
                "largest_group_concentration": str(concentration),
            },
            "prediction_coverage": {
                "accepted": len(predictions),
                "rejected": 0,
                "coverage_delta": "0",
            },
            "recent_score_degradation": {
                "resolved_recent": len(recent_scores),
                "brier_delta": str(recent_brier - full_brier),
                "log_loss_delta": str(recent_loss - full_loss),
            },
        }

    def assign_champion(
        self, *, deployment_id: str, reason: str, confirm_deployment: str, idempotency_key: str
    ) -> StrategyLabChampionAssignment:
        deployment = self._require_deployment(deployment_id)
        evaluation = self._repository.challenger_evaluation(deployment_id)
        if (
            confirm_deployment != deployment_id
            or deployment.state is not DeploymentState.ACTIVE
            or evaluation is None
            or evaluation.recommendation
            is not ChallengerRecommendation.ELIGIBLE_FOR_CHAMPION_REVIEW
        ):
            raise StrategyLabError("CHAMPION_ASSIGNMENT_NOT_APPROVED")
        now = datetime.now(tz=UTC)
        payload = {"deployment": deployment_id, "reason": reason, "key": idempotency_key}
        assignment = StrategyLabChampionAssignment(
            deterministic_id("strategy_lab_champion_assignment", payload),
            deployment.vertical,
            deployment.research_family,
            deployment_id,
            ChampionAssignmentState.ACTIVE,
            now,
            None,
            self._record_action(
                action_type="ASSIGN_SHADOW_CHAMPION",
                target_id=deployment_id,
                reason=reason,
                note="",
                idempotency_key=idempotency_key,
            ).action_id,
            reason,
            content_digest(payload),
            content_digest({"state": "ACTIVE"}),
        )
        return self._repository.assign_champion(assignment)

    def revoke_champion(
        self, *, assignment_id: str, reason: str, idempotency_key: str
    ) -> StrategyLabChampionAssignment:
        self._record_action(
            action_type="REVOKE_SHADOW_CHAMPION",
            target_id=assignment_id,
            reason=reason,
            note="",
            idempotency_key=idempotency_key,
        )
        return self._repository.revoke_champion(assignment_id, revoked_at=datetime.now(tz=UTC))

    def _candidate(self, candidate_id: str) -> StrategyLabModelCandidate:
        candidate = self._research.candidate(candidate_id)
        if candidate is None or candidate.state is not CandidateState.ELIGIBLE_FOR_SHADOW_REVIEW:
            raise StrategyLabError("CANDIDATE_NOT_ELIGIBLE_FOR_SHADOW")
        return candidate

    def _dataset(self, candidate: StrategyLabModelCandidate) -> StrategyLabDatasetSnapshot:
        run = self._run(candidate)
        dataset = self._research.dataset(run.dataset_id)
        if dataset is None:
            raise StrategyLabError("SHADOW_DATASET_MISSING")
        return dataset

    def _run(self, candidate: StrategyLabModelCandidate) -> StrategyLabTrainingRun:
        run = self._research.training(candidate.training_run_id)
        if run is None:
            raise StrategyLabError("SHADOW_TRAINING_MISSING")
        return run

    def _require_review(self, value: str) -> StrategyLabShadowReview:
        review = self._repository.review(value)
        if review is None:
            raise StrategyLabError("SHADOW_REVIEW_NOT_FOUND")
        return review

    def _require_deployment(self, value: str) -> StrategyLabModelDeployment:
        deployment = self._repository.deployment(value)
        if deployment is None:
            raise StrategyLabError("SHADOW_DEPLOYMENT_NOT_FOUND")
        return deployment

    def _record_action(
        self,
        *,
        action_type: str,
        target_id: str,
        reason: str,
        note: str,
        idempotency_key: str,
    ) -> StrategyLabShadowOperatorAction:
        if not reason or len(reason) > 500 or len(note) > 500 or not idempotency_key:
            raise StrategyLabError("SHADOW_OPERATOR_ACTION_INPUT_INVALID")
        payload = {
            "action_type": action_type,
            "target_id": target_id,
            "reason": reason,
            "note": note,
            "idempotency_key": idempotency_key,
        }
        return self._repository.save_operator_action(
            StrategyLabShadowOperatorAction(
                deterministic_id("strategy_lab_shadow_operator_action", payload),
                action_type,
                target_id,
                idempotency_key,
                reason,
                note,
                datetime.now(tz=UTC),
                content_digest(payload),
                content_digest({"action_type": action_type, "target_id": target_id}),
            )
        )
