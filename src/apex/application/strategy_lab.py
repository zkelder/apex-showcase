"""Provider-neutral, research-only supervised-learning contracts and services.

This module deliberately owns no strategy governance, campaigns, orders,
positions, reservations, settlement, or ledger access.  A qualified candidate
is evidence for later human review only.
"""

from __future__ import annotations

import io
import json
import platform
import sys
import warnings
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Any, Protocol, cast

import numpy as np
import sklearn  # type: ignore[import-untyped]
from joblib import dump as joblib_dump  # type: ignore[import-untyped]
from sklearn.ensemble import HistGradientBoostingClassifier  # type: ignore[import-untyped]
from sklearn.isotonic import IsotonicRegression  # type: ignore[import-untyped]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]

from apex.application.research_integrity import (
    ResearchMultipleTestingEvaluation,
    ResearchRecommendation,
)
from apex.domain.values import (
    ValueValidationError,
    canonical_json_bytes,
    content_digest,
    deterministic_id,
    parse_decimal,
)


class StrategyLabError(RuntimeError):
    """A deterministic Strategy Lab research gate rejected input or evidence."""


class StrategyLabArtifactError(RuntimeError):
    """A trusted managed artifact did not satisfy its durable identity."""


def research_integrity_candidate_rejection_reasons(
    evaluation: ResearchMultipleTestingEvaluation | None,
) -> tuple[str, ...]:
    if (
        evaluation is None
        or evaluation.recommendation is ResearchRecommendation.ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW
    ):
        return ()
    return ("RESEARCH_SELECTION_INTEGRITY_NOT_ELIGIBLE",)


@dataclass(frozen=True, slots=True)
class StrategyLabArtifact:
    """Immutable metadata for a generated research artifact."""

    relative_location: str
    sha256: str
    byte_size: int


class StrategyLabArtifactStore(Protocol):
    """Port for the only allowed artifact boundary.

    Application services receive generated identifiers only; this prevents a
    caller from turning the training path into arbitrary-file loading.
    """

    def write_bytes(self, relative_location: str, payload: bytes) -> StrategyLabArtifact: ...

    def read_bytes(self, artifact: StrategyLabArtifact) -> bytes: ...

    def exists(self, artifact: StrategyLabArtifact) -> bool: ...


class DatasetState(StrEnum):
    BUILDING = "BUILDING"
    READY = "READY"
    INVALID = "INVALID"
    SUPERSEDED = "SUPERSEDED"


class TrainingRunState(StrEnum):
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED_INTEGRITY = "REJECTED_INTEGRITY"


class EvaluationState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    INVALID = "INVALID"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    FAILED = "FAILED"


class CandidateState(StrEnum):
    TRAINED = "TRAINED"
    EVALUATED = "EVALUATED"
    REJECTED = "REJECTED"
    ELIGIBLE_FOR_SHADOW_REVIEW = "ELIGIBLE_FOR_SHADOW_REVIEW"
    ARCHIVED = "ARCHIVED"


class ModelFamily(StrEnum):
    LOGISTIC_REGRESSION = "logistic-regression-probability-v1"
    HISTOGRAM_GRADIENT_BOOSTING = "histogram-gradient-boosting-probability-v1"


class CalibrationMethod(StrEnum):
    NONE = "none"
    PLATT = "platt"
    ISOTONIC = "isotonic"


@dataclass(frozen=True, slots=True)
class StrategyLabFeatureDefinition:
    name: str
    data_type: str
    nullable: bool
    normalization_identity: str
    source_availability_requirement: str

    def __post_init__(self) -> None:
        if not self.name.isidentifier() or self.data_type != "DECIMAL":
            raise ValueValidationError("feature definition is invalid")
        if not self.normalization_identity or not self.source_availability_requirement:
            raise ValueValidationError("feature definition provenance is required")


@dataclass(frozen=True, slots=True)
class StrategyLabFeatureSchema:
    schema_id: str
    vertical: str
    research_family: str
    version: str
    features: tuple[StrategyLabFeatureDefinition, ...]
    created_at: datetime
    superseded_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.schema_id or not self.vertical or not self.research_family or not self.version:
            raise ValueValidationError("feature schema identity is required")
        if not self.features or len(self.features) > 32:
            raise ValueValidationError("feature schema must have between one and 32 features")
        if len({item.name for item in self.features}) != len(self.features):
            raise ValueValidationError("feature schema feature order contains duplicate names")
        if self.created_at.tzinfo is None:
            raise ValueValidationError("feature schema creation time must be timezone-aware")

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.features)

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "vertical": self.vertical,
            "research_family": self.research_family,
            "version": self.version,
            "features": tuple(asdict(item) for item in self.features),
            "created_at": self.created_at,
            "superseded_at": self.superseded_at,
        }

    @property
    def canonical_hash(self) -> str:
        return content_digest(self.canonical_payload)


@dataclass(frozen=True, slots=True)
class StrategyLabLabelDefinition:
    label_id: str
    vertical: str
    research_family: str
    version: str
    target_type: str
    positive_semantics: str
    negative_semantics: str
    resolution_rule: str
    feature_cutoff_rule: str
    availability_rule: str
    created_at: datetime
    superseded_at: datetime | None = None

    def __post_init__(self) -> None:
        required = (
            self.label_id,
            self.vertical,
            self.research_family,
            self.version,
            self.positive_semantics,
            self.negative_semantics,
            self.resolution_rule,
            self.feature_cutoff_rule,
            self.availability_rule,
        )
        if not all(required) or self.target_type != "BINARY_PROBABILITY":
            raise ValueValidationError("label definition is invalid")
        if self.created_at.tzinfo is None:
            raise ValueValidationError("label definition creation time must be timezone-aware")

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "label_id": self.label_id,
            "vertical": self.vertical,
            "research_family": self.research_family,
            "version": self.version,
            "target_type": self.target_type,
            "positive_semantics": self.positive_semantics,
            "negative_semantics": self.negative_semantics,
            "resolution_rule": self.resolution_rule,
            "feature_cutoff_rule": self.feature_cutoff_rule,
            "availability_rule": self.availability_rule,
            "created_at": self.created_at,
            "superseded_at": self.superseded_at,
        }

    @property
    def canonical_hash(self) -> str:
        return content_digest(self.canonical_payload)


