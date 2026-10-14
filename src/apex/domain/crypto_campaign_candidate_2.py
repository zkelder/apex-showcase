"""Pure rules for crypto campaign Candidate 2 development evidence."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from apex.domain.crypto_campaign_candidate import (
    AUTHORITY_WARNINGS,
    EARLIER_SNAPSHOT_ID,
    EARLIER_VALIDATION_END,
    EARLIER_VALIDATION_START,
    FIXED_CAMPAIGN_ID,
    FIXED_WINDOWS,
    LATER_SNAPSHOT_ID,
    LATER_VALIDATION_END,
    LATER_VALIDATION_START,
    CandidateNominationEvaluation,
    CandidateOutcome,
    CandidateState,
    CandidateWindow,
)
from apex.domain.crypto_dossier import (
    BTC_USD_MONTHLY_200_PRICE_TREND_FAMILY,
    BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY,
    BTC_USD_MONTHLY_STATEFUL_BREAKOUT_FAMILY,
    BTC_USD_SMA_20_100_STRATEGY_FAMILY,
    CryptoStrategySpecification,
)
from apex.domain.crypto_research import (
    COINBASE_GRANULARITY,
    COINBASE_PRODUCT,
    CONSERVATIVE_FEE_BPS,
    CONSERVATIVE_SLIPPAGE_BPS,
    CRYPTO_RESEARCH_COST_MODEL_V1,
    RESEARCH_METRIC_QUANTUM,
    STARTING_RESEARCH_NOTIONAL,
    CostScenarioName,
    CryptoDailyCandle,
)
from apex.domain.research_evidence import (
    EvidenceClass,
    ExperimentState,
    ReproducibilityStatus,
    ResearchHypothesisVersion,
)
from apex.domain.values import content_digest

CRYPTO_CAMPAIGN_CANDIDATE_2_HYPOTHESIS_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_2_HYPOTHESIS_V1"
CRYPTO_CAMPAIGN_CANDIDATE_2_COMPARISON_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_2_COMPARISON_V1"
CRYPTO_CAMPAIGN_CANDIDATE_2_PROTOCOL_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_2_PROTOCOL_V1"
CRYPTO_CAMPAIGN_CANDIDATE_2_RESULT_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_2_RESULT_V1"
CRYPTO_CAMPAIGN_CANDIDATE_2_DOSSIER_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_2_DOSSIER_V1"
CRYPTO_CAMPAIGN_CANDIDATE_2_POLICY_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_2_TWO_WINDOW_POLICY_V1"
CRYPTO_CAMPAIGN_CANDIDATE_2_EVALUATOR_V1 = "BTC_USD_MONTHLY_STATEFUL_90_45_BREAKOUT_EVALUATOR_V1"
CRYPTO_CAMPAIGN_CANDIDATE_2_FEATURE_V1 = "BTC_USD_MONTHLY_STATEFUL_90_45_CLOSE_BREAKOUT_V1"
ENTRY_LOOKBACK = 90
EXIT_LOOKBACK = 45

EXPECTED_MECHANISM = (
    "A stateful upside-breakout entry combined with a shorter downside-breakdown "
    "exit may avoid prolonged weak regimes, reduce repeated entry and exit churn, "
    "and respond asymmetrically to downside deterioration without relying on moving "
    "averages or endpoint momentum."
)

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")


class CryptoCampaignCandidate2Error(ValueError):
    pass


class BreakoutPortfolio(StrEnum):
    MONTHLY_STATEFUL_BREAKOUT = "MONTHLY_STATEFUL_90_45_BREAKOUT"
    CASH = "CASH"
    BUY_AND_HOLD = "BUY_AND_HOLD"


class BreakoutPositionState(StrEnum):
    CASH = "CASH"
    LONG = "LONG"


class BreakoutRule(StrEnum):
    ENTRY_90_CLOSE_MAXIMUM = "ENTRY_90_CLOSE_MAXIMUM"
    EXIT_45_CLOSE_MINIMUM = "EXIT_45_CLOSE_MINIMUM"


class BreakoutTransitionReason(StrEnum):
    ENTRY_BREAKOUT_TRIGGER = "ENTRY_BREAKOUT_TRIGGER"
    EXIT_BREAKDOWN_TRIGGER = "EXIT_BREAKDOWN_TRIGGER"
    PROTECTIVE_DATA_QUALITY_EXIT = "PROTECTIVE_DATA_QUALITY_EXIT"
    STATE_PRESERVED_NO_TRIGGER = "STATE_PRESERVED_NO_TRIGGER"
    INELIGIBLE_REMAIN_CASH = "INELIGIBLE_REMAIN_CASH"
    EXECUTION_UNAVAILABLE_STATE_UNCHANGED = "EXECUTION_UNAVAILABLE_STATE_UNCHANGED"


def _identity(value: str, code: str) -> str:
    normalized = value.strip()
    if _IDENTITY.fullmatch(normalized) is None:
        raise CryptoCampaignCandidate2Error(code)
    return normalized


def _aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CryptoCampaignCandidate2Error(code)
    return value.astimezone(UTC)


def _metric(value: Decimal) -> Decimal:
    return value.quantize(RESEARCH_METRIC_QUANTUM)


def _month_starts(start: datetime, end: datetime) -> tuple[datetime, ...]:
    current = datetime(start.year, start.month, 1, tzinfo=UTC)
    result: list[datetime] = []
    while current < end:
        result.append(current)
        current = (
            datetime(current.year + 1, 1, 1, tzinfo=UTC)
            if current.month == 12
            else datetime(current.year, current.month + 1, 1, tzinfo=UTC)
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class BreakoutHypothesisSpecification:
    hypothesis_version_id: str
    campaign_id: str
    product_id: str
    granularity: str
    evidence_class: EvidenceClass
    snapshot_ids: tuple[str, str]
    validation_windows: tuple[tuple[datetime, datetime], tuple[datetime, datetime]]
    initial_state: BreakoutPositionState
    entry_definition: str
    exit_definition: str
    execution_timing: str
    position_sizing: str
    cost_model: str
    per_window_policy: tuple[str, ...]
    aggregate_policy: tuple[str, ...]
    failure_conditions: tuple[str, ...]
    expected_mechanism: str
    limitations: tuple[str, ...]
    canonical_hash: str
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    opportunity_authority: str = "NONE"
    order_authority: str = "NONE"
    execution_authority: str = "NONE"
    financial_authority: str = "NONE"
    authority: str = "NONE"

    @classmethod
    def fixed(
        cls, hypothesis: ResearchHypothesisVersion, *, campaign_id: str
    ) -> BreakoutHypothesisSpecification:
        payload = (
            CRYPTO_CAMPAIGN_CANDIDATE_2_HYPOTHESIS_V1,
            hypothesis.hypothesis_version_id,
            _identity(campaign_id, "CRYPTO_CAMPAIGN_ID_INVALID"),
            COINBASE_PRODUCT,
            COINBASE_GRANULARITY,
            EvidenceClass.HISTORICAL_PUBLIC,
            (EARLIER_SNAPSHOT_ID, LATER_SNAPSHOT_ID),
            (
                (EARLIER_VALIDATION_START, EARLIER_VALIDATION_END),
                (LATER_VALIDATION_START, LATER_VALIDATION_END),
            ),
            BreakoutPositionState.CASH,
            (
                "While CASH, enter LONG only when the completed month-end decision "
                "close is strictly greater than every one of the preceding 90 accepted "
                "completed daily closes; the decision close is excluded."
            ),
            (
                "While LONG, exit to CASH only when the completed month-end decision "
                "close is strictly less than every one of the preceding 45 accepted "
                "completed daily closes; the decision close is excluded."
            ),
            "PRIOR_MONTH_END_INFORMATION_THEN_NEXT_AVAILABLE_ELIGIBLE_DAILY_OPEN",
            "FULL_100_PERCENT_HYPOTHETICAL_LONG_OR_100_PERCENT_CASH",
            CRYPTO_RESEARCH_COST_MODEL_V1,
            (
                "CONSERVATIVE_NET_RETURN_AT_LEAST_ZERO",
                "MAXIMUM_DRAWDOWN_STRICTLY_BELOW_BUY_AND_HOLD",
                "COMPLETE_AND_REPRODUCIBLE",
                "NO_DISQUALIFYING_WARNING",
                "AT_LEAST_ONE_LONG_EXPOSURE_PERIOD",
            ),
            (
                "BOTH_WINDOWS_PASS_PER_WINDOW_POLICY",
                "COMPOUNDED_CONSERVATIVE_RETURN_STRICTLY_POSITIVE",
                "SAME_SEMANTICS_AND_COST_MODEL_ACROSS_WINDOWS",
            ),
            (
                "INCOMPLETE_ENTRY_REFERENCE_REMAINS_CASH",
                "INCOMPLETE_EXIT_REFERENCE_FORCES_PROTECTIVE_CASH_TRANSITION",
                "DUPLICATE_OR_QUALITY_INELIGIBLE_OBSERVATION",
                "MISSING_MONTH_END_CANDLE",
                "MISSING_NEXT_EXECUTION_OPEN",
                "LEAKAGE_OR_NONREPRODUCIBLE_RESULT",
            ),
            EXPECTED_MECHANISM,
            (
                "EXPOSED_DEVELOPMENT_EVIDENCE_NOT_INDEPENDENT_VALIDATION",
                "RETROSPECTIVE_HISTORY_NOT_VINTAGE_POINT_IN_TIME",
                "MODELED_COSTS_ARE_NOT_A_USER_COINBASE_FEE_TIER",
                "SEALED_HOLDOUT_NOT_ACCESSED_OR_EVALUATED",
                "AUTHORITY_NONE",
            ),
        )
        return cls(*payload[1:], content_digest(payload))


@dataclass(frozen=True, slots=True)
class BreakoutMaterialDifference:
    prior_specification_id: str
    prior_family: str
    changed: tuple[tuple[str, str, str], ...]
    preserved: tuple[tuple[str, str], ...]
    materially_distinct: bool
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class BreakoutSemanticComparisons:
    candidate_specification_id: str
    candidate_semantics_hash: str
    against_sma_20_100: BreakoutMaterialDifference
    against_monthly_252: BreakoutMaterialDifference
    against_monthly_200: BreakoutMaterialDifference
    actor: str
    compared_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        candidate: CryptoStrategySpecification,
        sma: CryptoStrategySpecification,
        monthly_252: CryptoStrategySpecification,
        monthly_200: CryptoStrategySpecification,
        actor: str,
        compared_at: datetime,
    ) -> BreakoutSemanticComparisons:
        if candidate.strategy_family != BTC_USD_MONTHLY_STATEFUL_BREAKOUT_FAMILY:
            raise CryptoCampaignCandidate2Error("CRYPTO_CAMPAIGN_CANDIDATE_2_STRATEGY_INVALID")
        expected = (
            (sma, BTC_USD_SMA_20_100_STRATEGY_FAMILY),
            (monthly_252, BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY),
            (monthly_200, BTC_USD_MONTHLY_200_PRICE_TREND_FAMILY),
        )
        if any(value.strategy_family != family for value, family in expected):
            raise CryptoCampaignCandidate2Error("CRYPTO_CAMPAIGN_CANDIDATE_2_PRIOR_FAMILY_INVALID")
        shared = (
            ("product", COINBASE_PRODUCT),
            ("daily_candles", "TRUE"),
            ("long_or_cash", "TRUE"),
            ("prior_information_only", "TRUE"),
            ("next_open_execution", "TRUE"),
            ("no_short", "TRUE"),
            ("no_leverage", "TRUE"),
            ("costs", "60_FEE_PLUS_10_SLIPPAGE_BPS_PER_SIDE"),
            ("notional", "10000"),
            ("authority", "NONE"),
        )
        changed = (
            (
                (
                    "signal",
                    "FAST_VERSUS_SLOW_MOVING_AVERAGE_CROSSOVER",
                    "STATEFUL_PRIOR_CLOSE_EXTREMA_BREAKOUT",
                ),
                ("cadence", "DAILY", "MONTHLY"),
                ("features", "20_AND_100_CLOSE_AVERAGES", "90_MAXIMUM_AND_45_MINIMUM"),
                ("state", "SYMMETRIC_CROSSOVER", "PERSISTENT_ASYMMETRIC_HYSTERESIS"),
                ("turnover_mechanism", "CONTINUOUS_RECOMPUTATION", "STATE_TRANSITIONS_ONLY"),
            ),
            (
                ("signal", "252_OBSERVATION_ENDPOINT_RETURN", "PRIOR_CLOSE_RANGE_BREAKOUT"),
                ("thresholds", "ONE_SYMMETRIC_THRESHOLD", "SEPARATE_ENTRY_AND_EXIT_RULES"),
                ("state", "STATELESS_MONTHLY_SELECTION", "PERSISTENT_STATE"),
                ("lookback", "ONE_252_OBSERVATION_ANCHOR", "90_MAXIMUM_AND_45_MINIMUM"),
                ("risk_exit", "ENDPOINT_MOMENTUM_SELECTION", "SHORTER_DOWNSIDE_BREAKDOWN"),
            ),
            (
                ("signal", "PRICE_VERSUS_200_CLOSE_AVERAGE", "PRIOR_CLOSE_EXTREMA_BREAKOUT"),
                ("thresholds", "ONE_SYMMETRIC_SELECTION", "ASYMMETRIC_ENTRY_AND_EXIT"),
                ("features", "200_CLOSE_ARITHMETIC_MEAN", "90_MAXIMUM_AND_45_MINIMUM"),
                ("state", "STATELESS_MONTHLY_SELECTION", "PERSISTENT_STATE"),
                ("downside_response", "PRICE_VERSUS_AVERAGE", "45_CLOSE_BREAKDOWN"),
            ),
        )

        def comparison(
            prior: CryptoStrategySpecification,
            differences: tuple[tuple[str, str, str], ...],
        ) -> BreakoutMaterialDifference:
            material = (
                prior.semantics_hash != candidate.semantics_hash
                and prior.strategy_family != candidate.strategy_family
                and bool(differences)
            )
            if not material:
                raise CryptoCampaignCandidate2Error(
                    "CRYPTO_CAMPAIGN_CANDIDATE_2_MATERIAL_DIFFERENCE_REQUIRED"
                )
            payload = (
                CRYPTO_CAMPAIGN_CANDIDATE_2_COMPARISON_V1,
                prior.specification_id,
                prior.strategy_family,
                candidate.specification_id,
                prior.semantics_hash,
                candidate.semantics_hash,
                differences,
                shared,
                material,
            )
            return BreakoutMaterialDifference(
                prior.specification_id,
                prior.strategy_family,
                differences,
                shared,
                material,
                content_digest(payload),
            )

        comparisons = tuple(
            comparison(prior, differences)
            for (prior, _family), differences in zip(expected, changed, strict=True)
        )
        compared = _aware(compared_at, "CRYPTO_CAMPAIGN_CANDIDATE_2_COMPARISON_TIME_INVALID")
        normalized_actor = _identity(actor, "CRYPTO_CAMPAIGN_CANDIDATE_2_COMPARISON_ACTOR_INVALID")
        payload = (
            candidate.specification_id,
            candidate.semantics_hash,
            comparisons,
            normalized_actor,
            compared,
        )
        return cls(
            candidate.specification_id,
            candidate.semantics_hash,
            comparisons[0],
            comparisons[1],
            comparisons[2],
            normalized_actor,
            compared,
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class BreakoutProtocol:
    protocol_id: str
    campaign_id: str
    candidate_id: str
    hypothesis_version_id: str
    strategy_specification_id: str
    strategy_version: int
    strategy_semantics_hash: str
    semantic_comparisons_hash: str
    windows: tuple[CandidateWindow, CandidateWindow]
    snapshot_manifest_hashes: tuple[str, str]
    initial_state: BreakoutPositionState
    evaluator_identity: str
    feature_semantics: str
    entry_semantics: str
    exit_semantics: str
    state_transition_semantics: str
    protective_exit_semantics: str
    execution_semantics: str
    cost_model: str
    baseline_definitions: tuple[str, ...]
    per_window_policy: str
    aggregate_policy: str
    deterministic_seed: int
    code_revision: str
    evidence_classification: str
    protocol_hash: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def fixed(
        cls,
        *,
        protocol_id: str,
        campaign_id: str,
        candidate_id: str,
        hypothesis: BreakoutHypothesisSpecification,
        strategy: CryptoStrategySpecification,
        comparisons: BreakoutSemanticComparisons,
        snapshot_manifest_hashes: tuple[str, str],
        code_revision: str,
        created_at: datetime,
    ) -> BreakoutProtocol:
        if strategy.strategy_family != BTC_USD_MONTHLY_STATEFUL_BREAKOUT_FAMILY:
            raise CryptoCampaignCandidate2Error("CRYPTO_CAMPAIGN_CANDIDATE_2_PROTOCOL_INVALID")
        if len(snapshot_manifest_hashes) != 2 or any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None for value in snapshot_manifest_hashes
        ):
            raise CryptoCampaignCandidate2Error(
                "CRYPTO_CAMPAIGN_CANDIDATE_2_MANIFEST_BINDING_INVALID"
            )
        payload = (
            CRYPTO_CAMPAIGN_CANDIDATE_2_PROTOCOL_V1,
            _identity(campaign_id, "CRYPTO_CAMPAIGN_ID_INVALID"),
            _identity(candidate_id, "CRYPTO_CAMPAIGN_CANDIDATE_ID_INVALID"),
            hypothesis.hypothesis_version_id,
            strategy.specification_id,
            strategy.version,
            strategy.semantics_hash,
            comparisons.canonical_hash,
            FIXED_WINDOWS,
            snapshot_manifest_hashes,
            BreakoutPositionState.CASH,
            CRYPTO_CAMPAIGN_CANDIDATE_2_EVALUATOR_V1,
            CRYPTO_CAMPAIGN_CANDIDATE_2_FEATURE_V1,
            "CASH_ONLY_STRICT_CLOSE_ABOVE_PRECEDING_90_CLOSE_MAXIMUM",
            "LONG_ONLY_STRICT_CLOSE_BELOW_PRECEDING_45_CLOSE_MINIMUM",
            "PERSIST_STATE_UNLESS_APPLICABLE_RULE_TRIGGERS",
            "INVALID_EXIT_EVIDENCE_FORCES_NEXT_OPEN_PROTECTIVE_CASH_TRANSITION",
            "PRIOR_MONTH_END_INFORMATION_THEN_NEXT_AVAILABLE_ELIGIBLE_DAILY_OPEN",
            CRYPTO_RESEARCH_COST_MODEL_V1,
            (
                "CASH",
                "BUY_AND_HOLD",
                "RETAINED_REJECTED_SMA_20_100_SAME_WINDOW",
                "RETAINED_REJECTED_MONTHLY_252_SAME_WINDOW",
                "RETAINED_REJECTED_MONTHLY_200_SAME_WINDOW",
            ),
            CRYPTO_CAMPAIGN_CANDIDATE_2_POLICY_V1,
            CRYPTO_CAMPAIGN_CANDIDATE_2_POLICY_V1,
            0,
            _identity(code_revision, "CRYPTO_CAMPAIGN_CANDIDATE_CODE_REVISION_INVALID"),
            "EXPOSED_DEVELOPMENT_EVIDENCE_NOT_INDEPENDENT_VALIDATION",
        )
        protocol_hash = content_digest(payload)
        identity = _identity(protocol_id, "CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_ID_INVALID")
        created = _aware(created_at, "CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_TIME_INVALID")
        return cls(
            identity,
            *payload[1:],
            protocol_hash,
            created,
            content_digest((identity, protocol_hash, created)),
        )


@dataclass(frozen=True, slots=True)
class BreakoutDecision:
    decision_month: datetime
    prior_state: BreakoutPositionState
    information_through: datetime | None
    information_available_at: datetime | None
    decision_close: Decimal | None
    evaluated_rule: BreakoutRule
    reference_start: datetime | None
    reference_end: datetime | None
    reference_count: int
    entry_maximum: Decimal | None
    exit_minimum: Decimal | None
    trigger: bool
    resulting_state: BreakoutPositionState
    execution_at: datetime | None
    execution_open: Decimal | None
    transition_reason: BreakoutTransitionReason
    data_quality_status: str
    eligible: bool
    ineligibility_reason: str | None
    canonical_hash: str


def build_monthly_stateful_breakout_decisions(
    candles: tuple[CryptoDailyCandle, ...],
    *,
    validation_start: datetime,
    validation_end: datetime,
) -> tuple[BreakoutDecision, ...]:
    ordered = tuple(sorted(candles, key=lambda item: item.bucket_start))
    if len({item.bucket_start for item in ordered}) != len(ordered):
        raise CryptoCampaignCandidate2Error("CRYPTO_CAMPAIGN_CANDIDATE_2_DUPLICATE_OBSERVATION")
    state = BreakoutPositionState.CASH
    decisions: list[BreakoutDecision] = []
    for month_start in _month_starts(validation_start, validation_end):
        prior_state = state
        rule = (
            BreakoutRule.ENTRY_90_CLOSE_MAXIMUM
            if prior_state is BreakoutPositionState.CASH
            else BreakoutRule.EXIT_45_CLOSE_MINIMUM
        )
        lookback = ENTRY_LOOKBACK if prior_state is BreakoutPositionState.CASH else EXIT_LOOKBACK
        prior_year = month_start.year if month_start.month > 1 else month_start.year - 1
        prior_month = month_start.month - 1 if month_start.month > 1 else 12
        expected_month_end = datetime(
            prior_year,
            prior_month,
            calendar.monthrange(prior_year, prior_month)[1],
            tzinfo=UTC,
        )
        next_month = (
            datetime(month_start.year + 1, 1, 1, tzinfo=UTC)
            if month_start.month == 12
            else datetime(month_start.year, month_start.month + 1, 1, tzinfo=UTC)
        )
        execution = next(
            (item for item in ordered if month_start <= item.bucket_start < next_month),
            None,
        )
        prior = tuple(item for item in ordered if item.bucket_start < month_start)
        recent = prior[-1] if prior else None
        reference_source = prior[:-1] if recent is not None else ()
        reference = (
            reference_source[-lookback:] if len(reference_source) >= lookback else reference_source
        )
        reason: str | None = None
        if recent is None or recent.bucket_start != expected_month_end:
            reason = "FINAL_PRECEDING_MONTH_OBSERVATION_MISSING"
        elif len(reference) < lookback:
            reason = f"FEWER_THAN_{lookback}_PRECEDING_REFERENCE_OBSERVATIONS"
        elif reference[-1].bucket_start != recent.bucket_start - timedelta(days=1):
            reason = f"PRECEDING_{lookback}_REFERENCE_SEQUENCE_NOT_ADJACENT"
        elif reference[-1].bucket_start - reference[0].bucket_start != timedelta(days=lookback - 1):
            reason = f"PRECEDING_{lookback}_REFERENCE_SEQUENCE_HAS_GAP"
        elif execution is None:
            reason = "NEXT_ELIGIBLE_EXECUTION_OPEN_UNAVAILABLE"
        information_available_at = (
            recent.bucket_start + timedelta(days=1) if recent is not None else None
        )
        if (
            reason is None
            and execution is not None
            and information_available_at is not None
            and information_available_at > execution.bucket_start
        ):
            reason = "INFORMATION_NOT_AVAILABLE_BEFORE_EXECUTION"
        entry_maximum = (
            max(item.close for item in reference)
            if reason is None and rule is BreakoutRule.ENTRY_90_CLOSE_MAXIMUM
            else None
        )
        exit_minimum = (
            min(item.close for item in reference)
            if reason is None and rule is BreakoutRule.EXIT_45_CLOSE_MINIMUM
            else None
        )
        trigger = bool(
            recent is not None
            and (
                (entry_maximum is not None and recent.close > entry_maximum)
                or (exit_minimum is not None and recent.close < exit_minimum)
            )
        )
        if reason is None and trigger:
            state = (
                BreakoutPositionState.LONG
                if prior_state is BreakoutPositionState.CASH
                else BreakoutPositionState.CASH
            )
            transition = (
                BreakoutTransitionReason.ENTRY_BREAKOUT_TRIGGER
                if state is BreakoutPositionState.LONG
                else BreakoutTransitionReason.EXIT_BREAKDOWN_TRIGGER
            )
        elif reason is None:
            state = prior_state
            transition = BreakoutTransitionReason.STATE_PRESERVED_NO_TRIGGER
        elif prior_state is BreakoutPositionState.LONG and execution is not None:
            state = BreakoutPositionState.CASH
            transition = BreakoutTransitionReason.PROTECTIVE_DATA_QUALITY_EXIT
        elif execution is None:
            state = prior_state
            transition = BreakoutTransitionReason.EXECUTION_UNAVAILABLE_STATE_UNCHANGED
        else:
            state = BreakoutPositionState.CASH
            transition = BreakoutTransitionReason.INELIGIBLE_REMAIN_CASH
        payload = (
            CRYPTO_CAMPAIGN_CANDIDATE_2_FEATURE_V1,
            month_start,
            prior_state,
            recent.bucket_start if recent is not None else None,
            information_available_at,
            recent.close if recent is not None else None,
            rule,
            reference[0].bucket_start if len(reference) == lookback else None,
            reference[-1].bucket_start if len(reference) == lookback else None,
            len(reference),
            _metric(entry_maximum) if entry_maximum is not None else None,
            _metric(exit_minimum) if exit_minimum is not None else None,
            trigger,
            state,
            execution.bucket_start if execution is not None else None,
            execution.open if execution is not None else None,
            transition,
            "VALID" if reason is None else "INELIGIBLE_FAIL_CLOSED",
            reason is None,
            reason,
        )
        decisions.append(BreakoutDecision(*payload[1:], content_digest(payload)))
    return tuple(decisions)


@dataclass(frozen=True, slots=True)
class BreakoutHolding:
    bucket_start: datetime
    portfolio: BreakoutPortfolio
    scenario: CostScenarioName
    state: str
    cash: Decimal
    units: Decimal
    marked_value: Decimal
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class BreakoutMetrics:
    portfolio: BreakoutPortfolio
    scenario: CostScenarioName
    validation_start: datetime
    validation_end: datetime
    starting_notional: Decimal
    ending_notional: Decimal
    gross_return: Decimal
    net_return: Decimal
    maximum_drawdown: Decimal
    realized_volatility: Decimal
    entries: int
    exits: int
    completed_round_trips: int
    strategy_triggered_transitions: int
    protective_data_quality_transitions: int
    turnover: Decimal
    exposure_percentage: Decimal
    days_long: int
    days_in_cash: int
    monthly_decisions: int
    eligible_decisions: int
    ineligible_decisions: int
    entry_rule_evaluations: int
    exit_rule_evaluations: int
    entry_triggers: int
    exit_triggers: int
    modeled_fees: Decimal
    modeled_slippage: Decimal
    total_modeled_cost: Decimal
    missing_or_excluded_observations: int
    complete: bool
    reproducibility_status: ReproducibilityStatus
    holdings: tuple[BreakoutHolding, ...]
    canonical_hash: str


def _maximum_drawdown(values: tuple[Decimal, ...]) -> Decimal:
    peak = values[0]
    maximum = Decimal(0)
    for value in values:
        peak = max(peak, value)
        if peak:
            maximum = max(maximum, (peak - value) / peak)
    return maximum


def _volatility(values: tuple[Decimal, ...]) -> Decimal:
    returns = tuple(
        values[index] / values[index - 1] - Decimal(1)
        for index in range(1, len(values))
        if values[index - 1] != 0
    )
    if len(returns) < 2:
        return Decimal(0)
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    variance = sum(((value - mean) ** 2 for value in returns), Decimal(0)) / Decimal(
        len(returns) - 1
    )
    return (variance * Decimal(365)).sqrt()


def simulate_breakout_portfolio(
    *,
    portfolio: BreakoutPortfolio,
    scenario: CostScenarioName,
    candles: tuple[CryptoDailyCandle, ...],
    decisions: tuple[BreakoutDecision, ...],
    validation_start: datetime,
    validation_end: datetime,
    gross_return: Decimal | None = None,
) -> BreakoutMetrics:
    validation = tuple(
        item
        for item in sorted(candles, key=lambda item: item.bucket_start)
        if validation_start <= item.bucket_start < validation_end
    )
    missing = (validation_end - validation_start).days - len(validation)
    decision_map = {item.execution_at: item for item in decisions if item.execution_at is not None}
    fee_rate = (
        Decimal(0)
        if scenario is CostScenarioName.DIAGNOSTIC_ZERO_COST
        else CONSERVATIVE_FEE_BPS / Decimal(10_000)
    )
    slippage_rate = (
        Decimal(0)
        if scenario is CostScenarioName.DIAGNOSTIC_ZERO_COST
        else CONSERVATIVE_SLIPPAGE_BPS / Decimal(10_000)
    )
    cash = STARTING_RESEARCH_NOTIONAL
    units = Decimal(0)
    entries = exits = days_long = 0
    fees = slippage = turnover_notional = Decimal(0)
    values: list[Decimal] = [STARTING_RESEARCH_NOTIONAL]
    holdings: list[BreakoutHolding] = []
    for candle in validation:
        if portfolio is BreakoutPortfolio.CASH:
            desired = False
        elif portfolio is BreakoutPortfolio.BUY_AND_HOLD:
            desired = True
        else:
            decision = decision_map.get(candle.bucket_start)
            desired = (
                units > 0
                if decision is None
                else decision.resulting_state is BreakoutPositionState.LONG
            )
        if desired and units == 0:
            execution_price = candle.open * (Decimal(1) + slippage_rate)
            units = cash / (execution_price * (Decimal(1) + fee_rate))
            notional = units * execution_price
            fees += notional * fee_rate
            slippage += units * candle.open * slippage_rate
            turnover_notional += units * candle.open
            cash = Decimal(0)
            entries += 1
        elif not desired and units > 0:
            execution_price = candle.open * (Decimal(1) - slippage_rate)
            proceeds = units * execution_price
            fees += proceeds * fee_rate
            slippage += units * candle.open * slippage_rate
            turnover_notional += units * candle.open
            cash = proceeds - proceeds * fee_rate
            units = Decimal(0)
            exits += 1
        if units > 0:
            days_long += 1
        marked = cash + units * candle.close
        values.append(marked)
        holding_payload = (
            candle.bucket_start,
            portfolio,
            scenario,
            "LONG" if units > 0 else "CASH",
            _metric(cash),
            _metric(units),
            _metric(marked),
        )
        holdings.append(BreakoutHolding(*holding_payload, content_digest(holding_payload)))
    if units > 0 and validation:
        final_close = validation[-1].close
        execution_price = final_close * (Decimal(1) - slippage_rate)
        proceeds = units * execution_price
        fees += proceeds * fee_rate
        slippage += units * final_close * slippage_rate
        turnover_notional += units * final_close
        cash = proceeds - proceeds * fee_rate
        units = Decimal(0)
        exits += 1
        values[-1] = cash
        last = holdings[-1]
        liquidation_payload = (
            last.bucket_start,
            portfolio,
            scenario,
            "CASH_AFTER_END_OF_WINDOW_LIQUIDATION",
            _metric(cash),
            Decimal(0),
            _metric(cash),
        )
        holdings[-1] = BreakoutHolding(*liquidation_payload, content_digest(liquidation_payload))
    ending = _metric(cash)
    net = _metric(ending / STARTING_RESEARCH_NOTIONAL - Decimal(1))
    gross = net if gross_return is None else _metric(gross_return)
    ineligible = sum(not item.eligible for item in decisions)
    eligible = len(decisions) - ineligible
    is_strategy = portfolio is BreakoutPortfolio.MONTHLY_STATEFUL_BREAKOUT
    complete = missing == 0 and (not is_strategy or ineligible == 0)
    strategy_transitions = sum(
        item.transition_reason
        in {
            BreakoutTransitionReason.ENTRY_BREAKOUT_TRIGGER,
            BreakoutTransitionReason.EXIT_BREAKDOWN_TRIGGER,
        }
        for item in decisions
    )
    protective_transitions = sum(
        item.transition_reason is BreakoutTransitionReason.PROTECTIVE_DATA_QUALITY_EXIT
        for item in decisions
    )
    metric_payload = (
        portfolio,
        scenario,
        validation_start,
        validation_end,
        STARTING_RESEARCH_NOTIONAL,
        ending,
        gross,
        net,
        _metric(_maximum_drawdown(tuple(values))),
        _metric(_volatility(tuple(values))),
        entries,
        exits,
        min(entries, exits),
        strategy_transitions if is_strategy else 0,
        protective_transitions if is_strategy else 0,
        _metric(turnover_notional / STARTING_RESEARCH_NOTIONAL),
        _metric(Decimal(days_long) / Decimal(len(validation)) * Decimal(100))
        if validation
        else Decimal(0),
        days_long,
        len(validation) - days_long,
        len(decisions) if is_strategy else 0,
        eligible if is_strategy else 0,
        ineligible if is_strategy else 0,
        sum(
            item.eligible and item.evaluated_rule is BreakoutRule.ENTRY_90_CLOSE_MAXIMUM
            for item in decisions
        )
        if is_strategy
        else 0,
        sum(
            item.eligible and item.evaluated_rule is BreakoutRule.EXIT_45_CLOSE_MINIMUM
            for item in decisions
        )
        if is_strategy
        else 0,
        sum(
            item.trigger and item.evaluated_rule is BreakoutRule.ENTRY_90_CLOSE_MAXIMUM
            for item in decisions
        )
        if is_strategy
        else 0,
        sum(
            item.trigger and item.evaluated_rule is BreakoutRule.EXIT_45_CLOSE_MINIMUM
            for item in decisions
        )
        if is_strategy
        else 0,
        _metric(fees),
        _metric(slippage),
        _metric(fees + slippage),
        max(missing, 0),
        complete,
        ReproducibilityStatus.REPRODUCIBLE if complete else ReproducibilityStatus.NOT_REPRODUCIBLE,
        tuple(holdings),
    )
    return BreakoutMetrics(*metric_payload, content_digest(metric_payload))


def calculate_breakout_metrics(
    *,
    candles: tuple[CryptoDailyCandle, ...],
    decisions: tuple[BreakoutDecision, ...],
    validation_start: datetime,
    validation_end: datetime,
) -> tuple[BreakoutMetrics, ...]:
    result: list[BreakoutMetrics] = []
    for portfolio in BreakoutPortfolio:
        zero = simulate_breakout_portfolio(
            portfolio=portfolio,
            scenario=CostScenarioName.DIAGNOSTIC_ZERO_COST,
            candles=candles,
            decisions=decisions,
            validation_start=validation_start,
            validation_end=validation_end,
        )
        conservative = simulate_breakout_portfolio(
            portfolio=portfolio,
            scenario=CostScenarioName.CONSERVATIVE_RESEARCH,
            candles=candles,
            decisions=decisions,
            validation_start=validation_start,
            validation_end=validation_end,
            gross_return=zero.net_return,
        )
        result.extend((zero, conservative))
    return tuple(result)


def breakout_metric(
    metrics: tuple[BreakoutMetrics, ...],
    portfolio: BreakoutPortfolio,
    scenario: CostScenarioName = CostScenarioName.CONSERVATIVE_RESEARCH,
) -> BreakoutMetrics:
    try:
        return next(
            item for item in metrics if item.portfolio is portfolio and item.scenario is scenario
        )
    except StopIteration as error:
        raise CryptoCampaignCandidate2Error("CRYPTO_CAMPAIGN_CANDIDATE_2_METRIC_MISSING") from error


@dataclass(frozen=True, slots=True)
class BreakoutPriorStrategyComparison:
    strategy_family: str
    prior_run_id: str
    prior_result_hash: str
    prior_canonical_run_hash: str
    candidate_net_return_delta: Decimal
    candidate_drawdown_delta: Decimal
    candidate_turnover_delta: Decimal
    candidate_cost_delta: Decimal
    candidate_exposure_delta: Decimal
    candidate_trade_count_delta: int
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        strategy_family: str,
        prior_run_id: str,
        prior_result_hash: str,
        prior_canonical_run_hash: str,
        prior_net_return: Decimal,
        prior_drawdown: Decimal,
        prior_turnover: Decimal,
        prior_cost: Decimal,
        prior_exposure: Decimal,
        prior_trade_count: int,
        candidate: BreakoutMetrics,
    ) -> BreakoutPriorStrategyComparison:
        payload = (
            strategy_family,
            prior_run_id,
            prior_result_hash,
            prior_canonical_run_hash,
            _metric(candidate.net_return - prior_net_return),
            _metric(candidate.maximum_drawdown - prior_drawdown),
            _metric(candidate.turnover - prior_turnover),
            _metric(candidate.total_modeled_cost - prior_cost),
            _metric(candidate.exposure_percentage - prior_exposure),
            candidate.entries + candidate.exits - prior_trade_count,
        )
        return cls(*payload, content_digest(payload))


@dataclass(frozen=True, slots=True)
class BreakoutRun:
    run_id: str
    campaign_id: str
    candidate_id: str
    protocol_id: str
    hypothesis_version_id: str
    strategy_specification_id: str
    strategy_version: int
    strategy_semantics_hash: str
    snapshot_id: str
    snapshot_manifest_hash: str
    snapshot_exposure_state: str
    validation_start: datetime
    validation_end: datetime
    initial_state: BreakoutPositionState
    code_revision: str
    deterministic_seed: int
    executed_at: datetime
    state: ExperimentState
    complete: bool
    reproducibility_status: ReproducibilityStatus
    evidence_class: EvidenceClass
    decisions: tuple[BreakoutDecision, ...]
    metrics: tuple[BreakoutMetrics, ...]
    prior_comparisons: tuple[
        BreakoutPriorStrategyComparison,
        BreakoutPriorStrategyComparison,
        BreakoutPriorStrategyComparison,
    ]
    warnings: tuple[str, ...]
    disqualifying_warnings: tuple[str, ...]
    failure_reason: str | None
    configuration_hash: str
    result_hash: str
    canonical_hash: str
    authority: str = "NONE"


def breakout_window_passes(run: BreakoutRun) -> bool:
    candidate = breakout_metric(run.metrics, BreakoutPortfolio.MONTHLY_STATEFUL_BREAKOUT)
    baseline = breakout_metric(run.metrics, BreakoutPortfolio.BUY_AND_HOLD)
    return (
        candidate.net_return >= 0
        and candidate.maximum_drawdown < baseline.maximum_drawdown
        and run.complete
        and run.reproducibility_status is ReproducibilityStatus.REPRODUCIBLE
        and not run.disqualifying_warnings
        and candidate.days_long > 0
    )


@dataclass(frozen=True, slots=True)
class BreakoutDossier:
    dossier_id: str
    campaign_id: str
    candidate_id: str
    candidate_registration_hash: str
    hypothesis_version_id: str
    strategy_specification_id: str
    strategy_semantics_hash: str
    semantic_comparisons_hash: str
    protocol_id: str
    protocol_hash: str
    snapshot_ids: tuple[str, str]
    snapshot_manifest_hashes: tuple[str, str]
    snapshot_exposure_states: tuple[str, str]
    window_run_ids: tuple[str, str]
    window_result_hashes: tuple[str, str]
    window_outcome_ids: tuple[str, str]
    complete_windows: int
    passing_windows: int
    failing_windows: int
    inconclusive_windows: int
    compounded_conservative_return: Decimal
    median_conservative_return: Decimal
    worst_window_return: Decimal
    best_window_return: Decimal
    worst_maximum_drawdown: Decimal
    median_turnover: Decimal
    total_modeled_fees: Decimal
    total_modeled_slippage: Decimal
    total_modeled_costs: Decimal
    total_entries: int
    total_exits: int
    total_strategy_triggered_transitions: int
    total_protective_data_quality_transitions: int
    percentage_windows_beating_cash: Decimal
    percentage_windows_reducing_drawdown: Decimal
    percentage_windows_improving_sma: Decimal
    percentage_windows_improving_monthly_252: Decimal
    percentage_windows_improving_monthly_200: Decimal
    consistency_status: str
    warnings: tuple[str, ...]
    holdout_accessed: bool
    independent_validation: bool
    network_request_performed: bool
    membership_hash: str
    created_at: datetime
    canonical_hash: str
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    opportunity_authority: str = "NONE"
    order_authority: str = "NONE"
    execution_authority: str = "NONE"
    financial_authority: str = "NONE"
    authority: str = "NONE"


__all__ = [
    "AUTHORITY_WARNINGS",
    "ENTRY_LOOKBACK",
    "EXIT_LOOKBACK",
    "EXPECTED_MECHANISM",
    "FIXED_CAMPAIGN_ID",
    "FIXED_WINDOWS",
    "BreakoutDecision",
    "BreakoutDossier",
    "BreakoutHolding",
    "BreakoutHypothesisSpecification",
    "BreakoutMaterialDifference",
    "BreakoutMetrics",
    "BreakoutNominationEvaluation",
    "BreakoutOutcome",
    "BreakoutPortfolio",
    "BreakoutPositionState",
    "BreakoutPriorStrategyComparison",
    "BreakoutProtocol",
    "BreakoutRule",
    "BreakoutRun",
    "BreakoutSemanticComparisons",
    "BreakoutTransitionReason",
    "CandidateNominationEvaluation",
    "CandidateOutcome",
    "CandidateState",
    "CryptoCampaignCandidate2Error",
    "breakout_metric",
    "breakout_window_passes",
    "build_monthly_stateful_breakout_decisions",
    "calculate_breakout_metrics",
]

# Candidate 2 uses the campaign's generic outcome and nomination projections.
BreakoutOutcome = CandidateOutcome
BreakoutNominationEvaluation = CandidateNominationEvaluation
