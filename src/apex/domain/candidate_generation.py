"""Pure bounded-search rules for automated Strategy Lab research."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import reduce
from operator import mul

from apex.domain.values import content_digest, parse_decimal

OBJECTIVE_POLICY_ID = "STRATEGY_LAB_BOUNDED_OBJECTIVE_V1"
SEARCH_PLAN_POLICY_ID = "BOUNDED_CANDIDATE_SEARCH_PLAN_V1"
SELECTION_POLICY_ID = "APEX_INTEGRITY_ELIGIBLE_SELECTION_V1"
MLFLOW_MIRROR_POLICY_ID = "LOCAL_MLFLOW_RESEARCH_MIRROR_V1"


class CandidateGenerationError(ValueError):
    """A pure candidate-generation invariant rejected input."""


class ParameterKind(StrEnum):
    CATEGORICAL = "CATEGORICAL"
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"


class ObjectiveIdentity(StrEnum):
    BRIER = "BRIER"
    LOG_LOSS = "LOG_LOSS"
    COST_ADJUSTED_EXCESS_RETURN = "COST_ADJUSTED_EXCESS_RETURN"


class ObjectiveDirection(StrEnum):
    MINIMIZE = "MINIMIZE"
    MAXIMIZE = "MAXIMIZE"


@dataclass(frozen=True, slots=True)
class BoundedParameter:
    name: str
    kind: ParameterKind
    choices: tuple[str, ...] = ()
    lower: Decimal | None = None
    upper: Decimal | None = None
    step: Decimal | None = None
    logarithmic: bool = False

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 120:
            raise CandidateGenerationError("PARAMETER_NAME_INVALID")
        if self.kind is ParameterKind.CATEGORICAL:
            if (
                len(self.choices) < 1
                or len(set(self.choices)) != len(self.choices)
                or any(not value or len(value) > 160 for value in self.choices)
                or any(value is not None for value in (self.lower, self.upper, self.step))
                or self.logarithmic
            ):
                raise CandidateGenerationError("CATEGORICAL_PARAMETER_INVALID")
            return
        if self.choices or self.lower is None or self.upper is None:
            raise CandidateGenerationError("NUMERIC_PARAMETER_BOUNDS_REQUIRED")
        if not self.lower.is_finite() or not self.upper.is_finite():
            raise CandidateGenerationError("NUMERIC_PARAMETER_NONFINITE")
        if self.lower > self.upper:
            raise CandidateGenerationError("NUMERIC_PARAMETER_RANGE_INVALID")
        if self.logarithmic:
            if self.lower <= 0 or self.step is not None:
                raise CandidateGenerationError("LOG_PARAMETER_INVALID")
        elif self.step is None or not self.step.is_finite() or self.step <= 0:
            raise CandidateGenerationError("NUMERIC_PARAMETER_STEP_REQUIRED")
        if self.kind is ParameterKind.INTEGER:
            values = (self.lower, self.upper, self.step)
            if any(value != value.to_integral_value() for value in values if value is not None):
                raise CandidateGenerationError("INTEGER_PARAMETER_INVALID")

    @property
    def cardinality(self) -> int:
        if self.kind is ParameterKind.CATEGORICAL:
            return len(self.choices)
        if self.logarithmic:
            # A log range is finite only because Optuna receives this explicit,
            # closed interval and the plan separately caps total proposals.
            return 1
        assert self.lower is not None and self.upper is not None and self.step is not None
        return int(((self.upper - self.lower) / self.step).to_integral_value()) + 1

    @property
    def canonical_hash(self) -> str:
        return content_digest(self.payload)

    @property
    def payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "choices": self.choices,
            "lower": str(self.lower) if self.lower is not None else None,
            "upper": str(self.upper) if self.upper is not None else None,
            "step": str(self.step) if self.step is not None else None,
            "logarithmic": self.logarithmic,
        }

    def normalize(self, value: object) -> str:
        if self.kind is ParameterKind.CATEGORICAL:
            if not isinstance(value, str) or value not in self.choices:
                raise CandidateGenerationError(f"PROPOSAL_OUTSIDE_SPACE:{self.name}")
            return value
        if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
            raise CandidateGenerationError(f"PROPOSAL_NUMERIC_INVALID:{self.name}")
        decimal = parse_decimal(value)
        assert self.lower is not None and self.upper is not None
        if decimal < self.lower or decimal > self.upper:
            raise CandidateGenerationError(f"PROPOSAL_OUTSIDE_SPACE:{self.name}")
        if self.step is not None and (decimal - self.lower) % self.step:
            raise CandidateGenerationError(f"PROPOSAL_OFF_STEP:{self.name}")
        if self.kind is ParameterKind.INTEGER and decimal != decimal.to_integral_value():
            raise CandidateGenerationError(f"PROPOSAL_NOT_INTEGER:{self.name}")
        return str(int(decimal)) if self.kind is ParameterKind.INTEGER else format(decimal, "f")


@dataclass(frozen=True, slots=True)
class BoundedSearchSpace:
    parameters: tuple[BoundedParameter, ...]

    def __post_init__(self) -> None:
        if not self.parameters or len(self.parameters) > 32:
            raise CandidateGenerationError("SEARCH_SPACE_SIZE_INVALID")
        names = tuple(item.name for item in self.parameters)
        if len(set(names)) != len(names):
            raise CandidateGenerationError("SEARCH_PARAMETER_DUPLICATE")

    @property
    def theoretical_combinations(self) -> int:
        return reduce(mul, (item.cardinality for item in self.parameters), 1)

    @property
    def canonical_hash(self) -> str:
        return content_digest(tuple(item.payload for item in self.parameters))

    @property
    def payload(self) -> tuple[dict[str, object], ...]:
        return tuple(item.payload for item in self.parameters)

    def validate_proposal(self, proposal: dict[str, object]) -> dict[str, str]:
        if set(proposal) != {item.name for item in self.parameters}:
            raise CandidateGenerationError("PROPOSAL_PARAMETER_SET_MISMATCH")
        return {
            parameter.name: parameter.normalize(proposal[parameter.name])
            for parameter in self.parameters
        }


def initial_strategy_lab_search_space() -> BoundedSearchSpace:
    """Finite managed space for the two already implemented model families."""

    return BoundedSearchSpace(
        (
            BoundedParameter(
                "model_family",
                ParameterKind.CATEGORICAL,
                choices=(
                    "logistic-regression-probability-v1",
                    "histogram-gradient-boosting-probability-v1",
                ),
            ),
            BoundedParameter(
                "calibration",
                ParameterKind.CATEGORICAL,
                choices=("NONE", "PLATT", "ISOTONIC"),
            ),
            BoundedParameter(
                "regularization_c",
                ParameterKind.DECIMAL,
                lower=Decimal("0.1"),
                upper=Decimal("4.0"),
                step=Decimal("0.1"),
            ),
            BoundedParameter(
                "penalty",
                ParameterKind.CATEGORICAL,
                choices=("l2",),
            ),
            BoundedParameter(
                "class_weight",
                ParameterKind.CATEGORICAL,
                choices=("NONE", "BALANCED"),
            ),
            BoundedParameter(
                "logistic_max_iter",
                ParameterKind.INTEGER,
                lower=Decimal(200),
                upper=Decimal(800),
                step=Decimal(100),
            ),
            BoundedParameter(
                "learning_rate",
                ParameterKind.DECIMAL,
                lower=Decimal("0.05"),
                upper=Decimal("0.25"),
                step=Decimal("0.05"),
            ),
            BoundedParameter(
                "histogram_max_iter",
                ParameterKind.INTEGER,
                lower=Decimal(40),
                upper=Decimal(160),
                step=Decimal(20),
            ),
            BoundedParameter(
                "max_leaf_nodes",
                ParameterKind.INTEGER,
                lower=Decimal(7),
                upper=Decimal(31),
                step=Decimal(4),
            ),
            BoundedParameter(
                "max_bins",
                ParameterKind.INTEGER,
                lower=Decimal(64),
                upper=Decimal(255),
                step=Decimal(191),
            ),
            BoundedParameter(
                "min_samples_leaf",
                ParameterKind.INTEGER,
                lower=Decimal(5),
                upper=Decimal(30),
                step=Decimal(5),
            ),
            BoundedParameter(
                "l2_regularization",
                ParameterKind.DECIMAL,
                lower=Decimal("0"),
                upper=Decimal("2"),
                step=Decimal("0.25"),
            ),
        )
    )


def objective_direction(identity: ObjectiveIdentity) -> ObjectiveDirection:
    if identity in (ObjectiveIdentity.BRIER, ObjectiveIdentity.LOG_LOSS):
        return ObjectiveDirection.MINIMIZE
    return ObjectiveDirection.MAXIMIZE