@dataclass(frozen=True, slots=True)
class StrategyLabRow:
    row_id: str
    entity_id: str
    grouping_id: str
    feature_at: datetime
    label_available_at: datetime
    feature_values: tuple[Decimal | None, ...]
    label: int
    baseline_probability: Decimal | None
    entry_price: Decimal | None
    fee_cost: Decimal | None
    source_lineage_hash: str
    source_configuration_hash: str
    integrity_state: str = "VALID"
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.row_id or not self.entity_id or not self.grouping_id:
            raise ValueValidationError("Strategy Lab row identity is required")
        if self.feature_at.tzinfo is None or self.label_available_at.tzinfo is None:
            raise ValueValidationError("Strategy Lab row timestamps must be timezone-aware")
        if self.feature_at.astimezone(UTC) >= self.label_available_at.astimezone(UTC):
            raise ValueValidationError("feature timestamp must precede label availability")
        if self.label not in (0, 1):
            raise ValueValidationError("binary Strategy Lab label must be zero or one")
        if len(self.source_lineage_hash) != 64 or len(self.source_configuration_hash) != 64:
            raise ValueValidationError("Strategy Lab row lineage hashes are invalid")
        for value in (self.baseline_probability, self.entry_price, self.fee_cost):
            if value is not None:
                parse_decimal(value, allow_negative=False)
        if self.baseline_probability is not None and self.baseline_probability > 1:
            raise ValueValidationError("baseline probability must be at most one")

    def artifact_payload(self, feature_names: Sequence[str]) -> dict[str, object]:
        if len(feature_names) != len(self.feature_values):
            raise StrategyLabError("row feature count does not match the immutable schema")
        return {
            "baseline_probability": self.baseline_probability,
            "entity_id": self.entity_id,
            "entry_price": self.entry_price,
            "feature_at": self.feature_at,
            "feature_values": dict(zip(feature_names, self.feature_values, strict=True)),
            "fee_cost": self.fee_cost,
            "grouping_id": self.grouping_id,
            "integrity_state": self.integrity_state,
            "label": self.label,
            "label_available_at": self.label_available_at,
            "reason_codes": self.reason_codes,
            "row_id": self.row_id,
            "source_configuration_hash": self.source_configuration_hash,
            "source_lineage_hash": self.source_lineage_hash,
        }


@dataclass(frozen=True, slots=True)
class StrategyLabDatasetSnapshot:
    dataset_id: str
    name: str
    schema: StrategyLabFeatureSchema
    label_definition: StrategyLabLabelDefinition
    adapter_id: str
    adapter_version: str
    cutoff_at: datetime
    source_identity: str
    artifact: StrategyLabArtifact | None
    row_count: int
    entity_count: int
    grouping_count: int
    feature_first_at: datetime | None
    feature_last_at: datetime | None
    label_first_at: datetime | None
    label_last_at: datetime | None
    state: DatasetState
    integrity_warnings: tuple[str, ...]
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str | None
    created_at: datetime
    completed_at: datetime | None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyLabTrainingRun:
    training_run_id: str
    dataset_id: str
    dataset_hash: str
    model_family: ModelFamily
    model_specification_version: str
    hyperparameters: dict[str, object]
    calibration: CalibrationMethod
    seed: int
    python_version: str
    sklearn_version: str
    numpy_version: str
    runtime_identity: str
    state: TrainingRunState
    model_artifact: StrategyLabArtifact | None
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str | None
    started_at: datetime
    completed_at: datetime | None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyLabFold:
    ordinal: int
    training_group_ids: tuple[str, ...]
    validation_group_ids: tuple[str, ...]
    test_group_ids: tuple[str, ...]
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class StrategyLabWalkForwardEvaluation:
    evaluation_id: str
    training_run_id: str
    folds: tuple[StrategyLabFold, ...]
    requested_fold_count: int
    completed_fold_count: int
    prediction_count: int
    distinct_group_count: int
    brier_score: Decimal | None
    log_loss: Decimal | None
    baseline_brier_score: Decimal | None
    baseline_log_loss: Decimal | None
    brier_difference: Decimal | None
    log_loss_difference: Decimal | None
    calibration_summary: dict[str, object]
    recent_fold_metrics: dict[str, object]
    full_fold_metrics: dict[str, object]
    integrity_warnings: tuple[str, ...]
    skipped_fold_reasons: tuple[str, ...]
    state: EvaluationState
    canonical_input_hash: str
    canonical_result_hash: str | None
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class StrategyLabOutOfFoldPrediction:
    """One non-mutating, out-of-fold probability used for research evidence."""

    row_id: str
    grouping_id: str
    entity_id: str
    probability: Decimal


@dataclass(frozen=True, slots=True)
class StrategyLabModelCandidate:
    candidate_id: str
    training_run_id: str
    evaluation_id: str
    model_family: ModelFamily
    feature_schema_hash: str
    label_definition_hash: str
    dataset_hash: str
    model_artifact_hash: str
    state: CandidateState
    rejection_reasons: tuple[str, ...]
    canonical_candidate_hash: str
    created_at: datetime
    eligible_at: datetime | None = None
    reviewed_at: datetime | None = None


class StrategyLabFeatureAdapter(Protocol):
    adapter_id: str
    adapter_version: str
    schema: StrategyLabFeatureSchema
    label_definition: StrategyLabLabelDefinition

    def rows(
        self, *, cutoff_at: datetime, source_scope: str | None = None
    ) -> tuple[StrategyLabRow, ...]: ...

    def source_identity(self, *, cutoff_at: datetime, source_scope: str | None = None) -> str: ...


def canonical_dataset_jsonl(
    rows: Iterable[StrategyLabRow], *, feature_names: Sequence[str]
) -> bytes:
    ordered = sorted(
        rows,
        key=lambda row: (
            row.feature_at.astimezone(UTC),
            row.grouping_id,
            row.entity_id,
            row.row_id,
        ),
    )
    return b"".join(
        canonical_json_bytes(row.artifact_payload(feature_names)) + b"\n" for row in ordered
    )


def validate_dataset_rows(
    rows: Sequence[StrategyLabRow], *, schema: StrategyLabFeatureSchema, cutoff_at: datetime
) -> tuple[str, ...]:
    if cutoff_at.tzinfo is None:
        raise StrategyLabError("dataset cutoff must be timezone-aware")
    reasons: set[str] = set()
    seen_rows: set[str] = set()
    seen_entities: set[tuple[str, datetime]] = set()
    labels: dict[tuple[str, datetime], int] = {}
    configurations: set[str] = set()
    for row in rows:
        if row.row_id in seen_rows:
            reasons.add("DUPLICATE_ROW_ID")
        seen_rows.add(row.row_id)
        entity_key = (row.entity_id, row.feature_at.astimezone(UTC))
        if entity_key in seen_entities:
            reasons.add("DUPLICATE_ENTITY_FEATURE_TIMESTAMP")
        seen_entities.add(entity_key)
        if row.feature_at.astimezone(UTC) > cutoff_at.astimezone(UTC):
            reasons.add("FEATURE_AFTER_CUTOFF")
        if row.feature_at.astimezone(UTC) >= row.label_available_at.astimezone(UTC):
            reasons.add("LABEL_LOOKAHEAD")
        if len(row.feature_values) != len(schema.features):
            reasons.add("FEATURE_SCHEMA_MISMATCH")
        if not row.grouping_id:
            reasons.add("MISSING_GROUPING_IDENTITY")
        label_key = (row.entity_id, row.label_available_at.astimezone(UTC))
        prior_label = labels.get(label_key)
        if prior_label is not None and prior_label != row.label:
            reasons.add("CONTRADICTORY_LABEL")
        labels[label_key] = row.label
        configurations.add(row.source_configuration_hash)
        if row.integrity_state != "VALID":
            reasons.add("SOURCE_INTEGRITY_INVALID")
    if len(configurations) > 1:
        reasons.add("MIXED_SOURCE_CONFIGURATION")
    return tuple(sorted(reasons))


