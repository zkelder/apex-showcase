"""Bounded generic crypto-campaign candidate definitions and deterministic execution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from apex.domain.crypto_research import CostScenarioName, CryptoDailyCandle
from apex.domain.research_evidence import EvidenceClass, ReproducibilityStatus
from apex.domain.values import content_digest, parse_decimal

GENERIC_CANDIDATE_DEFINITION_V1 = "GENERIC_CRYPTO_CANDIDATE_DEFINITION_V1"
GENERIC_CANDIDATE_COMPILER_V1 = "GENERIC_CRYPTO_CANDIDATE_COMPILER_V1"
GENERIC_CANDIDATE_EXECUTOR_V1 = "GENERIC_CRYPTO_CANDIDATE_EXECUTOR_V1"
GENERIC_CANDIDATE_PLAN_V1 = "GENERIC_CRYPTO_CANDIDATE_PLAN_V1"
GENERIC_CANDIDATE_RUN_V1 = "GENERIC_CRYPTO_CANDIDATE_RUN_V1"
GENERIC_POLICY_REVIEW_V1 = "GENERIC_CRYPTO_POLICY_REVIEW_V1"

MAX_FEATURES = 8
MAX_RULES = 2
MAX_LOOKBACK = 366
MAX_TEXT_LENGTH = 2_000
METRIC_QUANTUM = Decimal("0.00000001")
BPS = Decimal("10000")

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class GenericCandidateError(ValueError):
    """A bounded generic candidate invariant was violated."""


class CandidateDefinitionSchema(StrEnum):
    V1 = GENERIC_CANDIDATE_DEFINITION_V1


class FeatureKind(StrEnum):
    ROLLING_MEAN = "ROLLING_MEAN"
    ROLLING_MINIMUM = "ROLLING_MINIMUM"
    ROLLING_MAXIMUM = "ROLLING_MAXIMUM"
    POINT_TO_POINT_RETURN = "POINT_TO_POINT_RETURN"


class ComparisonOperator(StrEnum):
    STRICTLY_GREATER_THAN = "STRICTLY_GREATER_THAN"
    STRICTLY_LESS_THAN = "STRICTLY_LESS_THAN"
    GREATER_THAN_OR_EQUAL = "GREATER_THAN_OR_EQUAL"
    LESS_THAN_OR_EQUAL = "LESS_THAN_OR_EQUAL"


class DecisionCadence(StrEnum):
    DAILY = "DAILY"
    CALENDAR_MONTHLY = "CALENDAR_MONTHLY"


class PositionState(StrEnum):
    CASH = "CASH"
    LONG = "LONG"


class RuleMode(StrEnum):
    STATELESS_LONG_CASH = "STATELESS_LONG_CASH"
    STATEFUL_ENTRY_EXIT = "STATEFUL_ENTRY_EXIT"


class OperandKind(StrEnum):
    DECISION_CLOSE = "DECISION_CLOSE"
    FEATURE = "FEATURE"
    CONSTANT = "CONSTANT"


class MissingDataAction(StrEnum):
    SELECT_CASH = "SELECT_CASH"
    PRESERVE_CASH = "PRESERVE_CASH"
    PROTECTIVE_EXIT_TO_CASH = "PROTECTIVE_EXIT_TO_CASH"
    MARK_RUN_INCOMPLETE = "MARK_RUN_INCOMPLETE"


class ExecutionTiming(StrEnum):
    NEXT_ELIGIBLE_DAILY_OPEN = "NEXT_ELIGIBLE_DAILY_OPEN"


class PositionSizing(StrEnum):
    FULL_LONG_OR_CASH = "FULL_LONG_OR_CASH"


class GenericPortfolio(StrEnum):
    CANDIDATE = "CANDIDATE"
    CASH = "CASH"
    BUY_AND_HOLD = "BUY_AND_HOLD"


class GenericOutcomeDecision(StrEnum):
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    PROMOTION_CANDIDATE = "PROMOTION_CANDIDATE"


def _identity(value: str, reason: str) -> str:
    if _IDENTITY.fullmatch(value) is None:
        raise GenericCandidateError(reason)
    return value


def _text(value: str, reason: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_TEXT_LENGTH:
        raise GenericCandidateError(reason)
    return normalized


def _aware(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GenericCandidateError(reason)
    return value.astimezone(UTC)


def _hash(value: str, reason: str) -> str:
    if _HASH.fullmatch(value) is None:
        raise GenericCandidateError(reason)
    return value


def _metric(value: Decimal) -> Decimal:
    return value.quantize(METRIC_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class GenericOperand:
    kind: OperandKind
    feature_name: str | None
    constant: Decimal | None

    @classmethod
    def decision_close(cls) -> GenericOperand:
        return cls(OperandKind.DECISION_CLOSE, None, None)

    @classmethod
    def feature(cls, name: str) -> GenericOperand:
        return cls(OperandKind.FEATURE, _identity(name, "GENERIC_FEATURE_REFERENCE_INVALID"), None)

    @classmethod
    def constant_value(cls, value: object) -> GenericOperand:
        try:
            parsed = parse_decimal(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise GenericCandidateError("GENERIC_RULE_CONSTANT_INVALID") from error
        return cls(OperandKind.CONSTANT, None, parsed)

    def __post_init__(self) -> None:
        if self.kind is OperandKind.FEATURE:
            if self.feature_name is None or self.constant is not None:
                raise GenericCandidateError("GENERIC_FEATURE_OPERAND_INVALID")
        elif self.kind is OperandKind.CONSTANT:
            if self.constant is None or self.feature_name is not None:
                raise GenericCandidateError("GENERIC_CONSTANT_OPERAND_INVALID")
        elif self.feature_name is not None or self.constant is not None:
            raise GenericCandidateError("GENERIC_DECISION_CLOSE_OPERAND_INVALID")

    @property
    def canonical_hash(self) -> str:
        return content_digest((self.kind, self.feature_name, self.constant))


@dataclass(frozen=True, slots=True)
class GenericFeatureDefinition:
    name: str
    kind: FeatureKind
    input_field: str
    observations: int
    exclude_decision_observation: bool
    missing_data_action: MissingDataAction
    precision: int
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        name: str,
        kind: FeatureKind,
        observations: int,
        exclude_decision_observation: bool,
        missing_data_action: MissingDataAction,
        input_field: str = "close",
        precision: int = 8,
    ) -> GenericFeatureDefinition:
        feature_name = _identity(name, "GENERIC_FEATURE_ID_INVALID")
        if input_field != "close":
            raise GenericCandidateError("GENERIC_FEATURE_INPUT_UNSUPPORTED")
        if isinstance(observations, bool) or not 1 <= observations <= MAX_LOOKBACK:
            raise GenericCandidateError("GENERIC_FEATURE_LOOKBACK_UNBOUNDED")
        if kind is FeatureKind.POINT_TO_POINT_RETURN and observations < 2:
            raise GenericCandidateError("GENERIC_RETURN_LOOKBACK_INVALID")
        if precision != 8:
            raise GenericCandidateError("GENERIC_FEATURE_PRECISION_UNSUPPORTED")
        payload = (
            feature_name,
            kind,
            input_field,
            observations,
            exclude_decision_observation,
            missing_data_action,
            precision,
        )
        return cls(*payload, content_digest(payload))


@dataclass(frozen=True, slots=True)
class GenericRuleDefinition:
    rule_id: str
    source_state: PositionState | None
    left: GenericOperand
    operator: ComparisonOperator
    right: GenericOperand
    trigger_state: PositionState
    non_trigger_state: PositionState
    missing_data_action: MissingDataAction
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        rule_id: str,
        source_state: PositionState | None,
        left: GenericOperand,
        operator: ComparisonOperator,
        right: GenericOperand,
        trigger_state: PositionState,
        non_trigger_state: PositionState,
        missing_data_action: MissingDataAction,
    ) -> GenericRuleDefinition:
        identity = _identity(rule_id, "GENERIC_RULE_ID_INVALID")
        if left == right:
            raise GenericCandidateError("GENERIC_RULE_OPERANDS_AMBIGUOUS")
        payload = (
            identity,
            source_state,
            left,
            operator,
            right,
            trigger_state,
            non_trigger_state,
            missing_data_action,
        )
        return cls(*payload, content_digest(payload))


@dataclass(frozen=True, slots=True)
class GenericCandidateDefinition:
    schema_version: CandidateDefinitionSchema
    definition_id: str
    campaign_id: str
    candidate_id: str
    hypothesis_id: str
    hypothesis_version_id: str
    strategy_specification_id: str
    strategy_version: int
    family: str
    parent_strategy_ids: tuple[str, ...]
    expected_mechanism: str
    product_id: str
    allowed_evidence_classes: tuple[EvidenceClass, ...]
    approved_snapshot_ids: tuple[str, str]
    validation_windows: tuple[tuple[datetime, datetime], tuple[datetime, datetime]]
    cadence: DecisionCadence
    initial_state: PositionState
    rule_mode: RuleMode
    features: tuple[GenericFeatureDefinition, ...]
    rules: tuple[GenericRuleDefinition, ...]
    execution_timing: ExecutionTiming
    position_sizing: PositionSizing
    fee_bps_per_side: Decimal
    slippage_bps_per_side: Decimal
    hypothetical_notional: Decimal
    baselines: tuple[GenericPortfolio, ...]
    prior_strategy_ids: tuple[str, ...]
    per_window_policy: tuple[str, ...]
    aggregate_nomination_policy: tuple[str, ...]
    authority_restrictions: tuple[str, ...]
    semantics_hash: str
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        definition_id: str,
        campaign_id: str,
        candidate_id: str,
        hypothesis_id: str,
        hypothesis_version_id: str,
        strategy_specification_id: str,
        strategy_version: int,
        family: str,
        expected_mechanism: str,
        approved_snapshot_ids: tuple[str, str],
        validation_windows: tuple[tuple[datetime, datetime], tuple[datetime, datetime]],
        cadence: DecisionCadence,
        initial_state: PositionState,
        rule_mode: RuleMode,
        features: tuple[GenericFeatureDefinition, ...],
        rules: tuple[GenericRuleDefinition, ...],
        parent_strategy_ids: tuple[str, ...] = (),
        product_id: str = "BTC-USD",
        allowed_evidence_classes: tuple[EvidenceClass, ...] = (
            EvidenceClass.HISTORICAL_PUBLIC,
            EvidenceClass.FIXTURE,
        ),
        execution_timing: ExecutionTiming = ExecutionTiming.NEXT_ELIGIBLE_DAILY_OPEN,
        position_sizing: PositionSizing = PositionSizing.FULL_LONG_OR_CASH,
        fee_bps_per_side: object = "60",
        slippage_bps_per_side: object = "10",
        hypothetical_notional: object = "10000",
        baselines: tuple[GenericPortfolio, ...] = (
            GenericPortfolio.CASH,
            GenericPortfolio.BUY_AND_HOLD,
        ),
        prior_strategy_ids: tuple[str, ...] = (),
        per_window_policy: tuple[str, ...] = (
            "CONSERVATIVE_NET_RETURN_AT_LEAST_ZERO",
            "DRAWDOWN_STRICTLY_BELOW_BUY_AND_HOLD",
            "EVIDENCE_COMPLETE",
            "REPRODUCIBLE",
            "NO_DISQUALIFYING_WARNING",
            "AT_LEAST_ONE_LONG_EXPOSURE_PERIOD",
        ),
        aggregate_nomination_policy: tuple[str, ...] = (
            "EVERY_WINDOW_PASSES",
            "COMPOUNDED_CONSERVATIVE_RETURN_POSITIVE",
        ),
        authority_restrictions: tuple[str, ...] = (
            "RESEARCH_ONLY",
            "NO_PAPER",
            "NO_LIVE",
            "NO_CAPITAL",
            "NO_FINANCIAL_MUTATION",
        ),
        schema_version: CandidateDefinitionSchema = CandidateDefinitionSchema.V1,
    ) -> GenericCandidateDefinition:
        identities = (
            _identity(definition_id, "GENERIC_DEFINITION_ID_INVALID"),
            _identity(campaign_id, "GENERIC_CAMPAIGN_ID_INVALID"),
            _identity(candidate_id, "GENERIC_CANDIDATE_ID_INVALID"),
            _identity(hypothesis_id, "GENERIC_HYPOTHESIS_ID_INVALID"),
            _identity(hypothesis_version_id, "GENERIC_HYPOTHESIS_VERSION_INVALID"),
            _identity(strategy_specification_id, "GENERIC_STRATEGY_ID_INVALID"),
        )
        if schema_version is not CandidateDefinitionSchema.V1:
            raise GenericCandidateError("GENERIC_DEFINITION_SCHEMA_UNSUPPORTED")
        if isinstance(strategy_version, bool) or strategy_version < 1:
            raise GenericCandidateError("GENERIC_STRATEGY_VERSION_INVALID")
        if product_id != "BTC-USD":
            raise GenericCandidateError("GENERIC_PRODUCT_UNAUTHORIZED")
        if not allowed_evidence_classes or any(
            item not in {EvidenceClass.FIXTURE, EvidenceClass.HISTORICAL_PUBLIC}
            for item in allowed_evidence_classes
        ):
            raise GenericCandidateError("GENERIC_EVIDENCE_CLASS_UNAPPROVED")
        if len(set(approved_snapshot_ids)) != 2:
            raise GenericCandidateError("GENERIC_APPROVED_SNAPSHOTS_INVALID")
        snapshot_values = tuple(
            _identity(item, "GENERIC_SNAPSHOT_ID_INVALID") for item in approved_snapshot_ids
        )
        snapshots = (snapshot_values[0], snapshot_values[1])
        window_values = tuple(
            (
                _aware(start, "GENERIC_VALIDATION_TIME_INVALID"),
                _aware(end, "GENERIC_VALIDATION_TIME_INVALID"),
            )
            for start, end in validation_windows
        )
        if len(window_values) != 2 or any(start >= end for start, end in window_values):
            raise GenericCandidateError("GENERIC_VALIDATION_WINDOWS_INVALID")
        windows = (window_values[0], window_values[1])
        if not features or len(features) > MAX_FEATURES:
            raise GenericCandidateError("GENERIC_FEATURE_COUNT_INVALID")
        if len({item.name for item in features}) != len(features):
            raise GenericCandidateError("GENERIC_FEATURE_DUPLICATE")
        if not rules or len(rules) > MAX_RULES:
            raise GenericCandidateError("GENERIC_RULE_COUNT_INVALID")
        if len({item.rule_id for item in rules}) != len(rules):
            raise GenericCandidateError("GENERIC_RULE_DUPLICATE")
        feature_names = {item.name for item in features}
        for rule in rules:
            for operand in (rule.left, rule.right):
                if (
                    operand.kind is OperandKind.FEATURE
                    and operand.feature_name not in feature_names
                ):
                    raise GenericCandidateError("GENERIC_RULE_FEATURE_UNKNOWN")
        if rule_mode is RuleMode.STATELESS_LONG_CASH:
            if (
                len(rules) != 1
                or rules[0].source_state is not None
                or rules[0].trigger_state is not PositionState.LONG
                or rules[0].non_trigger_state is not PositionState.CASH
                or rules[0].missing_data_action is not MissingDataAction.SELECT_CASH
            ):
                raise GenericCandidateError("GENERIC_STATELESS_RULE_INVALID")
        else:
            by_state = {item.source_state: item for item in rules}
            if set(by_state) != {PositionState.CASH, PositionState.LONG}:
                raise GenericCandidateError("GENERIC_STATEFUL_TRANSITIONS_INVALID")
            entry = by_state[PositionState.CASH]
            exit_rule = by_state[PositionState.LONG]
            if (
                entry.trigger_state is not PositionState.LONG
                or entry.non_trigger_state is not PositionState.CASH
                or entry.missing_data_action is not MissingDataAction.PRESERVE_CASH
                or exit_rule.trigger_state is not PositionState.CASH
                or exit_rule.non_trigger_state is not PositionState.LONG
                or exit_rule.missing_data_action is not MissingDataAction.PROTECTIVE_EXIT_TO_CASH
            ):
                raise GenericCandidateError("GENERIC_STATEFUL_TRANSITIONS_INVALID")
        if initial_state is not PositionState.CASH:
            raise GenericCandidateError("GENERIC_INITIAL_STATE_UNSUPPORTED")
        if execution_timing is not ExecutionTiming.NEXT_ELIGIBLE_DAILY_OPEN:
            raise GenericCandidateError("GENERIC_EXECUTION_TIMING_UNSUPPORTED")
        if position_sizing is not PositionSizing.FULL_LONG_OR_CASH:
            raise GenericCandidateError("GENERIC_POSITION_SIZING_UNSUPPORTED")
        try:
            fee = parse_decimal(fee_bps_per_side)  # type: ignore[arg-type]
            slippage = parse_decimal(slippage_bps_per_side)  # type: ignore[arg-type]
            notional = parse_decimal(hypothetical_notional, allow_zero=False)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise GenericCandidateError("GENERIC_COST_OR_NOTIONAL_INVALID") from error
        if fee != Decimal("60") or slippage != Decimal("10") or notional != Decimal("10000"):
            raise GenericCandidateError("GENERIC_CAMPAIGN_ECONOMICS_UNAPPROVED")
        if set(baselines) != {GenericPortfolio.CASH, GenericPortfolio.BUY_AND_HOLD}:
            raise GenericCandidateError("GENERIC_BASELINES_INVALID")
        parents = tuple(
            _identity(item, "GENERIC_PARENT_STRATEGY_INVALID") for item in parent_strategy_ids
        )
        priors = tuple(
            _identity(item, "GENERIC_PRIOR_STRATEGY_INVALID") for item in prior_strategy_ids
        )
        family_id = _identity(family, "GENERIC_STRATEGY_FAMILY_INVALID")
        mechanism = _text(expected_mechanism, "GENERIC_EXPECTED_MECHANISM_INVALID")
        if not per_window_policy or not aggregate_nomination_policy:
            raise GenericCandidateError("GENERIC_POLICY_REQUIRED")
        if set(authority_restrictions) != {
            "RESEARCH_ONLY",
            "NO_PAPER",
            "NO_LIVE",
            "NO_CAPITAL",
            "NO_FINANCIAL_MUTATION",
        }:
            raise GenericCandidateError("GENERIC_AUTHORITY_FIELD_FORBIDDEN")
        semantics_payload = (
            schema_version,
            product_id,
            tuple(sorted(item.value for item in allowed_evidence_classes)),
            cadence,
            initial_state,
            rule_mode,
            features,
            rules,
            execution_timing,
            position_sizing,
            fee,
            slippage,
            notional,
            baselines,
            per_window_policy,
            aggregate_nomination_policy,
            authority_restrictions,
        )
        semantics_hash = content_digest(semantics_payload)
        canonical_payload = (
            schema_version,
            *identities,
            strategy_version,
            family_id,
            parents,
            mechanism,
            product_id,
            tuple(allowed_evidence_classes),
            snapshots,
            windows,
            cadence,
            initial_state,
            rule_mode,
            features,
            rules,
            execution_timing,
            position_sizing,
            fee,
            slippage,
            notional,
            baselines,
            priors,
            per_window_policy,
            aggregate_nomination_policy,
            authority_restrictions,
            semantics_hash,
            "NONE",
        )
        return cls(
            schema_version,
            *identities,
            strategy_version,
            family_id,
            parents,
            mechanism,
            product_id,
            tuple(allowed_evidence_classes),
            snapshots,
            windows,
            cadence,
            initial_state,
            rule_mode,
            features,
            rules,
            execution_timing,
            position_sizing,
            fee,
            slippage,
            notional,
            baselines,
            priors,
            per_window_policy,
            aggregate_nomination_policy,
            authority_restrictions,
            semantics_hash,
            content_digest(canonical_payload),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> GenericCandidateDefinition:
        allowed = {
            "schema_version",
            "definition_id",
            "campaign_id",
            "candidate_id",
            "hypothesis_id",
            "hypothesis_version_id",
            "strategy_specification_id",
            "strategy_version",
            "family",
            "expected_mechanism",
            "approved_snapshot_ids",
            "validation_windows",
            "cadence",
            "initial_state",
            "rule_mode",
            "features",
            "rules",
            "parent_strategy_ids",
            "product_id",
            "allowed_evidence_classes",
            "execution_timing",
            "position_sizing",
            "fee_bps_per_side",
            "slippage_bps_per_side",
            "hypothetical_notional",
            "baselines",
            "prior_strategy_ids",
            "per_window_policy",
            "aggregate_nomination_policy",
            "authority_restrictions",
        }
        if set(value) != allowed:
            raise GenericCandidateError("GENERIC_DEFINITION_FIELDS_UNSUPPORTED")

        forbidden = {"code", "script", "formula", "plugin", "command", "expression"}

        def reject_executable(item: object) -> None:
            if isinstance(item, Mapping):
                if any(str(key).lower() in forbidden for key in item):
                    raise GenericCandidateError("GENERIC_EXECUTABLE_DEFINITION_FORBIDDEN")
                for nested in item.values():
                    reject_executable(nested)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    reject_executable(nested)

        def sequence(name: str) -> tuple[object, ...]:
            item = value[name]
            if not isinstance(item, (list, tuple)):
                raise GenericCandidateError("GENERIC_DEFINITION_SEQUENCE_INVALID")
            return tuple(item)

        def operand(item: object) -> GenericOperand:
            if not isinstance(item, Mapping) or set(item) != {
                "kind",
                "feature_name",
                "constant",
            }:
                raise GenericCandidateError("GENERIC_OPERAND_FIELDS_INVALID")
            try:
                kind = OperandKind(str(item["kind"]))
            except ValueError as error:
                raise GenericCandidateError("GENERIC_OPERAND_KIND_UNSUPPORTED") from error
            if kind is OperandKind.DECISION_CLOSE:
                return GenericOperand.decision_close()
            if kind is OperandKind.FEATURE:
                if not isinstance(item["feature_name"], str):
                    raise GenericCandidateError("GENERIC_FEATURE_REFERENCE_INVALID")
                return GenericOperand.feature(item["feature_name"])
            return GenericOperand.constant_value(item["constant"])

        reject_executable(value)
        raw_features = sequence("features")
        if len(raw_features) > MAX_FEATURES:
            raise GenericCandidateError("GENERIC_FEATURE_COUNT_INVALID")
        features: list[GenericFeatureDefinition] = []
        for item in raw_features:
            if not isinstance(item, Mapping) or set(item) != {
                "name",
                "kind",
                "input_field",
                "observations",
                "exclude_decision_observation",
                "missing_data_action",
                "precision",
            }:
                raise GenericCandidateError("GENERIC_FEATURE_FIELDS_INVALID")
            if (
                not isinstance(item["name"], str)
                or not isinstance(item["input_field"], str)
                or not isinstance(item["observations"], int)
                or not isinstance(item["exclude_decision_observation"], bool)
                or not isinstance(item["precision"], int)
            ):
                raise GenericCandidateError("GENERIC_FEATURE_TYPES_INVALID")
            try:
                feature_kind = FeatureKind(str(item["kind"]))
                feature_missing = MissingDataAction(str(item["missing_data_action"]))
            except ValueError as error:
                raise GenericCandidateError("GENERIC_FEATURE_ENUM_UNSUPPORTED") from error
            features.append(
                GenericFeatureDefinition.create(
                    name=item["name"],
                    kind=feature_kind,
                    input_field=item["input_field"],
                    observations=item["observations"],
                    exclude_decision_observation=item["exclude_decision_observation"],
                    missing_data_action=feature_missing,
                    precision=item["precision"],
                )
            )
        raw_rules = sequence("rules")
        if len(raw_rules) > MAX_RULES:
            raise GenericCandidateError("GENERIC_RULE_COUNT_INVALID")
        rules: list[GenericRuleDefinition] = []
        for item in raw_rules:
            if not isinstance(item, Mapping) or set(item) != {
                "rule_id",
                "source_state",
                "left",
                "operator",
                "right",
                "trigger_state",
                "non_trigger_state",
                "missing_data_action",
            }:
                raise GenericCandidateError("GENERIC_RULE_FIELDS_INVALID")
            if not isinstance(item["rule_id"], str):
                raise GenericCandidateError("GENERIC_RULE_ID_INVALID")
            try:
                source_state = (
                    None
                    if item["source_state"] is None
                    else PositionState(str(item["source_state"]))
                )
                operator_value = ComparisonOperator(str(item["operator"]))
                trigger_state = PositionState(str(item["trigger_state"]))
                non_trigger_state = PositionState(str(item["non_trigger_state"]))
                missing_action = MissingDataAction(str(item["missing_data_action"]))
            except ValueError as error:
                raise GenericCandidateError("GENERIC_RULE_ENUM_UNSUPPORTED") from error
            rules.append(
                GenericRuleDefinition.create(
                    rule_id=item["rule_id"],
                    source_state=source_state,
                    left=operand(item["left"]),
                    operator=operator_value,
                    right=operand(item["right"]),
                    trigger_state=trigger_state,
                    non_trigger_state=non_trigger_state,
                    missing_data_action=missing_action,
                )
            )
        raw_windows = sequence("validation_windows")
        if len(raw_windows) != 2:
            raise GenericCandidateError("GENERIC_VALIDATION_WINDOWS_INVALID")
        windows: list[tuple[datetime, datetime]] = []
        for item in raw_windows:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise GenericCandidateError("GENERIC_VALIDATION_WINDOWS_INVALID")
            try:
                start = datetime.fromisoformat(str(item[0]).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(item[1]).replace("Z", "+00:00"))
            except ValueError as error:
                raise GenericCandidateError("GENERIC_VALIDATION_TIME_INVALID") from error
            windows.append((start, end))

        string_fields = (
            "definition_id",
            "campaign_id",
            "candidate_id",
            "hypothesis_id",
            "hypothesis_version_id",
            "strategy_specification_id",
            "family",
            "expected_mechanism",
            "product_id",
        )
        if any(not isinstance(value[name], str) for name in string_fields):
            raise GenericCandidateError("GENERIC_DEFINITION_STRING_INVALID")
        if not isinstance(value["strategy_version"], int) or isinstance(
            value["strategy_version"], bool
        ):
            raise GenericCandidateError("GENERIC_STRATEGY_VERSION_INVALID")
        try:
            schema = CandidateDefinitionSchema(str(value["schema_version"]))
            cadence_value = DecisionCadence(str(value["cadence"]))
            initial = PositionState(str(value["initial_state"]))
            mode = RuleMode(str(value["rule_mode"]))
            execution = ExecutionTiming(str(value["execution_timing"]))
            sizing = PositionSizing(str(value["position_sizing"]))
            evidence = tuple(
                EvidenceClass(str(item)) for item in sequence("allowed_evidence_classes")
            )
            baseline_values = tuple(GenericPortfolio(str(item)) for item in sequence("baselines"))
        except ValueError as error:
            raise GenericCandidateError("GENERIC_DEFINITION_ENUM_UNSUPPORTED") from error

        def strings(name: str) -> tuple[str, ...]:
            items = sequence(name)
            if any(not isinstance(item, str) for item in items):
                raise GenericCandidateError("GENERIC_DEFINITION_STRING_SEQUENCE_INVALID")
            return tuple(str(item) for item in items)

        snapshots = strings("approved_snapshot_ids")
        if len(snapshots) != 2:
            raise GenericCandidateError("GENERIC_APPROVED_SNAPSHOTS_INVALID")
        return cls.create(
            schema_version=schema,
            definition_id=str(value["definition_id"]),
            campaign_id=str(value["campaign_id"]),
            candidate_id=str(value["candidate_id"]),
            hypothesis_id=str(value["hypothesis_id"]),
            hypothesis_version_id=str(value["hypothesis_version_id"]),
            strategy_specification_id=str(value["strategy_specification_id"]),
            strategy_version=int(value["strategy_version"]),
            family=str(value["family"]),
            parent_strategy_ids=strings("parent_strategy_ids"),
            expected_mechanism=str(value["expected_mechanism"]),
            product_id=str(value["product_id"]),
            allowed_evidence_classes=evidence,
            approved_snapshot_ids=(snapshots[0], snapshots[1]),
            validation_windows=(windows[0], windows[1]),
            cadence=cadence_value,
            initial_state=initial,
            rule_mode=mode,
            features=tuple(features),
            rules=tuple(rules),
            execution_timing=execution,
            position_sizing=sizing,
            fee_bps_per_side=value["fee_bps_per_side"],
            slippage_bps_per_side=value["slippage_bps_per_side"],
            hypothetical_notional=value["hypothetical_notional"],
            baselines=baseline_values,
            prior_strategy_ids=strings("prior_strategy_ids"),
            per_window_policy=strings("per_window_policy"),
            aggregate_nomination_policy=strings("aggregate_nomination_policy"),
            authority_restrictions=strings("authority_restrictions"),
        )


@dataclass(frozen=True, slots=True)
class CompiledCandidatePlan:
    plan_id: str
    definition_id: str
    definition_schema_version: CandidateDefinitionSchema
    semantics_hash: str
    compiler_id: str
    compiler_version: int
    product_id: str
    cadence: DecisionCadence
    initial_state: PositionState
    rule_mode: RuleMode
    required_features: tuple[GenericFeatureDefinition, ...]
    decision_rules: tuple[GenericRuleDefinition, ...]
    execution_timing: ExecutionTiming
    position_sizing: PositionSizing
    fee_bps_per_side: Decimal
    slippage_bps_per_side: Decimal
    hypothetical_notional: Decimal
    missing_data_policy: tuple[tuple[str, MissingDataAction], ...]
    warnings: tuple[str, ...]
    plan_hash: str
    canonical_hash: str
    authority: str = "NONE"


def compile_candidate_definition(
    definition: GenericCandidateDefinition,
) -> CompiledCandidatePlan:
    if definition.schema_version is not CandidateDefinitionSchema.V1:
        raise GenericCandidateError("GENERIC_DEFINITION_SCHEMA_UNSUPPORTED")
    missing = tuple(
        (item.rule_id, item.missing_data_action)
        for item in sorted(definition.rules, key=lambda value: value.rule_id)
    )
    warnings = (
        "EXPOSED OR FIXTURE DEVELOPMENT EVIDENCE ONLY",
        "HYPOTHETICAL RESEARCH NOTIONAL — NOT ACCOUNT VALUE",
        "SEALED HOLDOUT EXCLUDED",
        "NO PAPER, LIVE, CAPITAL, ORDER, BROKER, OR FINANCIAL AUTHORITY",
    )
    plan_payload = (
        GENERIC_CANDIDATE_PLAN_V1,
        definition.schema_version,
        definition.semantics_hash,
        GENERIC_CANDIDATE_COMPILER_V1,
        1,
        definition.product_id,
        definition.cadence,
        definition.initial_state,
        definition.rule_mode,
        definition.features,
        definition.rules,
        definition.execution_timing,
        definition.position_sizing,
        definition.fee_bps_per_side,
        definition.slippage_bps_per_side,
        definition.hypothetical_notional,
        missing,
        warnings,
        "NONE",
    )
    plan_hash = content_digest(plan_payload)
    plan_id = f"generic-crypto-plan-{definition.semantics_hash[:24]}"
    canonical = content_digest((plan_id, plan_payload, plan_hash))
    return CompiledCandidatePlan(
        plan_id,
        definition.definition_id,
        definition.schema_version,
        definition.semantics_hash,
        GENERIC_CANDIDATE_COMPILER_V1,
        1,
        definition.product_id,
        definition.cadence,
        definition.initial_state,
        definition.rule_mode,
        definition.features,
        definition.rules,
        definition.execution_timing,
        definition.position_sizing,
        definition.fee_bps_per_side,
        definition.slippage_bps_per_side,
        definition.hypothetical_notional,
        missing,
        warnings,
        plan_hash,
        canonical,
    )


def candidate_definition_mapping(
    definition: GenericCandidateDefinition,
) -> dict[str, object]:
    """Return the complete non-executable structured registration contract."""

    def operand(value: GenericOperand) -> dict[str, object]:
        return {
            "kind": value.kind.value,
            "feature_name": value.feature_name,
            "constant": str(value.constant) if value.constant is not None else None,
        }

    return {
        "schema_version": definition.schema_version.value,
        "definition_id": definition.definition_id,
        "campaign_id": definition.campaign_id,
        "candidate_id": definition.candidate_id,
        "hypothesis_id": definition.hypothesis_id,
        "hypothesis_version_id": definition.hypothesis_version_id,
        "strategy_specification_id": definition.strategy_specification_id,
        "strategy_version": definition.strategy_version,
        "family": definition.family,
        "parent_strategy_ids": list(definition.parent_strategy_ids),
        "expected_mechanism": definition.expected_mechanism,
        "product_id": definition.product_id,
        "allowed_evidence_classes": [item.value for item in definition.allowed_evidence_classes],
        "approved_snapshot_ids": list(definition.approved_snapshot_ids),
        "validation_windows": [
            [start.isoformat(), end.isoformat()] for start, end in definition.validation_windows
        ],
        "cadence": definition.cadence.value,
        "initial_state": definition.initial_state.value,
        "rule_mode": definition.rule_mode.value,
        "features": [
            {
                "name": item.name,
                "kind": item.kind.value,
                "input_field": item.input_field,
                "observations": item.observations,
                "exclude_decision_observation": item.exclude_decision_observation,
                "missing_data_action": item.missing_data_action.value,
                "precision": item.precision,
            }
            for item in definition.features
        ],
        "rules": [
            {
                "rule_id": item.rule_id,
                "source_state": (
                    item.source_state.value if item.source_state is not None else None
                ),
                "left": operand(item.left),
                "operator": item.operator.value,
                "right": operand(item.right),
                "trigger_state": item.trigger_state.value,
                "non_trigger_state": item.non_trigger_state.value,
                "missing_data_action": item.missing_data_action.value,
            }
            for item in definition.rules
        ],
        "execution_timing": definition.execution_timing.value,
        "position_sizing": definition.position_sizing.value,
        "fee_bps_per_side": str(definition.fee_bps_per_side),
        "slippage_bps_per_side": str(definition.slippage_bps_per_side),
        "hypothetical_notional": str(definition.hypothetical_notional),
        "baselines": [item.value for item in definition.baselines],
        "prior_strategy_ids": list(definition.prior_strategy_ids),
        "per_window_policy": list(definition.per_window_policy),
        "aggregate_nomination_policy": list(definition.aggregate_nomination_policy),
        "authority_restrictions": list(definition.authority_restrictions),
    }


@dataclass(frozen=True, slots=True)
class GenericDecision:
    decision_id: str
    execution_at: datetime
    information_through: datetime
    decision_close: Decimal
    feature_values: tuple[tuple[str, Decimal | None], ...]
    prior_state: PositionState
    selected_state: PositionState
    evaluated_rule_id: str
    trigger: bool | None
    eligible: bool
    transition_reason: str
    execution_open: Decimal
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class GenericMetrics:
    portfolio: GenericPortfolio
    scenario: CostScenarioName
    starting_notional: Decimal
    ending_notional: Decimal
    gross_return: Decimal
    net_return: Decimal
    maximum_drawdown: Decimal
    entries: int
    exits: int
    turnover: Decimal
    exposure_percentage: Decimal
    days_long: int
    days_in_cash: int
    modeled_fees: Decimal
    modeled_slippage: Decimal
    total_modeled_cost: Decimal
    complete: bool
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class GenericRunResult:
    run_id: str
    protocol_id: str
    definition_id: str
    plan_id: str
    plan_hash: str
    snapshot_id: str
    snapshot_manifest_hash: str
    snapshot_exposure_state: str
    validation_start: datetime
    validation_end: datetime
    executed_at: datetime
    decisions: tuple[GenericDecision, ...]
    metrics: tuple[GenericMetrics, ...]
    complete: bool
    reproducibility_status: ReproducibilityStatus
    warnings: tuple[str, ...]
    disqualifying_warnings: tuple[str, ...]
    configuration_hash: str
    result_hash: str
    canonical_hash: str
    authority: str = "NONE"


def _feature_value(
    feature: GenericFeatureDefinition,
    information: tuple[CryptoDailyCandle, ...],
) -> Decimal | None:
    source = information[:-1] if feature.exclude_decision_observation else information
    if len(source) < feature.observations:
        return None
    window = source[-feature.observations :]
    if any(
        window[index].bucket_start - window[index - 1].bucket_start != timedelta(days=1)
        for index in range(1, len(window))
    ):
        return None
    closes = tuple(item.close for item in window)
    if feature.kind is FeatureKind.ROLLING_MEAN:
        value = sum(closes, Decimal(0)) / Decimal(len(closes))
    elif feature.kind is FeatureKind.ROLLING_MINIMUM:
        value = min(closes)
    elif feature.kind is FeatureKind.ROLLING_MAXIMUM:
        value = max(closes)
    else:
        value = closes[-1] / closes[0] - Decimal(1)
    return _metric(value)


def _operand_value(
    operand: GenericOperand,
    *,
    decision_close: Decimal,
    features: Mapping[str, Decimal | None],
) -> Decimal | None:
    if operand.kind is OperandKind.DECISION_CLOSE:
        return decision_close
    if operand.kind is OperandKind.CONSTANT:
        return operand.constant
    return features[operand.feature_name or ""]


def _compare(left: Decimal, operator: ComparisonOperator, right: Decimal) -> bool:
    if operator is ComparisonOperator.STRICTLY_GREATER_THAN:
        return left > right
    if operator is ComparisonOperator.STRICTLY_LESS_THAN:
        return left < right
    if operator is ComparisonOperator.GREATER_THAN_OR_EQUAL:
        return left >= right
    return left <= right


def _decision_execution_candles(
    candles: tuple[CryptoDailyCandle, ...],
    cadence: DecisionCadence,
    start: datetime,
    end: datetime,
) -> tuple[CryptoDailyCandle, ...]:
    eligible = tuple(item for item in candles if start <= item.bucket_start < end)
    if cadence is DecisionCadence.DAILY:
        return eligible
    result: list[CryptoDailyCandle] = []
    seen: set[tuple[int, int]] = set()
    for item in eligible:
        key = (item.bucket_start.year, item.bucket_start.month)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return tuple(result)


def _drawdown(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return Decimal(0)
    peak = values[0]
    maximum = Decimal(0)
    for value in values:
        peak = max(peak, value)
        if peak:
            maximum = max(maximum, (peak - value) / peak)
    return _metric(maximum)


def _simulate_metrics(
    *,
    portfolio: GenericPortfolio,
    scenario: CostScenarioName,
    candles: tuple[CryptoDailyCandle, ...],
    decisions: tuple[GenericDecision, ...],
    notional: Decimal,
) -> GenericMetrics:
    fee_rate = (
        Decimal(0) if scenario is CostScenarioName.DIAGNOSTIC_ZERO_COST else Decimal("60") / BPS
    )
    slip_rate = (
        Decimal(0) if scenario is CostScenarioName.DIAGNOSTIC_ZERO_COST else Decimal("10") / BPS
    )
    state = PositionState.CASH
    cash = notional
    units = Decimal(0)
    fees = Decimal(0)
    slippage = Decimal(0)
    turnover_value = Decimal(0)
    entries = 0
    exits = 0
    days_long = 0
    values: list[Decimal] = []
    decisions_by_time = {item.execution_at: item for item in decisions}
    for index, candle in enumerate(candles):
        selected = state
        if portfolio is GenericPortfolio.BUY_AND_HOLD:
            selected = PositionState.LONG if index == 0 else state
        elif portfolio is GenericPortfolio.CANDIDATE and candle.bucket_start in decisions_by_time:
            selected = decisions_by_time[candle.bucket_start].selected_state
        if state is PositionState.CASH and selected is PositionState.LONG:
            execution_price = candle.open * (Decimal(1) + slip_rate)
            fee = cash * fee_rate
            available = cash - fee
            units = available / execution_price
            slippage_cost = units * candle.open * slip_rate
            fees += fee
            slippage += slippage_cost
            turnover_value += available
            cash = Decimal(0)
            state = PositionState.LONG
            entries += 1
        elif state is PositionState.LONG and selected is PositionState.CASH:
            gross = units * candle.open
            fee = gross * fee_rate
            slippage_cost = gross * slip_rate
            cash = gross - fee - slippage_cost
            fees += fee
            slippage += slippage_cost
            turnover_value += gross
            units = Decimal(0)
            state = PositionState.CASH
            exits += 1
        value = cash if state is PositionState.CASH else units * candle.close
        values.append(value)
        if state is PositionState.LONG:
            days_long += 1
    ending = values[-1] if values else notional
    gross_return = (
        Decimal(0)
        if not values
        else _metric(
            (
                ending + fees + slippage
                if scenario is CostScenarioName.CONSERVATIVE_RESEARCH
                else ending
            )
            / notional
            - Decimal(1)
        )
    )
    net_return = _metric(ending / notional - Decimal(1))
    exposure = (
        Decimal(0)
        if not candles
        else _metric(Decimal(days_long) * Decimal(100) / Decimal(len(candles)))
    )
    payload = (
        portfolio,
        scenario,
        notional,
        _metric(ending),
        gross_return,
        net_return,
        _drawdown(tuple(values)),
        entries,
        exits,
        _metric(turnover_value / notional),
        exposure,
        days_long,
        len(candles) - days_long,
        _metric(fees),
        _metric(slippage),
        _metric(fees + slippage),
        bool(candles),
    )
    return GenericMetrics(*payload, content_digest(payload))


def execute_compiled_plan(
    *,
    plan: CompiledCandidatePlan,
    definition: GenericCandidateDefinition,
    run_id: str,
    protocol_id: str,
    snapshot_id: str,
    snapshot_manifest_hash: str,
    snapshot_exposure_state: str,
    validation_start: datetime,
    validation_end: datetime,
    candles: tuple[CryptoDailyCandle, ...],
    executed_at: datetime,
) -> GenericRunResult:
    identity = _identity(run_id, "GENERIC_RUN_ID_INVALID")
    protocol = _identity(protocol_id, "GENERIC_PROTOCOL_ID_INVALID")
    snapshot = _identity(snapshot_id, "GENERIC_SNAPSHOT_ID_INVALID")
    manifest = _hash(snapshot_manifest_hash, "GENERIC_SNAPSHOT_MANIFEST_INVALID")
    start = _aware(validation_start, "GENERIC_VALIDATION_TIME_INVALID")
    end = _aware(validation_end, "GENERIC_VALIDATION_TIME_INVALID")
    execution_time = _aware(executed_at, "GENERIC_RUN_TIME_INVALID")
    expected_plan = compile_candidate_definition(definition)
    if plan.definition_id != definition.definition_id or plan.plan_hash != expected_plan.plan_hash:
        raise GenericCandidateError("GENERIC_PLAN_DEFINITION_MISMATCH")
    try:
        index = definition.approved_snapshot_ids.index(snapshot)
    except ValueError as error:
        raise GenericCandidateError("GENERIC_SNAPSHOT_NOT_APPROVED") from error
    if definition.validation_windows[index] != (start, end):
        raise GenericCandidateError("GENERIC_VALIDATION_WINDOW_MISMATCH")
    if snapshot_exposure_state not in {
        "EXPOSED_DEVELOPMENT_EVIDENCE",
        "FIXTURE",
    }:
        raise GenericCandidateError("GENERIC_SNAPSHOT_EXPOSURE_UNAPPROVED")
    ordered = tuple(sorted(candles, key=lambda item: item.bucket_start))
    if (
        len({item.bucket_start for item in ordered}) != len(ordered)
        or any(item.product_id != plan.product_id for item in ordered)
        or any(item.evidence_class not in definition.allowed_evidence_classes for item in ordered)
    ):
        raise GenericCandidateError("GENERIC_CANDLE_EVIDENCE_INVALID")
    execution_candles = _decision_execution_candles(ordered, plan.cadence, start, end)
    state = plan.initial_state
    decisions: list[GenericDecision] = []
    incomplete = False
    rules_by_state = {item.source_state: item for item in plan.decision_rules}
    for execution_candle in execution_candles:
        information = tuple(
            item for item in ordered if item.bucket_start < execution_candle.bucket_start
        )
        if not information:
            incomplete = True
            continue
        decision_candle = information[-1]
        values = {
            feature.name: _feature_value(feature, information) for feature in plan.required_features
        }
        rule = (
            plan.decision_rules[0]
            if plan.rule_mode is RuleMode.STATELESS_LONG_CASH
            else rules_by_state[state]
        )
        left = _operand_value(rule.left, decision_close=decision_candle.close, features=values)
        right = _operand_value(rule.right, decision_close=decision_candle.close, features=values)
        eligible = left is not None and right is not None
        trigger: bool | None
        selected: PositionState
        reason: str
        if eligible:
            trigger = _compare(left, rule.operator, right)  # type: ignore[arg-type]
            selected = rule.trigger_state if trigger else rule.non_trigger_state
            reason = "RULE_TRIGGERED" if trigger else "STATE_OR_SELECTION_PRESERVED"
        else:
            trigger = None
            if rule.missing_data_action in {
                MissingDataAction.SELECT_CASH,
                MissingDataAction.PRESERVE_CASH,
                MissingDataAction.PROTECTIVE_EXIT_TO_CASH,
            }:
                selected = PositionState.CASH
                reason = rule.missing_data_action.value
            else:
                selected = state
                reason = MissingDataAction.MARK_RUN_INCOMPLETE.value
                incomplete = True
        decision_payload = (
            identity,
            execution_candle.bucket_start,
            decision_candle.bucket_start,
            decision_candle.close,
            tuple(sorted(values.items())),
            state,
            selected,
            rule.rule_id,
            trigger,
            eligible,
            reason,
            execution_candle.open,
        )
        decisions.append(
            GenericDecision(
                content_digest(decision_payload)[:32],
                execution_candle.bucket_start,
                decision_candle.bucket_start,
                decision_candle.close,
                tuple(sorted(values.items())),
                state,
                selected,
                rule.rule_id,
                trigger,
                eligible,
                reason,
                execution_candle.open,
                content_digest(decision_payload),
            )
        )
        state = selected
    validation_candles = tuple(item for item in ordered if start <= item.bucket_start < end)
    metrics = tuple(
        _simulate_metrics(
            portfolio=portfolio,
            scenario=scenario,
            candles=validation_candles,
            decisions=tuple(decisions),
            notional=plan.hypothetical_notional,
        )
        for portfolio in GenericPortfolio
        for scenario in CostScenarioName
    )
    complete = bool(validation_candles) and not incomplete
    reproducibility = (
        ReproducibilityStatus.REPRODUCIBLE if complete else ReproducibilityStatus.NOT_REPRODUCIBLE
    )
    disqualifying = () if complete else ("INCOMPLETE_OR_INVALID_EVIDENCE",)
    configuration_hash = content_digest(
        (
            plan.plan_hash,
            snapshot,
            manifest,
            start,
            end,
            GENERIC_CANDIDATE_EXECUTOR_V1,
        )
    )
    result_payload = (
        GENERIC_CANDIDATE_RUN_V1,
        plan.plan_hash,
        snapshot,
        manifest,
        start,
        end,
        tuple(decisions),
        metrics,
        complete,
        reproducibility,
        plan.warnings,
        disqualifying,
    )
    result_hash = content_digest(result_payload)
    canonical_payload = (
        identity,
        protocol,
        definition.definition_id,
        plan.plan_id,
        plan.plan_hash,
        snapshot,
        manifest,
        snapshot_exposure_state,
        start,
        end,
        execution_time,
        result_hash,
        configuration_hash,
        "NONE",
    )
    return GenericRunResult(
        identity,
        protocol,
        definition.definition_id,
        plan.plan_id,
        plan.plan_hash,
        snapshot,
        manifest,
        snapshot_exposure_state,
        start,
        end,
        execution_time,
        tuple(decisions),
        metrics,
        complete,
        reproducibility,
        plan.warnings,
        disqualifying,
        configuration_hash,
        result_hash,
        content_digest(canonical_payload),
    )


def generic_metric(
    run: GenericRunResult,
    portfolio: GenericPortfolio,
    scenario: CostScenarioName = CostScenarioName.CONSERVATIVE_RESEARCH,
) -> GenericMetrics:
    try:
        return next(
            item
            for item in run.metrics
            if item.portfolio is portfolio and item.scenario is scenario
        )
    except StopIteration as error:
        raise GenericCandidateError("GENERIC_METRIC_MISSING") from error


def generic_window_passes(definition: GenericCandidateDefinition, run: GenericRunResult) -> bool:
    candidate = generic_metric(run, GenericPortfolio.CANDIDATE)
    buy_hold = generic_metric(run, GenericPortfolio.BUY_AND_HOLD)
    conditions = {
        "CONSERVATIVE_NET_RETURN_AT_LEAST_ZERO": candidate.net_return >= 0,
        "DRAWDOWN_STRICTLY_BELOW_BUY_AND_HOLD": (
            candidate.maximum_drawdown < buy_hold.maximum_drawdown
        ),
        "EVIDENCE_COMPLETE": run.complete,
        "REPRODUCIBLE": (run.reproducibility_status is ReproducibilityStatus.REPRODUCIBLE),
        "NO_DISQUALIFYING_WARNING": not run.disqualifying_warnings,
        "AT_LEAST_ONE_LONG_EXPOSURE_PERIOD": candidate.days_long > 0,
    }
    return all(conditions.get(item, False) for item in definition.per_window_policy)


@dataclass(frozen=True, slots=True)
class CampaignPolicyIntegrityFinding:
    finding_id: str
    reviewed_policy_id: str
    reviewed_policy_hash: str
    reviewed_condition: str
    campaign_question: str
    observed_edge_case: str
    logical_analysis: tuple[str, ...]
    degeneracy_risk: str
    recommended_future_treatment: str
    requires_future_policy_version: bool
    actor: str
    reviewed_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        finding_id: str,
        reviewed_policy_id: str,
        reviewed_policy_hash: str,
        campaign_question: str,
        actor: str,
        reviewed_at: datetime,
    ) -> CampaignPolicyIntegrityFinding:
        payload = (
            GENERIC_POLICY_REVIEW_V1,
            _identity(finding_id, "GENERIC_POLICY_FINDING_ID_INVALID"),
            _identity(reviewed_policy_id, "GENERIC_POLICY_ID_INVALID"),
            _hash(reviewed_policy_hash, "GENERIC_POLICY_HASH_INVALID"),
            "AT_LEAST_ONE_LONG_EXPOSURE_PERIOD_PER_WINDOW",
            _text(campaign_question, "GENERIC_POLICY_QUESTION_INVALID"),
            (
                "Candidate 2 selected CASH for the complete later bearish window, "
                "returning 0 with 0 drawdown while buy-and-hold returned -0.46152312 "
                "with 0.53403968 drawdown."
            ),
            (
                "Participation is necessary somewhere in the campaign to distinguish "
                "a market-timing strategy from an always-cash null.",
                "Per-window participation is not logically necessary for a defensive "
                "long-or-cash mechanism whose intended action can be CASH for a full "
                "bearish regime.",
                "Applying participation across the complete dossier preserves the "
                "anti-degeneracy purpose without forcing exposure in every regime.",
                "This conclusion is policy analysis, not a retroactive Candidate 2 pass.",
            ),
            (
                "Removing participation entirely would admit an always-cash definition "
                "that mechanically has non-negative return and zero drawdown."
            ),
            (
                "For a future policy version registered before evaluation, require at "
                "least one eligible LONG period across the complete multi-window dossier "
                "and separately reject definitions observationally equivalent to always "
                "cash. Preserve all per-window return, drawdown, quality, and leakage gates."
            ),
            True,
            _identity(actor, "GENERIC_POLICY_ACTOR_INVALID"),
            _aware(reviewed_at, "GENERIC_POLICY_REVIEW_TIME_INVALID"),
        )
        return cls(*payload[1:], content_digest(payload))
