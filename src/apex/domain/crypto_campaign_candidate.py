"""Pure rules for crypto campaign Candidate 1 development evidence."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from apex.domain.crypto_dossier import (
    BTC_USD_MONTHLY_200_PRICE_TREND_FAMILY,
    BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY,
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
    ExperimentOutcomeDecision,
    ExperimentState,
    ReproducibilityStatus,
    ResearchHypothesisVersion,
)
from apex.domain.values import content_digest

CRYPTO_CAMPAIGN_CANDIDATE_HYPOTHESIS_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_HYPOTHESIS_V1"
CRYPTO_CAMPAIGN_CANDIDATE_COMPARISON_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_COMPARISON_V1"
CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_V1"
CRYPTO_CAMPAIGN_CANDIDATE_RESULT_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_RESULT_V1"
CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_V1"
CRYPTO_CAMPAIGN_CANDIDATE_NOMINATION_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_NOMINATION_V1"
CRYPTO_CAMPAIGN_CANDIDATE_POLICY_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_1_TWO_WINDOW_POLICY_V1"
CRYPTO_CAMPAIGN_CANDIDATE_EVALUATOR_V1 = "BTC_USD_MONTHLY_200_PRICE_TREND_EVALUATOR_V1"
CRYPTO_CAMPAIGN_CANDIDATE_FEATURE_V1 = "BTC_USD_MONTH_END_CLOSE_VS_200_CLOSE_MEAN_V1"

FIXED_CAMPAIGN_ID = "btc-usd-risk-managed-long-cash-campaign-v1"
EARLIER_SNAPSHOT_ID = "btc-usd-genuine-replication-snapshot-2022-07-01-2024-07-01-v1"
LATER_SNAPSHOT_ID = "btc-usd-genuine-snapshot-2024-07-01-2026-07-01-v1"
EARLIER_VALIDATION_START = datetime(2023, 7, 1, tzinfo=UTC)
EARLIER_VALIDATION_END = datetime(2024, 7, 1, tzinfo=UTC)
LATER_VALIDATION_START = datetime(2025, 7, 1, tzinfo=UTC)
LATER_VALIDATION_END = datetime(2026, 7, 1, tzinfo=UTC)
LOOKBACK_OBSERVATIONS = 200

EXPECTED_MECHANISM = (
    "A medium-horizon price-versus-average trend filter may exit sustained declines "
    "sooner than a 252-observation endpoint-momentum rule while avoiding the higher "
    "turnover and whipsaw of a daily fast/slow crossover."
)
DEVELOPMENT_WARNING = "EXPOSED DEVELOPMENT EVIDENCE — DOES NOT PROVIDE INDEPENDENT VALIDATION"
AUTHORITY_WARNINGS = (
    DEVELOPMENT_WARNING,
    "HYPOTHETICAL RESEARCH NOTIONAL — NOT ACCOUNT VALUE OR EARNED PROFIT",
    "SEALED HOLDOUT NOT ACCESSED, RELEASED, OR EVALUATED",
    "NO PAPER, LIVE, CAPITAL, OPPORTUNITY, ORDER, EXECUTION, OR FINANCIAL AUTHORITY",
)

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")


class CryptoCampaignCandidateError(ValueError):
    pass


class CandidatePortfolio(StrEnum):
    MONTHLY_200_PRICE_TREND = "MONTHLY_200_PRICE_TREND"
    CASH = "CASH"
    BUY_AND_HOLD = "BUY_AND_HOLD"


class CandidateState(StrEnum):
    DEVELOPMENT_EVALUATED = "DEVELOPMENT_EVALUATED"
    NOMINATION_ELIGIBLE = "NOMINATION_ELIGIBLE"
    NOT_NOMINATION_ELIGIBLE = "NOT_NOMINATION_ELIGIBLE"
    NOMINATED = "NOMINATED"


def _identity(value: str, code: str) -> str:
    normalized = value.strip()
    if _IDENTITY.fullmatch(normalized) is None:
        raise CryptoCampaignCandidateError(code)
    return normalized


def _aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CryptoCampaignCandidateError(code)
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
class CandidateHypothesisSpecification:
    hypothesis_version_id: str
    campaign_id: str
    product_id: str
    granularity: str
    evidence_class: EvidenceClass
    snapshot_ids: tuple[str, str]
    validation_windows: tuple[tuple[datetime, datetime], tuple[datetime, datetime]]
    signal_definition: str
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
    ) -> CandidateHypothesisSpecification:
        payload = (
            CRYPTO_CAMPAIGN_CANDIDATE_HYPOTHESIS_V1,
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
            (
                "At each calendar-month boundary, select long only when the final "
                "completed preceding-month close is strictly greater than the arithmetic "
                "mean of that close and the preceding 199 accepted completed closes."
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
                "INSUFFICIENT_200_OBSERVATIONS",
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
class MaterialDifference:
    prior_specification_id: str
    prior_family: str
    changed: tuple[tuple[str, str, str], ...]
    preserved: tuple[tuple[str, str], ...]
    materially_distinct: bool
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class CandidateSemanticComparisons:
    candidate_specification_id: str
    candidate_semantics_hash: str
    against_sma_20_100: MaterialDifference
    against_monthly_252: MaterialDifference
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        candidate: CryptoStrategySpecification,
        sma: CryptoStrategySpecification,
        monthly_252: CryptoStrategySpecification,
    ) -> CandidateSemanticComparisons:
        if candidate.strategy_family != BTC_USD_MONTHLY_200_PRICE_TREND_FAMILY:
            raise CryptoCampaignCandidateError("CRYPTO_CAMPAIGN_CANDIDATE_STRATEGY_INVALID")
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
        sma_changed = (
            ("signal", "FAST_VERSUS_SLOW_CROSSOVER", "ONE_PRICE_VERSUS_ONE_AVERAGE"),
            ("cadence", "DAILY", "MONTHLY"),
            ("features", "20_AND_100_CLOSE_SMAS", "ONE_200_CLOSE_MEAN"),
            ("rebalance_frequency", "HIGHER_EXPECTED", "LOWER_EXPECTED"),
            ("whipsaw_mechanism", "DAILY_CROSSOVER", "MONTHLY_SMOOTHED_THRESHOLD"),
        )
        momentum_changed = (
            ("signal", "ENDPOINT_RETURN", "PRICE_VERSUS_AVERAGE_TREND"),
            ("lookback", "ONE_252_OBSERVATION_ANCHOR", "MEAN_OF_200_OBSERVATIONS"),
            ("threshold", "POINT_TO_POINT", "SMOOTHED_MEAN"),
            ("path_sensitivity", "ENDPOINTS_ONLY", "FULL_200_OBSERVATION_PATH"),
        )

        def comparison(
            prior: CryptoStrategySpecification,
            changed: tuple[tuple[str, str, str], ...],
        ) -> MaterialDifference:
            material = (
                prior.semantics_hash != candidate.semantics_hash
                and prior.strategy_family != candidate.strategy_family
                and bool(changed)
            )
            if not material:
                raise CryptoCampaignCandidateError(
                    "CRYPTO_CAMPAIGN_CANDIDATE_MATERIAL_DIFFERENCE_REQUIRED"
                )
            payload = (
                CRYPTO_CAMPAIGN_CANDIDATE_COMPARISON_V1,
                prior.specification_id,
                prior.strategy_family,
                candidate.specification_id,
                prior.semantics_hash,
                candidate.semantics_hash,
                changed,
                shared,
                material,
            )
            return MaterialDifference(
                prior.specification_id,
                prior.strategy_family,
                changed,
                shared,
                material,
                content_digest(payload),
            )

        if sma.strategy_family != BTC_USD_SMA_20_100_STRATEGY_FAMILY:
            raise CryptoCampaignCandidateError("CRYPTO_CAMPAIGN_CANDIDATE_SMA_PRIOR_INVALID")
        if monthly_252.strategy_family != BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY:
            raise CryptoCampaignCandidateError("CRYPTO_CAMPAIGN_CANDIDATE_MONTHLY_PRIOR_INVALID")
        against_sma = comparison(sma, sma_changed)
        against_monthly = comparison(monthly_252, momentum_changed)
        payload = (
            candidate.specification_id,
            candidate.semantics_hash,
            against_sma,
            against_monthly,
        )
        return cls(*payload, content_digest(payload))


@dataclass(frozen=True, slots=True)
class CandidateWindow:
    label: str
    snapshot_id: str
    validation_start: datetime
    validation_end: datetime


FIXED_WINDOWS = (
    CandidateWindow(
        "EARLIER_DISJOINT",
        EARLIER_SNAPSHOT_ID,
        EARLIER_VALIDATION_START,
        EARLIER_VALIDATION_END,
    ),
    CandidateWindow("LATER", LATER_SNAPSHOT_ID, LATER_VALIDATION_START, LATER_VALIDATION_END),
)


@dataclass(frozen=True, slots=True)
class CandidateProtocol:
    protocol_id: str
    campaign_id: str
    candidate_id: str
    hypothesis_version_id: str
    strategy_specification_id: str
    strategy_semantics_hash: str
    windows: tuple[CandidateWindow, CandidateWindow]
    evaluator_identity: str
    feature_semantics: str
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
        hypothesis: CandidateHypothesisSpecification,
        strategy: CryptoStrategySpecification,
        code_revision: str,
        created_at: datetime,
    ) -> CandidateProtocol:
        if strategy.strategy_family != BTC_USD_MONTHLY_200_PRICE_TREND_FAMILY:
            raise CryptoCampaignCandidateError("CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_INVALID")
        payload = (
            CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_V1,
            _identity(campaign_id, "CRYPTO_CAMPAIGN_ID_INVALID"),
            _identity(candidate_id, "CRYPTO_CAMPAIGN_CANDIDATE_ID_INVALID"),
            hypothesis.hypothesis_version_id,
            strategy.specification_id,
            strategy.semantics_hash,
            FIXED_WINDOWS,
            CRYPTO_CAMPAIGN_CANDIDATE_EVALUATOR_V1,
            CRYPTO_CAMPAIGN_CANDIDATE_FEATURE_V1,
            "PRIOR_MONTH_END_INFORMATION_THEN_NEXT_AVAILABLE_ELIGIBLE_DAILY_OPEN",
            CRYPTO_RESEARCH_COST_MODEL_V1,
            (
                "CASH",
                "BUY_AND_HOLD",
                "RETAINED_REJECTED_SMA_20_100_SAME_WINDOW",
                "RETAINED_REJECTED_MONTHLY_252_SAME_WINDOW",
            ),
            CRYPTO_CAMPAIGN_CANDIDATE_POLICY_V1,
            CRYPTO_CAMPAIGN_CANDIDATE_POLICY_V1,
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
class Monthly200Decision:
    decision_month: datetime
    information_through: datetime | None
    information_available_at: datetime | None
    observation_start: datetime | None
    observation_count: int
    completed_close: Decimal | None
    arithmetic_mean: Decimal | None
    selected_state: str
    desired_long: bool
    execution_at: datetime | None
    execution_open: Decimal | None
    eligible: bool
    ineligibility_reason: str | None
    canonical_hash: str


def build_monthly_200_decisions(
    candles: tuple[CryptoDailyCandle, ...],
    *,
    validation_start: datetime,
    validation_end: datetime,
) -> tuple[Monthly200Decision, ...]:
    ordered = tuple(sorted(candles, key=lambda item: item.bucket_start))
    if len({item.bucket_start for item in ordered}) != len(ordered):
        raise CryptoCampaignCandidateError("CRYPTO_CAMPAIGN_CANDIDATE_DUPLICATE_OBSERVATION")
    decisions: list[Monthly200Decision] = []
    for month_start in _month_starts(validation_start, validation_end):
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
        required = prior[-LOOKBACK_OBSERVATIONS:] if len(prior) >= LOOKBACK_OBSERVATIONS else prior
        reason: str | None = None
        if recent is None or recent.bucket_start != expected_month_end:
            reason = "FINAL_PRECEDING_MONTH_OBSERVATION_MISSING"
        elif len(required) < LOOKBACK_OBSERVATIONS:
            reason = "FEWER_THAN_200_ELIGIBLE_COMPLETED_OBSERVATIONS"
        elif required[-1].bucket_start - required[0].bucket_start != timedelta(
            days=LOOKBACK_OBSERVATIONS - 1
        ):
            reason = "REQUIRED_200_OBSERVATION_SEQUENCE_HAS_GAP"
        elif execution is None:
            reason = "NEXT_ELIGIBLE_EXECUTION_OPEN_UNAVAILABLE"
        info_available = recent.bucket_start + timedelta(days=1) if recent is not None else None
        if (
            reason is None
            and execution is not None
            and info_available is not None
            and info_available > execution.bucket_start
        ):
            reason = "INFORMATION_NOT_AVAILABLE_BEFORE_EXECUTION"
        exact_mean = (
            sum((item.close for item in required), Decimal(0)) / Decimal(LOOKBACK_OBSERVATIONS)
            if reason is None
            else None
        )
        bounded_mean = _metric(exact_mean) if exact_mean is not None else None
        desired = recent is not None and exact_mean is not None and recent.close > exact_mean
        payload = (
            CRYPTO_CAMPAIGN_CANDIDATE_FEATURE_V1,
            month_start,
            recent.bucket_start if recent else None,
            info_available,
            required[0].bucket_start if len(required) == LOOKBACK_OBSERVATIONS else None,
            len(required),
            recent.close if recent else None,
            bounded_mean,
            "LONG" if desired else "CASH",
            desired,
            execution.bucket_start if execution else None,
            execution.open if execution else None,
            reason is None,
            reason,
        )
        decisions.append(Monthly200Decision(*payload[1:], content_digest(payload)))
    return tuple(decisions)


@dataclass(frozen=True, slots=True)
class CandidateHolding:
    bucket_start: datetime
    portfolio: CandidatePortfolio
    scenario: CostScenarioName
    state: str
    cash: Decimal
    units: Decimal
    marked_value: Decimal
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    portfolio: CandidatePortfolio
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
    turnover: Decimal
    exposure_percentage: Decimal
    days_long: int
    days_in_cash: int
    monthly_decisions: int
    eligible_decisions: int
    ineligible_decisions: int
    modeled_fees: Decimal
    modeled_slippage: Decimal
    total_modeled_cost: Decimal
    missing_or_excluded_observations: int
    complete: bool
    reproducibility_status: ReproducibilityStatus
    holdings: tuple[CandidateHolding, ...]
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


def simulate_candidate_portfolio(
    *,
    portfolio: CandidatePortfolio,
    scenario: CostScenarioName,
    candles: tuple[CryptoDailyCandle, ...],
    decisions: tuple[Monthly200Decision, ...],
    validation_start: datetime,
    validation_end: datetime,
    gross_return: Decimal | None = None,
) -> CandidateMetrics:
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
    holdings: list[CandidateHolding] = []
    for candle in validation:
        if portfolio is CandidatePortfolio.CASH:
            desired = False
        elif portfolio is CandidatePortfolio.BUY_AND_HOLD:
            desired = True
        else:
            decision = decision_map.get(candle.bucket_start)
            desired = units > 0 if decision is None else decision.desired_long and decision.eligible
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
        holdings.append(CandidateHolding(*holding_payload, content_digest(holding_payload)))
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
        holdings[-1] = CandidateHolding(*liquidation_payload, content_digest(liquidation_payload))
    ending = _metric(cash)
    net = _metric(ending / STARTING_RESEARCH_NOTIONAL - Decimal(1))
    gross = net if gross_return is None else _metric(gross_return)
    ineligible = sum(not item.eligible for item in decisions)
    eligible = len(decisions) - ineligible
    complete = missing == 0 and ineligible == 0
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
        _metric(turnover_notional / STARTING_RESEARCH_NOTIONAL),
        _metric(Decimal(days_long) / Decimal(len(validation)) * Decimal(100))
        if validation
        else Decimal(0),
        days_long,
        len(validation) - days_long,
        len(decisions) if portfolio is CandidatePortfolio.MONTHLY_200_PRICE_TREND else 0,
        eligible if portfolio is CandidatePortfolio.MONTHLY_200_PRICE_TREND else 0,
        ineligible if portfolio is CandidatePortfolio.MONTHLY_200_PRICE_TREND else 0,
        _metric(fees),
        _metric(slippage),
        _metric(fees + slippage),
        max(missing, 0),
        complete,
        ReproducibilityStatus.REPRODUCIBLE if complete else ReproducibilityStatus.NOT_REPRODUCIBLE,
        tuple(holdings),
    )
    return CandidateMetrics(*metric_payload, content_digest(metric_payload))


def calculate_candidate_metrics(
    *,
    candles: tuple[CryptoDailyCandle, ...],
    decisions: tuple[Monthly200Decision, ...],
    validation_start: datetime,
    validation_end: datetime,
) -> tuple[CandidateMetrics, ...]:
    result: list[CandidateMetrics] = []
    for portfolio in CandidatePortfolio:
        zero = simulate_candidate_portfolio(
            portfolio=portfolio,
            scenario=CostScenarioName.DIAGNOSTIC_ZERO_COST,
            candles=candles,
            decisions=decisions,
            validation_start=validation_start,
            validation_end=validation_end,
        )
        conservative = simulate_candidate_portfolio(
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


def candidate_metric(
    metrics: tuple[CandidateMetrics, ...],
    portfolio: CandidatePortfolio,
    scenario: CostScenarioName = CostScenarioName.CONSERVATIVE_RESEARCH,
) -> CandidateMetrics:
    try:
        return next(
            item for item in metrics if item.portfolio is portfolio and item.scenario is scenario
        )
    except StopIteration as error:
        raise CryptoCampaignCandidateError("CRYPTO_CAMPAIGN_CANDIDATE_METRIC_MISSING") from error


@dataclass(frozen=True, slots=True)
class PriorStrategyComparison:
    strategy_family: str
    prior_run_id: str
    prior_result_hash: str
    prior_canonical_run_hash: str
    candidate_net_return_delta: Decimal
    candidate_drawdown_delta: Decimal
    candidate_turnover_delta: Decimal
    candidate_cost_delta: Decimal
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
        candidate: CandidateMetrics,
    ) -> PriorStrategyComparison:
        payload = (
            strategy_family,
            prior_run_id,
            prior_result_hash,
            prior_canonical_run_hash,
            _metric(candidate.net_return - prior_net_return),
            _metric(candidate.maximum_drawdown - prior_drawdown),
            _metric(candidate.turnover - prior_turnover),
            _metric(candidate.total_modeled_cost - prior_cost),
        )
        return cls(*payload, content_digest(payload))


@dataclass(frozen=True, slots=True)
class CandidateRun:
    run_id: str
    campaign_id: str
    candidate_id: str
    protocol_id: str
    hypothesis_version_id: str
    strategy_specification_id: str
    strategy_semantics_hash: str
    snapshot_id: str
    snapshot_manifest_hash: str
    snapshot_exposure_state: str
    validation_start: datetime
    validation_end: datetime
    code_revision: str
    deterministic_seed: int
    executed_at: datetime
    state: ExperimentState
    complete: bool
    reproducibility_status: ReproducibilityStatus
    evidence_class: EvidenceClass
    decisions: tuple[Monthly200Decision, ...]
    metrics: tuple[CandidateMetrics, ...]
    prior_comparisons: tuple[PriorStrategyComparison, PriorStrategyComparison]
    warnings: tuple[str, ...]
    disqualifying_warnings: tuple[str, ...]
    failure_reason: str | None
    configuration_hash: str
    result_hash: str
    canonical_hash: str
    authority: str = "NONE"


def candidate_window_passes(run: CandidateRun) -> bool:
    candidate = candidate_metric(run.metrics, CandidatePortfolio.MONTHLY_200_PRICE_TREND)
    baseline = candidate_metric(run.metrics, CandidatePortfolio.BUY_AND_HOLD)
    return (
        run.state is ExperimentState.COMPLETED
        and run.complete
        and run.reproducibility_status is ReproducibilityStatus.REPRODUCIBLE
        and run.evidence_class is EvidenceClass.HISTORICAL_PUBLIC
        and not run.disqualifying_warnings
        and candidate.net_return >= 0
        and candidate.maximum_drawdown < baseline.maximum_drawdown
        and candidate.days_long > 0
        and candidate.ineligible_decisions == 0
    )


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    outcome_id: str
    run_id: str
    decision: ExperimentOutcomeDecision
    reason: str
    passed_conditions: tuple[str, ...]
    failed_conditions: tuple[str, ...]
    decided_at: datetime
    actor: str
    canonical_hash: str
    paper_eligible: bool = False
    live_eligible: bool = False
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class CandidateDossier:
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
    percentage_windows_beating_cash: Decimal
    percentage_windows_reducing_drawdown: Decimal
    percentage_windows_improving_sma: Decimal
    percentage_windows_improving_monthly_252: Decimal
    consistency_status: str
    warnings: tuple[str, ...]
    holdout_accessed: bool
    independent_validation: bool
    membership_hash: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class CandidateNominationEvaluation:
    evaluation_id: str
    dossier_id: str
    campaign_id: str
    candidate_id: str
    conditions: tuple[tuple[str, bool], ...]
    passed_conditions: tuple[str, ...]
    failed_conditions: tuple[str, ...]
    eligible: bool
    candidate_state: CandidateState
    evaluated_at: datetime
    actor: str
    reason: str
    canonical_hash: str
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    opportunity_authority: str = "NONE"
    order_authority: str = "NONE"
    execution_authority: str = "NONE"
    financial_authority: str = "NONE"
    authority: str = "NONE"