@dataclass(frozen=True, slots=True)
class GroupedWalkForwardPolicy:
    fold_count: int = 3
    minimum_training_rows: int = 24
    minimum_validation_rows: int = 12
    minimum_test_rows: int = 12
    minimum_groups_per_partition: int = 4
    policy_id: str = "GROUPED_WALK_FORWARD_V1"

    def __post_init__(self) -> None:
        if not 3 <= self.fold_count <= 8:
            raise ValueValidationError("walk-forward fold count must be between three and eight")

    def build_folds(self, rows: Sequence[StrategyLabRow]) -> tuple[StrategyLabFold, ...]:
        groups: dict[str, list[StrategyLabRow]] = {}
        for row in rows:
            groups.setdefault(row.grouping_id, []).append(row)
        ordered_groups = tuple(
            key
            for key, _ in sorted(
                groups.items(),
                key=lambda item: (
                    min(row.feature_at.astimezone(UTC) for row in item[1]),
                    item[0],
                ),
            )
        )
        chunk = len(ordered_groups) // (self.fold_count + 2)
        if chunk < self.minimum_groups_per_partition:
            return ()
        folds: list[StrategyLabFold] = []
        for ordinal in range(self.fold_count):
            train = ordered_groups[: (ordinal + 1) * chunk]
            validation = ordered_groups[(ordinal + 1) * chunk : (ordinal + 2) * chunk]
            test = ordered_groups[(ordinal + 2) * chunk : (ordinal + 3) * chunk]
            counts = tuple(
                sum(len(groups[group]) for group in partition)
                for partition in (train, validation, test)
            )
            if (
                len(train) < self.minimum_groups_per_partition
                or len(validation) < self.minimum_groups_per_partition
                or len(test) < self.minimum_groups_per_partition
                or counts[0] < self.minimum_training_rows
                or counts[1] < self.minimum_validation_rows
                or counts[2] < self.minimum_test_rows
            ):
                continue
            payload = {
                "policy": self.policy_id,
                "ordinal": ordinal,
                "train": train,
                "validation": validation,
                "test": test,
            }
            folds.append(
                StrategyLabFold(
                    ordinal=ordinal,
                    training_group_ids=train,
                    validation_group_ids=validation,
                    test_group_ids=test,
                    canonical_hash=content_digest(payload),
                )
            )
        return tuple(folds)


class WalkForwardPolicy(Protocol):
    @property
    def fold_count(self) -> int: ...

    def build_folds(self, rows: Sequence[StrategyLabRow]) -> tuple[StrategyLabFold, ...]: ...


def bounded_hyperparameters(family: ModelFamily, values: dict[str, object]) -> dict[str, object]:
    allowed: dict[ModelFamily, dict[str, tuple[float, float] | tuple[str, ...]]] = {
        ModelFamily.LOGISTIC_REGRESSION: {
            "C": (0.01, 10.0),
            "penalty": ("l1", "l2"),
            "class_weight": ("NONE", "BALANCED"),
            "max_iter": (100, 2_000),
        },
        ModelFamily.HISTOGRAM_GRADIENT_BOOSTING: {
            "max_iter": (1, 300),
            "learning_rate": (0.01, 0.5),
            "max_leaf_nodes": (2, 64),
            "max_depth": (1, 16),
            "max_bins": (2, 255),
            "min_samples_leaf": (2, 128),
            "l2_regularization": (0.0, 10.0),
        },
    }
    unknown = set(values) - set(allowed[family])
    if unknown:
        raise StrategyLabError("model hyperparameters are not supported")
    normalized: dict[str, object] = {}
    for name, constraint in allowed[family].items():
        if name not in values:
            continue
        value = values[name]
        if isinstance(constraint[0], str):
            if not isinstance(value, str) or value not in constraint:
                raise StrategyLabError("model categorical hyperparameter is invalid")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise StrategyLabError("model numeric hyperparameter is invalid")
            try:
                numeric_value = Decimal(str(value))
            except Exception as error:
                raise StrategyLabError("model numeric hyperparameter is invalid") from error
            if not numeric_value.is_finite() or not Decimal(
                str(constraint[0])
            ) <= numeric_value <= Decimal(str(constraint[1])):
                raise StrategyLabError("model hyperparameter is outside its bounded range")
            if name in {
                "max_iter",
                "max_leaf_nodes",
                "max_depth",
                "max_bins",
                "min_samples_leaf",
            }:
                if numeric_value != numeric_value.to_integral_value():
                    raise StrategyLabError("model integer hyperparameter is invalid")
                normalized[name] = str(int(numeric_value))
            else:
                normalized[name] = format(numeric_value.normalize(), "f")
            continue
        normalized[name] = value
    return normalized


def canonical_hyperparameters(values: dict[str, object]) -> dict[str, object]:
    """Keep persisted model inputs free of binary float representation."""

    return {
        name: str(value) if isinstance(value, float) else value
        for name, value in sorted(values.items())
    }


@dataclass(slots=True)
class _FittedModel:
    model: Any
    calibration: CalibrationMethod
    calibrator: Any | None
    training_warnings: tuple[str, ...]

    def probabilities(self, matrix: np.ndarray) -> np.ndarray:
        values = self.model.predict_proba(matrix)[:, 1]
        if self.calibrator is None:
            return cast(np.ndarray, values)
        if self.calibration is CalibrationMethod.PLATT:
            return cast(np.ndarray, self.calibrator.predict_proba(values.reshape(-1, 1))[:, 1])
        return cast(np.ndarray, self.calibrator.predict(values))


def _matrix(rows: Sequence[StrategyLabRow]) -> np.ndarray:
    values: list[list[float]] = []
    for row in rows:
        if any(value is None for value in row.feature_values):
            raise StrategyLabError("NULL_FEATURE_VALUES_REQUIRE_EXPLICIT_ADAPTER_IMPUTATION")
        values.append([float(value) for value in row.feature_values if value is not None])
    return np.asarray(values, dtype=np.float64)


def _parameter_float(values: dict[str, object], name: str, default: float) -> float:
    return float(str(values.get(name, default)))


def _parameter_int(values: dict[str, object], name: str, default: int) -> int:
    return int(str(values.get(name, default)))


def _fit(
    *,
    family: ModelFamily,
    hyperparameters: dict[str, object],
    seed: int,
    calibration: CalibrationMethod,
    training: Sequence[StrategyLabRow],
    validation: Sequence[StrategyLabRow],
) -> _FittedModel:
    train_y = np.asarray([row.label for row in training], dtype=np.int64)
    if len(set(train_y.tolist())) != 2:
        raise StrategyLabError("training partition must contain both binary label classes")
    if family is ModelFamily.LOGISTIC_REGRESSION:
        model = LogisticRegression(
            C=_parameter_float(hyperparameters, "C", 1.0),
            penalty=str(hyperparameters.get("penalty", "l2")),
            class_weight=(
                "balanced" if hyperparameters.get("class_weight") == "BALANCED" else None
            ),
            max_iter=_parameter_int(hyperparameters, "max_iter", 500),
            solver=("liblinear" if hyperparameters.get("penalty") == "l1" else "lbfgs"),
            random_state=seed,
        )
    else:
        model = HistGradientBoostingClassifier(
            max_iter=_parameter_int(hyperparameters, "max_iter", 100),
            learning_rate=_parameter_float(hyperparameters, "learning_rate", 0.1),
            max_leaf_nodes=_parameter_int(hyperparameters, "max_leaf_nodes", 15),
            max_depth=_parameter_int(hyperparameters, "max_depth", 4),
            max_bins=_parameter_int(hyperparameters, "max_bins", 255),
            min_samples_leaf=_parameter_int(hyperparameters, "min_samples_leaf", 10),
            l2_regularization=_parameter_float(hyperparameters, "l2_regularization", 0.0),
            random_state=seed,
        )
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        model.fit(_matrix(training), train_y)
    training_warnings = tuple(
        f"{warning.category.__name__}:{warning.message}" for warning in captured
    )
    if calibration is CalibrationMethod.NONE:
        return _FittedModel(
            model=model,
            calibration=calibration,
            calibrator=None,
            training_warnings=training_warnings,
        )
    validation_y = np.asarray([row.label for row in validation], dtype=np.int64)
    if len(set(validation_y.tolist())) != 2:
        raise StrategyLabError("calibration partition must contain both binary label classes")
    raw = model.predict_proba(_matrix(validation))[:, 1]
    with warnings.catch_warnings(record=True) as calibration_warnings:
        warnings.simplefilter("always")
        if calibration is CalibrationMethod.PLATT:
            calibrator = LogisticRegression(C=1.0, max_iter=500, random_state=seed)
            calibrator.fit(raw.reshape(-1, 1), validation_y)
        else:
            if len(validation) < 24:
                raise StrategyLabError("isotonic calibration requires at least 24 validation rows")
            calibrator = IsotonicRegression(out_of_bounds="clip")
            calibrator.fit(raw, validation_y)
    training_warnings += tuple(
        f"{warning.category.__name__}:{warning.message}" for warning in calibration_warnings
    )
    return _FittedModel(
        model=model,
        calibration=calibration,
        calibrator=calibrator,
        training_warnings=training_warnings,
    )


def _decimal_metric(values: Iterable[float]) -> Decimal:
    values_tuple = tuple(values)
    if not values_tuple:
        raise StrategyLabError("metric requires at least one prediction")
    with localcontext() as context:
        context.prec = 28
        mean = sum((Decimal(str(value)) for value in values_tuple), Decimal()) / Decimal(
            len(values_tuple)
        )
        return parse_decimal(format(mean.quantize(Decimal("0.00000001")), "f"))


def _log_loss(label: int, probability: float) -> float:
    bounded = min(max(probability, 1e-12), 1 - 1e-12)
    with localcontext() as context:
        context.prec = 28
        value = Decimal(str(bounded))
        return float(
            -(Decimal(label) * value.ln() + Decimal(1 - label) * (Decimal(1) - value).ln())
        )


def _calibration_summary(
    labels: Sequence[int], probabilities: Sequence[float]
) -> dict[str, object]:
    buckets: list[dict[str, object]] = []
    for ordinal in range(5):
        lower = ordinal / 5
        upper = (ordinal + 1) / 5
        selected = [
            index
            for index, value in enumerate(probabilities)
            if lower <= value < upper or (ordinal == 4 and value == upper)
        ]
        if selected:
            buckets.append(
                {
                    "bucket": ordinal,
                    "count": len(selected),
                    "mean_prediction": str(
                        sum(probabilities[index] for index in selected) / len(selected)
                    ),
                    "mean_label": str(sum(labels[index] for index in selected) / len(selected)),
                }
            )
    return {"bucket_count": len(buckets), "buckets": tuple(buckets)}


def evaluate_grouped_walk_forward(
    *,
    rows: Sequence[StrategyLabRow],
    family: ModelFamily,
    hyperparameters: dict[str, object],
    seed: int,
    calibration: CalibrationMethod,
    policy: WalkForwardPolicy,
) -> StrategyLabWalkForwardEvaluation:
    hyperparameters = canonical_hyperparameters(bounded_hyperparameters(family, hyperparameters))
    folds = policy.build_folds(rows)
    grouped = {
        group: tuple(row for row in rows if row.grouping_id == group)
        for group in {row.grouping_id for row in rows}
    }
    predictions: list[float] = []
    baselines: list[float] = []
    labels: list[int] = []
    skipped: list[str] = []
    complete: list[StrategyLabFold] = []
    for fold in folds:
        training = tuple(row for group in fold.training_group_ids for row in grouped[group])
        validation = tuple(row for group in fold.validation_group_ids for row in grouped[group])
        test = tuple(row for group in fold.test_group_ids for row in grouped[group])
        try:
            model = _fit(
                family=family,
                hyperparameters=hyperparameters,
                seed=seed + fold.ordinal,
                calibration=calibration,
                training=training,
                validation=validation,
            )
        except StrategyLabError as error:
            skipped.append(f"FOLD_{fold.ordinal}_{error}")
            continue
        values = model.probabilities(_matrix(test))
        if not np.isfinite(values).all() or np.any(values < 0) or np.any(values > 1):
            skipped.append(f"FOLD_{fold.ordinal}_INVALID_PREDICTIONS")
            continue
        predictions.extend(float(item) for item in values)
        baselines.extend(
            float(row.baseline_probability) if row.baseline_probability is not None else 0.5
            for row in test
        )
        labels.extend(row.label for row in test)
        complete.append(fold)
    now = datetime.now(tz=UTC)
    payload = {
        "family": family.value,
        "hyperparameters": hyperparameters,
        "calibration": calibration.value,
        "folds": tuple(item.canonical_hash for item in complete),
        "skipped": tuple(skipped),
    }
    if len(complete) < 3:
        return StrategyLabWalkForwardEvaluation(
            evaluation_id=deterministic_id("strategy_lab_evaluation", payload),
            training_run_id="",
            folds=tuple(complete),
            requested_fold_count=policy.fold_count,
            completed_fold_count=len(complete),
            prediction_count=len(labels),
            distinct_group_count=len({row.grouping_id for row in rows}),
            brier_score=None,
            log_loss=None,
            baseline_brier_score=None,
            baseline_log_loss=None,
            brier_difference=None,
            log_loss_difference=None,
            calibration_summary={},
            recent_fold_metrics={},
            full_fold_metrics={},
            integrity_warnings=(),
            skipped_fold_reasons=tuple(skipped),
            state=EvaluationState.INSUFFICIENT_EVIDENCE,
            canonical_input_hash=content_digest(payload),
            canonical_result_hash=None,
            created_at=now,
            completed_at=now,
        )
    brier = _decimal_metric(
        (prediction - label) ** 2 for prediction, label in zip(predictions, labels, strict=True)
    )
    log_loss = _decimal_metric(
        _log_loss(label, prediction) for prediction, label in zip(predictions, labels, strict=True)
    )
    baseline_brier = _decimal_metric(
        (prediction - label) ** 2 for prediction, label in zip(baselines, labels, strict=True)
    )
    baseline_log_loss = _decimal_metric(
        _log_loss(label, prediction) for prediction, label in zip(baselines, labels, strict=True)
    )
    calibration_summary = _calibration_summary(labels, predictions)
    result = {
        "brier": brier,
        "log_loss": log_loss,
        "baseline_brier": baseline_brier,
        "baseline_log_loss": baseline_log_loss,
        "calibration": calibration_summary,
    }
    return StrategyLabWalkForwardEvaluation(
        evaluation_id=deterministic_id("strategy_lab_evaluation", payload),
        training_run_id="",
        folds=tuple(complete),
        requested_fold_count=policy.fold_count,
        completed_fold_count=len(complete),
        prediction_count=len(labels),
        distinct_group_count=len({row.grouping_id for row in rows}),
        brier_score=brier,
        log_loss=log_loss,
        baseline_brier_score=baseline_brier,
        baseline_log_loss=baseline_log_loss,
        brier_difference=brier - baseline_brier,
        log_loss_difference=log_loss - baseline_log_loss,
        calibration_summary=calibration_summary,
        recent_fold_metrics={"fold_hash": complete[-1].canonical_hash},
        full_fold_metrics=result,
        integrity_warnings=(),
        skipped_fold_reasons=tuple(skipped),
        state=EvaluationState.COMPLETED,
        canonical_input_hash=content_digest(payload),
        canonical_result_hash=content_digest(result),
        created_at=now,
        completed_at=now,
    )


def out_of_fold_predictions(
    *,
    rows: Sequence[StrategyLabRow],
    family: ModelFamily,
    hyperparameters: dict[str, object],
    seed: int,
    calibration: CalibrationMethod,
    policy: WalkForwardPolicy,
) -> tuple[StrategyLabOutOfFoldPrediction, ...]:
    """Reproduce only valid walk-forward test predictions without fitting on them."""

    normalized = canonical_hyperparameters(bounded_hyperparameters(family, hyperparameters))
    grouped = {
        group: tuple(row for row in rows if row.grouping_id == group)
        for group in {row.grouping_id for row in rows}
    }
    result: list[StrategyLabOutOfFoldPrediction] = []
    for fold in policy.build_folds(rows):
        training = tuple(row for group in fold.training_group_ids for row in grouped[group])
        validation = tuple(row for group in fold.validation_group_ids for row in grouped[group])
        test = tuple(row for group in fold.test_group_ids for row in grouped[group])
        try:
            fitted = _fit(
                family=family,
                hyperparameters=normalized,
                seed=seed + fold.ordinal,
                calibration=calibration,
                training=training,
                validation=validation,
            )
        except StrategyLabError:
            continue
        probabilities = fitted.probabilities(_matrix(test))
        if (
            not np.isfinite(probabilities).all()
            or np.any(probabilities < 0)
            or np.any(probabilities > 1)
        ):
            continue
        result.extend(
            StrategyLabOutOfFoldPrediction(
                row_id=row.row_id,
                grouping_id=row.grouping_id,
                entity_id=row.entity_id,
                probability=parse_decimal(format(float(probability), ".8f")),
            )
            for row, probability in zip(test, probabilities, strict=True)
        )
    return tuple(result)


def candidate_rejection_reasons(evaluation: StrategyLabWalkForwardEvaluation) -> tuple[str, ...]:
    if evaluation.state is not EvaluationState.COMPLETED:
        return ("INSUFFICIENT_VALID_FOLDS",)
    assert evaluation.brier_difference is not None and evaluation.log_loss_difference is not None
    reasons: list[str] = []
    if evaluation.brier_difference >= 0 and evaluation.log_loss_difference >= 0:
        reasons.append("WORSE_THAN_BASELINE_ON_BRIER_AND_LOG_LOSS")
    bucket_count = evaluation.calibration_summary.get("bucket_count", 0)
    if not isinstance(bucket_count, int) or bucket_count < 3:
        reasons.append("INSUFFICIENT_CALIBRATION_COVERAGE")
    if evaluation.distinct_group_count < 12:
        reasons.append("INSUFFICIENT_INDEPENDENT_GROUPS")
    return tuple(reasons)


def runtime_identity() -> dict[str, str]:
    return {
        "python_version": sys.version.split()[0],
        "sklearn_version": sklearn.__version__,
        "numpy_version": np.__version__,
        "runtime_identity": f"{platform.system()}-{platform.release()}-{platform.machine()}",
    }


def serialize_logistic_model(
    *, model: LogisticRegression, feature_names: Sequence[str], calibration: CalibrationMethod
) -> bytes:
    return canonical_json_bytes(
        {
            "artifact_type": "APEX_LOGISTIC_REGRESSION_V1",
            "feature_names": tuple(feature_names),
            "coefficients": tuple(str(value) for value in model.coef_[0]),
            "intercept": str(model.intercept_[0]),
            "classes": tuple(int(value) for value in model.classes_),
            "calibration": calibration.value,
            "runtime": runtime_identity(),
        }
    )


def build_dataset_artifact(
    *,
    schema: StrategyLabFeatureSchema,
    rows: Sequence[StrategyLabRow],
    store: StrategyLabArtifactStore,
) -> StrategyLabArtifact:
    payload = canonical_dataset_jsonl(rows, feature_names=schema.feature_names)
    artifact_id = content_digest(
        {"schema": schema.canonical_hash, "payload": payload.decode("utf-8")}
    )
    return store.write_bytes(f"datasets/{artifact_id}.jsonl", payload)


def load_dataset_artifact(
    *,
    artifact: StrategyLabArtifact,
    store: StrategyLabArtifactStore,
    schema: StrategyLabFeatureSchema,
) -> tuple[StrategyLabRow, ...]:
    try:
        lines = store.read_bytes(artifact).decode("utf-8").splitlines()
        values = [json.loads(line) for line in lines]
    except (UnicodeDecodeError, json.JSONDecodeError, StrategyLabArtifactError) as error:
        raise StrategyLabError("dataset artifact is not reproducible") from error
    rows: list[StrategyLabRow] = []
    for value in values:
        features = value["feature_values"]
        rows.append(
            StrategyLabRow(
                row_id=value["row_id"],
                entity_id=value["entity_id"],
                grouping_id=value["grouping_id"],
                feature_at=datetime.fromisoformat(value["feature_at"]),
                label_available_at=datetime.fromisoformat(value["label_available_at"]),
                feature_values=tuple(
                    parse_decimal(features[name], allow_negative=True)
                    if features[name] is not None
                    else None
                    for name in schema.feature_names
                ),
                label=int(value["label"]),
                baseline_probability=parse_decimal(value["baseline_probability"])
                if value["baseline_probability"] is not None
                else None,
                entry_price=parse_decimal(value["entry_price"])
                if value["entry_price"] is not None
                else None,
                fee_cost=parse_decimal(value["fee_cost"])
                if value["fee_cost"] is not None
                else None,
                source_lineage_hash=value["source_lineage_hash"],
                source_configuration_hash=value["source_configuration_hash"],
                integrity_state=value["integrity_state"],
                reason_codes=tuple(value["reason_codes"]),
            )
        )
    return tuple(rows)


class StrategyLabRepository(Protocol):
    """Durable research metadata port with no financial repositories."""

    def feature_schema(self, schema_id: str) -> StrategyLabFeatureSchema | None: ...

    def save_feature_schema(self, schema: StrategyLabFeatureSchema) -> StrategyLabFeatureSchema: ...

    def label_definition(self, label_id: str) -> StrategyLabLabelDefinition | None: ...

    def save_label_definition(
        self, label: StrategyLabLabelDefinition
    ) -> StrategyLabLabelDefinition: ...

    def dataset_by_idempotency(
        self, adapter_id: str, key: str
    ) -> StrategyLabDatasetSnapshot | None: ...

    def save_dataset(self, dataset: StrategyLabDatasetSnapshot) -> StrategyLabDatasetSnapshot: ...

    def dataset(self, dataset_id: str) -> StrategyLabDatasetSnapshot | None: ...

    def training_by_idempotency(
        self, dataset_id: str, key: str
    ) -> StrategyLabTrainingRun | None: ...

    def save_training(self, training: StrategyLabTrainingRun) -> StrategyLabTrainingRun: ...

    def training(self, training_run_id: str) -> StrategyLabTrainingRun | None: ...

    def save_evaluation(
        self, evaluation: StrategyLabWalkForwardEvaluation
    ) -> StrategyLabWalkForwardEvaluation: ...

    def evaluation(self, evaluation_id: str) -> StrategyLabWalkForwardEvaluation | None: ...

    def save_candidate(self, candidate: StrategyLabModelCandidate) -> StrategyLabModelCandidate: ...

    def candidate(self, candidate_id: str) -> StrategyLabModelCandidate | None: ...


class InMemoryStrategyLabRepository:
    """Deterministic repository used only by tests and the provider-free demo."""

    def __init__(self) -> None:
        self._schemas: dict[str, StrategyLabFeatureSchema] = {}
        self._labels: dict[str, StrategyLabLabelDefinition] = {}
        self._datasets: dict[str, StrategyLabDatasetSnapshot] = {}
        self._trainings: dict[str, StrategyLabTrainingRun] = {}
        self._evaluations: dict[str, StrategyLabWalkForwardEvaluation] = {}
        self._candidates: dict[str, StrategyLabModelCandidate] = {}

    def feature_schema(self, schema_id: str) -> StrategyLabFeatureSchema | None:
        return self._schemas.get(schema_id)

    def save_feature_schema(self, schema: StrategyLabFeatureSchema) -> StrategyLabFeatureSchema:
        prior = self._schemas.get(schema.schema_id)
        if prior is not None and prior.canonical_hash != schema.canonical_hash:
            raise StrategyLabError("FEATURE_SCHEMA_IMMUTABLE")
        self._schemas[schema.schema_id] = schema
        return schema

    def label_definition(self, label_id: str) -> StrategyLabLabelDefinition | None:
        return self._labels.get(label_id)

    def save_label_definition(
        self, label: StrategyLabLabelDefinition
    ) -> StrategyLabLabelDefinition:
        prior = self._labels.get(label.label_id)
        if prior is not None and prior.canonical_hash != label.canonical_hash:
            raise StrategyLabError("LABEL_DEFINITION_IMMUTABLE")
        self._labels[label.label_id] = label
        return label

    def dataset_by_idempotency(
        self, adapter_id: str, key: str
    ) -> StrategyLabDatasetSnapshot | None:
        return next(
            (
                dataset
                for dataset in self._datasets.values()
                if dataset.adapter_id == adapter_id and dataset.idempotency_key == key
            ),
            None,
        )

    def save_dataset(self, dataset: StrategyLabDatasetSnapshot) -> StrategyLabDatasetSnapshot:
        prior = self._datasets.get(dataset.dataset_id)
        if prior is not None and prior.canonical_input_hash != dataset.canonical_input_hash:
            raise StrategyLabError("DATASET_IDEMPOTENCY_CONFLICT")
        self._datasets[dataset.dataset_id] = dataset
        return dataset

    def dataset(self, dataset_id: str) -> StrategyLabDatasetSnapshot | None:
        return self._datasets.get(dataset_id)

    def training_by_idempotency(self, dataset_id: str, key: str) -> StrategyLabTrainingRun | None:
        return next(
            (
                training
                for training in self._trainings.values()
                if training.dataset_id == dataset_id and training.idempotency_key == key
            ),
            None,
        )

    def save_training(self, training: StrategyLabTrainingRun) -> StrategyLabTrainingRun:
        self._trainings[training.training_run_id] = training
        return training

    def training(self, training_run_id: str) -> StrategyLabTrainingRun | None:
        return self._trainings.get(training_run_id)

    def save_evaluation(
        self, evaluation: StrategyLabWalkForwardEvaluation
    ) -> StrategyLabWalkForwardEvaluation:
        self._evaluations[evaluation.evaluation_id] = evaluation
        return evaluation

    def evaluation(self, evaluation_id: str) -> StrategyLabWalkForwardEvaluation | None:
        return self._evaluations.get(evaluation_id)

    def save_candidate(self, candidate: StrategyLabModelCandidate) -> StrategyLabModelCandidate:
        self._candidates[candidate.candidate_id] = candidate
        return candidate

    def candidate(self, candidate_id: str) -> StrategyLabModelCandidate | None:
        return self._candidates.get(candidate_id)


def _model_artifact_payload(
    *, fitted: _FittedModel, family: ModelFamily, feature_names: Sequence[str]
) -> tuple[str, bytes]:
    metadata = {
        "artifact_type": "APEX_STRATEGY_LAB_MODEL_V1",
        "model_family": family.value,
        "feature_names": tuple(feature_names),
        "calibration": fitted.calibration.value,
        "runtime": runtime_identity(),
        "training_warnings": fitted.training_warnings,
    }
    if family is ModelFamily.LOGISTIC_REGRESSION:
        model = fitted.model
        calibration_payload: dict[str, object] | None = None
        if fitted.calibrator is not None and fitted.calibration is CalibrationMethod.PLATT:
            calibration_payload = {
                "type": "PLATT",
                "coefficients": tuple(str(item) for item in fitted.calibrator.coef_[0]),
                "intercept": str(fitted.calibrator.intercept_[0]),
            }
        return "json", canonical_json_bytes(
            {
                **metadata,
                "coefficients": tuple(str(value) for value in model.coef_[0]),
                "intercept": str(model.intercept_[0]),
                "classes": tuple(int(value) for value in model.classes_),
                "calibration_parameters": calibration_payload,
            }
        )
    buffer = io.BytesIO()
    joblib_dump(
        {"metadata": metadata, "model": fitted.model, "calibrator": fitted.calibrator}, buffer
    )
    return "joblib", buffer.getvalue()


class StrategyLabService:
    """Bounded research workflow; deliberately incapable of granting authority."""

    def __init__(
        self, repository: StrategyLabRepository, artifacts: StrategyLabArtifactStore
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts

    def register_feature_schema(self, schema: StrategyLabFeatureSchema) -> StrategyLabFeatureSchema:
        return self._repository.save_feature_schema(schema)

    def register_label_definition(
        self, label: StrategyLabLabelDefinition
    ) -> StrategyLabLabelDefinition:
        return self._repository.save_label_definition(label)

    def build_dataset(
        self,
        *,
        adapter: StrategyLabFeatureAdapter,
        name: str,
        cutoff_at: datetime,
        idempotency_key: str,
        source_scope: str | None = None,
    ) -> StrategyLabDatasetSnapshot:
        if not name or not idempotency_key or cutoff_at.tzinfo is None:
            raise StrategyLabError("DATASET_BUILD_INPUT_INVALID")
        input_payload = {
            "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.adapter_version,
            "name": name,
            "cutoff_at": cutoff_at,
            "scope": source_scope,
            "source_identity": adapter.source_identity(
                cutoff_at=cutoff_at, source_scope=source_scope
            ),
            "schema_hash": adapter.schema.canonical_hash,
            "label_hash": adapter.label_definition.canonical_hash,
            "key": idempotency_key,
        }
        prior = self._repository.dataset_by_idempotency(adapter.adapter_id, idempotency_key)
        input_hash = content_digest(input_payload)
        if prior is not None:
            if prior.canonical_input_hash != input_hash:
                raise StrategyLabError("DATASET_IDEMPOTENCY_CONFLICT")
            return prior
        self.register_feature_schema(adapter.schema)
        self.register_label_definition(adapter.label_definition)
        now = datetime.now(tz=UTC)
        building = StrategyLabDatasetSnapshot(
            dataset_id=deterministic_id("strategy_lab_dataset", input_payload),
            name=name,
            schema=adapter.schema,
            label_definition=adapter.label_definition,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            cutoff_at=cutoff_at,
            source_identity=adapter.source_identity(cutoff_at=cutoff_at, source_scope=source_scope),
            artifact=None,
            row_count=0,
            entity_count=0,
            grouping_count=0,
            feature_first_at=None,
            feature_last_at=None,
            label_first_at=None,
            label_last_at=None,
            state=DatasetState.BUILDING,
            integrity_warnings=(),
            idempotency_key=idempotency_key,
            canonical_input_hash=input_hash,
            canonical_result_hash=None,
            created_at=now,
            completed_at=None,
        )
        building = self._repository.save_dataset(building)
        try:
            rows = adapter.rows(cutoff_at=cutoff_at, source_scope=source_scope)
            reasons = validate_dataset_rows(rows, schema=adapter.schema, cutoff_at=cutoff_at)
            if reasons:
                raise StrategyLabError("DATASET_INTEGRITY_" + reasons[0])
            if not rows:
                raise StrategyLabError("DATASET_EMPTY")
            artifact = build_dataset_artifact(
                schema=adapter.schema, rows=rows, store=self._artifacts
            )
            ordered = tuple(
                sorted(
                    rows,
                    key=lambda row: (row.feature_at, row.grouping_id, row.entity_id, row.row_id),
                )
            )
            result_payload = {
                "artifact_hash": artifact.sha256,
                "rows": len(ordered),
                "groups": len({row.grouping_id for row in ordered}),
            }
            return self._repository.save_dataset(
                replace(
                    building,
                    artifact=artifact,
                    row_count=len(ordered),
                    entity_count=len({row.entity_id for row in ordered}),
                    grouping_count=len({row.grouping_id for row in ordered}),
                    feature_first_at=ordered[0].feature_at,
                    feature_last_at=ordered[-1].feature_at,
                    label_first_at=min(row.label_available_at for row in ordered),
                    label_last_at=max(row.label_available_at for row in ordered),
                    state=DatasetState.READY,
                    canonical_result_hash=content_digest(result_payload),
                    completed_at=datetime.now(tz=UTC),
                )
            )
        except StrategyLabError as error:
            self._repository.save_dataset(
                replace(
                    building,
                    state=DatasetState.INVALID,
                    failure_code=str(error),
                    completed_at=datetime.now(tz=UTC),
                )
            )
            raise
        except Exception as error:
            self._repository.save_dataset(
                replace(
                    building,
                    state=DatasetState.INVALID,
                    failure_code="DATASET_BUILD_FAILED",
                    completed_at=datetime.now(tz=UTC),
                )
            )
            raise StrategyLabError("DATASET_BUILD_FAILED") from error

    def train_model(
        self,
        *,
        dataset_id: str,
        family: ModelFamily,
        hyperparameters: dict[str, object],
        calibration: CalibrationMethod,
        seed: int,
        idempotency_key: str,
        policy: WalkForwardPolicy | None = None,
    ) -> StrategyLabTrainingRun:
        if seed < 0 or not idempotency_key:
            raise StrategyLabError("TRAINING_INPUT_INVALID")
        dataset = self._repository.dataset(dataset_id)
        if dataset is None or dataset.state is not DatasetState.READY:
            raise StrategyLabError("DATASET_NOT_READY")
        if dataset.canonical_result_hash is None:
            raise StrategyLabError("DATASET_RESULT_HASH_MISSING")
        hyperparameters = canonical_hyperparameters(
            bounded_hyperparameters(family, hyperparameters)
        )
        input_payload = {
            "dataset_hash": dataset.canonical_result_hash,
            "family": family.value,
            "hyperparameters": hyperparameters,
            "calibration": calibration.value,
            "seed": seed,
            "key": idempotency_key,
        }
        prior = self._repository.training_by_idempotency(dataset_id, idempotency_key)
        input_hash = content_digest(input_payload)
        if prior is not None:
            if prior.canonical_input_hash != input_hash:
                raise StrategyLabError("TRAINING_IDEMPOTENCY_CONFLICT")
            return prior
        now = datetime.now(tz=UTC)
        identity = runtime_identity()
        requested = StrategyLabTrainingRun(
            training_run_id=deterministic_id("strategy_lab_training", input_payload),
            dataset_id=dataset.dataset_id,
            dataset_hash=dataset.canonical_result_hash,
            model_family=family,
            model_specification_version="V1",
            hyperparameters=hyperparameters,
            calibration=calibration,
            seed=seed,
            python_version=identity["python_version"],
            sklearn_version=identity["sklearn_version"],
            numpy_version=identity["numpy_version"],
            runtime_identity=identity["runtime_identity"],
            state=TrainingRunState.REQUESTED,
            model_artifact=None,
            idempotency_key=idempotency_key,
            canonical_input_hash=input_hash,
            canonical_result_hash=None,
            started_at=now,
            completed_at=None,
        )
        run = self._repository.save_training(requested)
        run = self._repository.save_training(replace(run, state=TrainingRunState.RUNNING))
        try:
            if dataset.artifact is None:
                raise StrategyLabError("DATASET_ARTIFACT_MISSING")
            rows = load_dataset_artifact(
                artifact=dataset.artifact, store=self._artifacts, schema=dataset.schema
            )
            folds = (policy or GroupedWalkForwardPolicy()).build_folds(rows)
            if not folds:
                raise StrategyLabError("TRAINING_INSUFFICIENT_GROUPED_FOLDS")
            last_fold = folds[-1]
            training_rows = tuple(
                row for row in rows if row.grouping_id in last_fold.training_group_ids
            )
            validation_rows = tuple(
                row for row in rows if row.grouping_id in last_fold.validation_group_ids
            )
            fitted = _fit(
                family=family,
                hyperparameters=hyperparameters,
                seed=seed,
                calibration=calibration,
                training=training_rows,
                validation=validation_rows,
            )
            suffix, payload = _model_artifact_payload(
                fitted=fitted, family=family, feature_names=dataset.schema.feature_names
            )
            artifact_id = content_digest({"input": input_hash, "artifact": payload.hex()})
            artifact = self._artifacts.write_bytes(f"models/{artifact_id}.{suffix}", payload)
            result_hash = content_digest({"artifact": artifact.sha256, "runtime": identity})
            return self._repository.save_training(
                replace(
                    run,
                    state=TrainingRunState.COMPLETED,
                    model_artifact=artifact,
                    canonical_result_hash=result_hash,
                    completed_at=datetime.now(tz=UTC),
                )
            )
        except StrategyLabError as error:
            self._repository.save_training(
                replace(
                    run,
                    state=TrainingRunState.FAILED,
                    failure_code=str(error),
                    completed_at=datetime.now(tz=UTC),
                )
            )
            raise
        except Exception as error:
            self._repository.save_training(
                replace(
                    run,
                    state=TrainingRunState.FAILED,
                    failure_code="TRAINING_FAILED",
                    completed_at=datetime.now(tz=UTC),
                )
            )
            raise StrategyLabError("TRAINING_FAILED") from error

    def evaluate_candidate(
        self,
        *,
        training_run_id: str,
        policy: WalkForwardPolicy | None = None,
        research_integrity_evaluation: ResearchMultipleTestingEvaluation | None = None,
    ) -> tuple[StrategyLabWalkForwardEvaluation, StrategyLabModelCandidate]:
        run = self._repository.training(training_run_id)
        if run is None or run.state is not TrainingRunState.COMPLETED or run.model_artifact is None:
            raise StrategyLabError("TRAINING_RUN_NOT_COMPLETED")
        dataset = self._repository.dataset(run.dataset_id)
        if dataset is None:
            raise StrategyLabError("TRAINING_DATASET_MISSING")
        if dataset.canonical_result_hash is None:
            raise StrategyLabError("DATASET_RESULT_HASH_MISSING")
        if not self._artifacts.exists(run.model_artifact):
            raise StrategyLabError("MODEL_ARTIFACT_HASH_MISMATCH")
        if dataset.artifact is None:
            raise StrategyLabError("DATASET_ARTIFACT_MISSING")
        rows = load_dataset_artifact(
            artifact=dataset.artifact, store=self._artifacts, schema=dataset.schema
        )
        selected_policy: WalkForwardPolicy = policy or GroupedWalkForwardPolicy()
        evaluation = evaluate_grouped_walk_forward(
            rows=rows,
            family=run.model_family,
            hyperparameters=run.hyperparameters,
            seed=run.seed,
            calibration=run.calibration,
            policy=selected_policy,
        )
        evaluation = replace(evaluation, training_run_id=run.training_run_id)
        self._repository.save_evaluation(evaluation)
        reasons = candidate_rejection_reasons(evaluation)
        reasons = (
            *reasons,
            *research_integrity_candidate_rejection_reasons(research_integrity_evaluation),
        )
        state = CandidateState.REJECTED if reasons else CandidateState.ELIGIBLE_FOR_SHADOW_REVIEW
        now = datetime.now(tz=UTC)
        candidate_payload = {
            "training": run.training_run_id,
            "evaluation": evaluation.evaluation_id,
            "artifact": run.model_artifact.sha256,
            "state": state.value,
            "reasons": reasons,
        }
        candidate = StrategyLabModelCandidate(
            candidate_id=deterministic_id("strategy_lab_candidate", candidate_payload),
            training_run_id=run.training_run_id,
            evaluation_id=evaluation.evaluation_id,
            model_family=run.model_family,
            feature_schema_hash=dataset.schema.canonical_hash,
            label_definition_hash=dataset.label_definition.canonical_hash,
            dataset_hash=dataset.canonical_result_hash,
            model_artifact_hash=run.model_artifact.sha256,
            state=state,
            rejection_reasons=reasons,
            canonical_candidate_hash=content_digest(candidate_payload),
            created_at=now,
            eligible_at=now if state is CandidateState.ELIGIBLE_FOR_SHADOW_REVIEW else None,
        )
        return evaluation, self._repository.save_candidate(candidate)

    def candidate_report(self, candidate_id: str) -> dict[str, object]:
        candidate = self._repository.candidate(candidate_id)
        if candidate is None:
            raise StrategyLabError("CANDIDATE_NOT_FOUND")
        evaluation = self._repository.evaluation(candidate.evaluation_id)
        return {
            "warning": "RESEARCH CANDIDATE — NO STRATEGY OR CAPITAL AUTHORITY",
            "candidate": candidate,
            "evaluation": evaluation,
        }
