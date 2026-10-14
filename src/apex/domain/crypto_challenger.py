"""Pure monthly-momentum challenger and multi-window evaluation rules."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from apex.domain.crypto_dossier import (
    BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY,
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
    CryptoPerformanceMetrics,
    PortfolioName,
)
from apex.domain.research_evidence import (
    EvidenceClass,
    ExperimentOutcomeDecision,
    ExperimentState,
    ReproducibilityStatus,
    ResearchHypothesisVersion,
)
from apex.domain.values import content_digest

CRYPTO_MONTHLY_CHALLENGER_HYPOTHESIS_V1 = "CRYPTO_MONTHLY_CHALLENGER_HYPOTHESIS_V1"
CRYPTO_MONTHLY_MULTI_WINDOW_PROTOCOL_V1 = "CRYPTO_MONTHLY_MULTI_WINDOW_PROTOCOL_V1"
CRYPTO_MONTHLY_MOMENTUM_FEATURES_V1 = "BTC_USD_PRIOR_MONTH_END_ABSOLUTE_MOMENTUM_252_V1"
CRYPTO_MONTHLY_MOMENTUM_EXECUTOR_V1 = "BTC_USD_MONTHLY_ABSOLUTE_MOMENTUM_252_V1"
CRYPTO_MONTHLY_MULTI_WINDOW_POLICY_V1 = "CRYPTO_MONTHLY_MULTI_WINDOW_POLICY_V1"
CRYPTO_MONTHLY_CHALLENGER_RESULT_V1 = "CRYPTO_MONTHLY_CHALLENGER_RESULT_V1"
CRYPTO_MONTHLY_CHALLENGER_DOSSIER_V1 = "CRYPTO_MONTHLY_CHALLENGER_DOSSIER_V1"
CRYPTO_MONTHLY_CHALLENGER_CONCLUSION_V1 = "CRYPTO_MONTHLY_CHALLENGER_CONCLUSION_V1"
NO_LEADING_CHALLENGER = "NO_LEADING_CHALLENGER"
LEADING_RESEARCH_CHALLENGER_REQUIRES_FORWARD_VALIDATION = (
    "LEADING_RESEARCH_CHALLENGER_REQUIRES_FORWARD_VALIDATION"
)

EARLIER_SNAPSHOT_ID = "btc-usd-genuine-replication-snapshot-2022-07-01-2024-07-01-v1"
LATER_SNAPSHOT_ID = "btc-usd-genuine-snapshot-2024-07-01-2026-07-01-v1"
EARLIER_PRIOR_RUN_ID = "btc-usd-genuine-disjoint-replication-v1"
LATER_PRIOR_RUN_ID = "btc-usd-genuine-trend-validation-v1"
EARLIER_VALIDATION_START = datetime(2023, 7, 1, tzinfo=UTC)
EARLIER_VALIDATION_END = datetime(2024, 7, 1, tzinfo=UTC)
LATER_VALIDATION_START = datetime(2025, 7, 1, tzinfo=UTC)
LATER_VALIDATION_END = datetime(2026, 7, 1, tzinfo=UTC)
MOMENTUM_LOOKBACK_OBSERVATIONS = 252

CHANGED_SEMANTICS = (
    "strategy_family",
    "signal_definition",
    "decision_cadence",
    "indicator_inputs",
    "rebalance_timing",
    "expected_turnover",
)
PRESERVED_SEMANTICS = (
    "product",
    "data_granularity",
    "long_or_cash",
    "no_short",
    "no_leverage",
    "next_open_execution",
    "conservative_costs",
    "hypothetical_notional",
    "evidence_class",
    "authority_boundary",
)
AUTHORITY_WARNINGS = (
    "RETROSPECTIVE HISTORICAL PUBLIC EVIDENCE — NOT FORWARD OBSERVATION",
    "HYPOTHETICAL RESEARCH NOTIONAL — NOT ACCOUNT VALUE OR PROFIT",
    "NO PAPER, LIVE, CAPITAL, OPPORTUNITY, ALLOCATION, OR ORDER AUTHORITY",
)

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")


class CryptoChallengerError(ValueError):
    pass


class ChallengerPortfolio(StrEnum):
    MONTHLY_MOMENTUM = "MONTHLY_MOMENTUM"
    CASH = "CASH"
    BUY_AND_HOLD = "BUY_AND_HOLD"


class ChallengerConclusionType(StrEnum):
    RESEARCH_CANDIDATE = "RESEARCH_CANDIDATE"
    MIXED_EVIDENCE = "MIXED_EVIDENCE"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


def _identity(value: str, code: str) -> str:
    normalized = value.strip()
    if _IDENTITY.fullmatch(normalized) is None:
        raise CryptoChallengerError(code)
    return normalized


def _aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CryptoChallengerError(code)
    return value.astimezone(UTC)


def _metric(value: Decimal) -> Decimal:
    return value.quantize(RESEARCH_METRIC_QUANTUM)


def _month_starts(start: datetime, end: datetime) -> tuple[datetime, ...]:
    current = datetime(start.year, start.month, 1, tzinfo=UTC)
    result: list[datetime] = []
    while current < end:
        result.append(current)
        year = current.year + (1 if current.month == 12 else 0)
        month = 1 if current.month == 12 else current.month + 1
        current = datetime(year, month, 1, tzinfo=UTC)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class MonthlyMomentumHypothesisSpecification:
    hypothesis_version_id: str
    product_id: str
    granularity: str
    evidence_class: EvidenceClass
    snapshot_ids: tuple[str, str]
    validation_windows: tuple[tuple[datetime, datetime], tuple[datetime, datetime]]
    exact_rule: str
    execution_timing: str
    cost_model: str
    baselines: tuple[str, ...]
    success_criteria: tuple[str, ...]
    failure_criteria: tuple[str, ...]
    limitations: tuple[str, ...]
    canonical_hash: str
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    opportunity_authority: str = "NONE"
    order_authority: str = "NONE"
    authority: str = "NONE"

    @classmethod
    def fixed(cls, hypothesis: ResearchHypothesisVersion) -> MonthlyMomentumHypothesisSpecification:
        payload = (
            CRYPTO_MONTHLY_CHALLENGER_HYPOTHESIS_V1,
            hypothesis.hypothesis_version_id,
            COINBASE_PRODUCT,
            COINBASE_GRANULARITY,
            EvidenceClass.HISTORICAL_PUBLIC,
            (EARLIER_SNAPSHOT_ID, LATER_SNAPSHOT_ID),
            (
                (EARLIER_VALIDATION_START, EARLIER_VALIDATION_END),
                (LATER_VALIDATION_START, LATER_VALIDATION_END),
            ),
            (
                "At each calendar-month boundary, go long only when the final "
                "completed preceding-month close divided by the completed close "
                "252 accepted daily observations earlier minus one is strictly positive."
            ),
            "DECIDE_AFTER_PRECEDING_MONTH_CLOSE_EXECUTE_NEXT_AVAILABLE_DAILY_OPEN",
            CRYPTO_RESEARCH_COST_MODEL_V1,
            ("CASH", "BUY_AND_HOLD", "RETAINED_REJECTED_SMA_20_100"),
            (
                "EACH_WINDOW_CONSERVATIVE_NET_RETURN_AT_LEAST_ZERO",
                "EACH_WINDOW_DRAWDOWN_STRICTLY_BELOW_BUY_AND_HOLD",
                "BOTH_WINDOWS_COMPLETE_AND_REPRODUCIBLE",
                "AT_LEAST_ONE_ELIGIBLE_LONG_PERIOD_EACH_WINDOW",
                "AGGREGATE_COMPOUNDED_CONSERVATIVE_RETURN_POSITIVE",
            ),
            (
                "ANY_WINDOW_NEGATIVE_RETURN",
                "ANY_WINDOW_DRAWDOWN_NOT_BELOW_BUY_AND_HOLD",
                "INCOMPLETE_OR_NONREPRODUCIBLE_EVIDENCE",
                "LEAKAGE_OR_QUALITY_INVALIDATION",
                "NO_ELIGIBLE_LONG_PERIOD",
            ),
            (
                "RETROSPECTIVE_HISTORY_NOT_VINTAGE_POINT_IN_TIME",
                "TWO_PREDECLARED_WINDOWS_ONLY",
                "MODELED_COSTS_ARE_NOT_USER_FEE_TIERS",
                "REQUIRES_FORWARD_VINTAGE_OBSERVATION_BEFORE_PAPER_REVIEW",
                "AUTHORITY_NONE",
            ),
        )
        return cls(*payload[1:], content_digest(payload))


@dataclass(frozen=True, slots=True)
class StrategySemanticComparison:
    prior_specification_id: str
    challenger_specification_id: str
    changed: tuple[tuple[str, str, str], ...]
    preserved: tuple[tuple[str, str], ...]
    material_change: bool
    rationale: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        prior: CryptoStrategySpecification,
        challenger: CryptoStrategySpecification,
    ) -> StrategySemanticComparison:
        prior_values = dict(prior.semantics)
        challenger_values = dict(challenger.semantics)
        required = {
            "strategy_family",
            "entry_rule",
            "decision_cadence",
            "execution_timing_rule",
        }
        if not required.issubset(challenger_values):
            raise CryptoChallengerError("CRYPTO_CHALLENGER_MATERIAL_CHANGE_REQUIRED")
        changed = (
            (
                "strategy_family",
                str(prior_values["strategy_family"]),
                str(challenger_values["strategy_family"]),
            ),
            (
                "signal_definition",
                str(prior_values["entry_rule"]),
                str(challenger_values["entry_rule"]),
            ),
            ("decision_cadence", "DAILY", str(challenger_values["decision_cadence"])),
            (
                "indicator_inputs",
                "20_AND_100_PRIOR_DAILY_CLOSES",
                "PRIOR_MONTH_END_AND_CLOSE_252_ACCEPTED_OBSERVATIONS_EARLIER",
            ),
            (
                "rebalance_timing",
                str(prior_values["execution_timing_rule"]),
                "MONTHLY_" + str(challenger_values["execution_timing_rule"]),
            ),
            ("expected_turnover", "DAILY_DECISION_POTENTIAL", "MONTHLY_DECISION_POTENTIAL"),
        )
        preserved = tuple(
            (name, value)
            for name, value in (
                ("product", COINBASE_PRODUCT),
                ("data_granularity", COINBASE_GRANULARITY),
                ("long_or_cash", "TRUE"),
                ("no_short", "TRUE"),
                ("no_leverage", "TRUE"),
                ("next_open_execution", "TRUE"),
                ("conservative_costs", "60_FEE_PLUS_10_SLIPPAGE_BPS_PER_SIDE"),
                ("hypothetical_notional", "10000"),
                ("evidence_class", EvidenceClass.HISTORICAL_PUBLIC.value),
                ("authority_boundary", "NONE"),
            )
        )
        material = (
            prior.semantics_hash != challenger.semantics_hash
            and tuple(item[0] for item in changed) == CHANGED_SEMANTICS
            and tuple(item[0] for item in preserved) == PRESERVED_SEMANTICS
            and prior.strategy_family != challenger.strategy_family
        )
        if not material:
            raise CryptoChallengerError("CRYPTO_CHALLENGER_MATERIAL_CHANGE_REQUIRED")
        payload = (
            prior.specification_id,
            challenger.specification_id,
            changed,
            preserved,
            material,
            challenger.change_rationale,
        )
        return cls(*payload, content_digest(payload))


@dataclass(frozen=True, slots=True)
class ChallengerWindow:
    label: str
    snapshot_id: str
    validation_start: datetime
    validation_end: datetime
    prior_run_id: str


FIXED_WINDOWS = (
    ChallengerWindow(
        "EARLIER_DISJOINT",
        EARLIER_SNAPSHOT_ID,
        EARLIER_VALIDATION_START,
        EARLIER_VALIDATION_END,
        EARLIER_PRIOR_RUN_ID,
    ),
    ChallengerWindow(
        "LATER",
        LATER_SNAPSHOT_ID,
        LATER_VALIDATION_START,
        LATER_VALIDATION_END,
        LATER_PRIOR_RUN_ID,
    ),
)


@dataclass(frozen=True, slots=True)
class CryptoChallengerProtocol:
    protocol_id: str
    hypothesis_version_id: str
    strategy_specification_id: str
    strategy_semantics_hash: str
    windows: tuple[ChallengerWindow, ChallengerWindow]
    evaluator_identity: str
    feature_semantics: str
    execution_semantics: str
    cost_model: str
    baseline_definitions: tuple[str, ...]
    per_window_policy: str
    aggregate_policy: str
    deterministic_seed: int
    code_revision: str
    protocol_hash: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def fixed(
        cls,
        *,
        protocol_id: str,
        hypothesis: MonthlyMomentumHypothesisSpecification,
        strategy: CryptoStrategySpecification,
        code_revision: str,
        created_at: datetime,
    ) -> CryptoChallengerProtocol:
        if strategy.strategy_family != BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY:
            raise CryptoChallengerError("CRYPTO_CHALLENGER_PROTOCOL_STRATEGY_INVALID")
        payload = (
            CRYPTO_MONTHLY_MULTI_WINDOW_PROTOCOL_V1,
            hypothesis.hypothesis_version_id,
            strategy.specification_id,
            strategy.semantics_hash,
            FIXED_WINDOWS,
            CRYPTO_MONTHLY_MOMENTUM_EXECUTOR_V1,
            CRYPTO_MONTHLY_MOMENTUM_FEATURES_V1,
            "PRIOR_MONTH_END_INFORMATION_THEN_NEXT_AVAILABLE_DAILY_OPEN",
            CRYPTO_RESEARCH_COST_MODEL_V1,
            ("CASH", "BUY_AND_HOLD", "RETAINED_REJECTED_SMA_20_100"),
            CRYPTO_MONTHLY_MULTI_WINDOW_POLICY_V1,
            CRYPTO_MONTHLY_MULTI_WINDOW_POLICY_V1,
            0,
            _identity(code_revision, "CRYPTO_CHALLENGER_CODE_REVISION_INVALID"),
        )
        protocol_hash = content_digest(payload)
        identity = _identity(protocol_id, "CRYPTO_CHALLENGER_PROTOCOL_ID_INVALID")
        created = _aware(created_at, "CRYPTO_CHALLENGER_PROTOCOL_TIME_INVALID")
        return cls(
            identity,
            hypothesis.hypothesis_version_id,
            strategy.specification_id,
            strategy.semantics_hash,
            FIXED_WINDOWS,
            CRYPTO_MONTHLY_MOMENTUM_EXECUTOR_V1,
            CRYPTO_MONTHLY_MOMENTUM_FEATURES_V1,
            payload[7],
            CRYPTO_RESEARCH_COST_MODEL_V1,
            payload[9],
            CRYPTO_MONTHLY_MULTI_WINDOW_POLICY_V1,
            CRYPTO_MONTHLY_MULTI_WINDOW_POLICY_V1,
            0,
            payload[13],
            protocol_hash,
            created,
            content_digest((identity, protocol_hash, created)),
        )


@dataclass(frozen=True, slots=True)
class MonthlyMomentumDecision:
    decision_month: datetime
    information_through: datetime | None
    information_available_at: datetime | None
    trailing_bucket_start: datetime | None
    execution_at: datetime | None
    recent_close: Decimal | None
    trailing_close: Decimal | None
    momentum: Decimal | None
    desired_long: bool
    eligible: bool
    failure_reason: str | None
    canonical_hash: str


def build_monthly_momentum_decisions(
    candles: tuple[CryptoDailyCandle, ...],
    *,
    validation_start: datetime,
    validation_end: datetime,
) -> tuple[MonthlyMomentumDecision, ...]:
    ordered = tuple(sorted(candles, key=lambda item: item.bucket_start))
    if len({item.bucket_start for item in ordered}) != len(ordered):
        raise CryptoChallengerError("CRYPTO_CHALLENGER_DUPLICATE_OBSERVATION")
    decisions: list[MonthlyMomentumDecision] = []
    for month_start in _month_starts(validation_start, validation_end):
        year = month_start.year if month_start.month > 1 else month_start.year - 1
        month = month_start.month - 1 if month_start.month > 1 else 12
        prior_month_end = datetime(year, month, calendar.monthrange(year, month)[1], tzinfo=UTC)
        execution = next(
            (
                item
                for item in ordered
                if month_start <= item.bucket_start
                and item.bucket_start
                < (
                    datetime(month_start.year + 1, 1, 1, tzinfo=UTC)
                    if month_start.month == 12
                    else datetime(month_start.year, month_start.month + 1, 1, tzinfo=UTC)
                )
            ),
            None,
        )
        prior = tuple(item for item in ordered if item.bucket_start < month_start)
        reason: str | None = None
        recent = prior[-1] if prior else None
        trailing = prior[-(MOMENTUM_LOOKBACK_OBSERVATIONS + 1)] if len(prior) > 252 else None
        if execution is None:
            reason = "NEXT_AVAILABLE_EXECUTION_CANDLE_MISSING"
        elif recent is None or recent.bucket_start != prior_month_end:
            reason = "FINAL_PRECEDING_MONTH_OBSERVATION_MISSING"
        elif trailing is None:
            reason = "TRAILING_252_OBSERVATION_MISSING"
        elif recent.bucket_start - trailing.bucket_start != timedelta(
            days=MOMENTUM_LOOKBACK_OBSERVATIONS
        ):
            reason = "TRAILING_252_OBSERVATION_GAP"
        info_available = recent.bucket_start + timedelta(days=1) if recent is not None else None
        if (
            reason is None
            and execution is not None
            and info_available is not None
            and info_available > execution.bucket_start
        ):
            reason = "INFORMATION_NOT_AVAILABLE_BEFORE_EXECUTION"
        momentum = (
            recent.close / trailing.close - Decimal(1)
            if reason is None and recent is not None and trailing is not None
            else None
        )
        bounded_momentum = _metric(momentum) if momentum is not None else None
        payload = (
            CRYPTO_MONTHLY_MOMENTUM_FEATURES_V1,
            month_start,
            recent.bucket_start if recent else None,
            info_available,
            trailing.bucket_start if trailing else None,
            execution.bucket_start if execution else None,
            recent.close if recent else None,
            trailing.close if trailing else None,
            bounded_momentum,
            bounded_momentum is not None and bounded_momentum > 0,
            reason is None,
            reason,
        )
        decisions.append(
            MonthlyMomentumDecision(
                month_start,
                payload[2],
                info_available,
                payload[4],
                payload[5],
                payload[6],
                payload[7],
                bounded_momentum,
                bool(payload[9]),
                reason is None,
                reason,
                content_digest(payload),
            )
        )
    return tuple(decisions)


@dataclass(frozen=True, slots=True)
class ChallengerMetrics:
    portfolio: ChallengerPortfolio
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
    rebalance_decisions: int
    failed_or_skipped_rebalances: int
    modeled_fees: Decimal
    modeled_slippage: Decimal
    total_modeled_cost: Decimal
    missing_or_excluded_observations: int
    complete: bool
    reproducibility_status: ReproducibilityStatus
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


def simulate_challenger_portfolio(
    *,
    portfolio: ChallengerPortfolio,
    scenario: CostScenarioName,
    candles: tuple[CryptoDailyCandle, ...],
    decisions: tuple[MonthlyMomentumDecision, ...],
    validation_start: datetime,
    validation_end: datetime,
    gross_return: Decimal | None = None,
) -> ChallengerMetrics:
    validation = tuple(
        item
        for item in sorted(candles, key=lambda item: item.bucket_start)
        if validation_start <= item.bucket_start < validation_end
    )
    expected = (validation_end - validation_start).days
    missing = expected - len(validation)
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
    for candle in validation:
        if portfolio is ChallengerPortfolio.CASH:
            desired = False
        elif portfolio is ChallengerPortfolio.BUY_AND_HOLD:
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
        values.append(cash + units * candle.close)
    if units > 0 and validation:
        final_close = validation[-1].close
        execution_price = final_close * (Decimal(1) - slippage_rate)
        proceeds = units * execution_price
        fees += proceeds * fee_rate
        slippage += units * final_close * slippage_rate
        turnover_notional += units * final_close
        cash = proceeds - proceeds * fee_rate
        exits += 1
        values[-1] = cash
    ending = _metric(cash)
    net = _metric(ending / STARTING_RESEARCH_NOTIONAL - Decimal(1))
    gross = net if gross_return is None else _metric(gross_return)
    failed = sum(not item.eligible for item in decisions)
    complete = missing == 0 and failed == 0
    payload = (
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
        len(decisions) if portfolio is ChallengerPortfolio.MONTHLY_MOMENTUM else 0,
        failed if portfolio is ChallengerPortfolio.MONTHLY_MOMENTUM else 0,
        _metric(fees),
        _metric(slippage),
        _metric(fees + slippage),
        max(missing, 0),
        complete,
        ReproducibilityStatus.REPRODUCIBLE if complete else ReproducibilityStatus.NOT_REPRODUCIBLE,
    )
    return ChallengerMetrics(*payload, content_digest(payload))


def calculate_challenger_metrics(
    *,
    candles: tuple[CryptoDailyCandle, ...],
    decisions: tuple[MonthlyMomentumDecision, ...],
    validation_start: datetime,
    validation_end: datetime,
) -> tuple[ChallengerMetrics, ...]:
    result: list[ChallengerMetrics] = []
    for portfolio in ChallengerPortfolio:
        zero = simulate_challenger_portfolio(
            portfolio=portfolio,
            scenario=CostScenarioName.DIAGNOSTIC_ZERO_COST,
            candles=candles,
            decisions=decisions,
            validation_start=validation_start,
            validation_end=validation_end,
        )
        conservative = simulate_challenger_portfolio(
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


@dataclass(frozen=True, slots=True)
class RetainedPriorComparison:
    prior_run_id: str
    prior_result_hash: str
    prior_canonical_run_hash: str
    challenger_net_return_delta: Decimal
    challenger_drawdown_delta: Decimal
    challenger_turnover_delta: Decimal
    challenger_cost_delta: Decimal
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        prior_run_id: str,
        prior_result_hash: str,
        prior_canonical_run_hash: str,
        prior_metric: CryptoPerformanceMetrics,
        challenger_metric: ChallengerMetrics,
    ) -> RetainedPriorComparison:
        if (
            prior_metric.portfolio is not PortfolioName.TREND_RULE
            or prior_metric.scenario is not CostScenarioName.CONSERVATIVE_RESEARCH
        ):
            raise CryptoChallengerError("CRYPTO_CHALLENGER_PRIOR_METRIC_INVALID")
        payload = (
            prior_run_id,
            prior_result_hash,
            prior_canonical_run_hash,
            _metric(challenger_metric.net_return - prior_metric.net_return),
            _metric(challenger_metric.maximum_drawdown - prior_metric.maximum_drawdown),
            _metric(challenger_metric.turnover - prior_metric.total_turnover),
            _metric(challenger_metric.total_modeled_cost - prior_metric.cost_paid),
        )
        return cls(*payload, content_digest(payload))


@dataclass(frozen=True, slots=True)
class CryptoChallengerRun:
    run_id: str
    protocol_id: str
    hypothesis_version_id: str
    strategy_specification_id: str
    strategy_semantics_hash: str
    snapshot_id: str
    snapshot_manifest_hash: str
    validation_start: datetime
    validation_end: datetime
    code_revision: str
    deterministic_seed: int
    executed_at: datetime
    state: ExperimentState
    complete: bool
    reproducibility_status: ReproducibilityStatus
    evidence_class: EvidenceClass
    decisions: tuple[MonthlyMomentumDecision, ...]
    metrics: tuple[ChallengerMetrics, ...]
    prior_comparison: RetainedPriorComparison
    warnings: tuple[str, ...]
    failure_reason: str | None
    configuration_hash: str
    result_hash: str
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class CryptoChallengerOutcome:
    outcome_id: str
    run_id: str
    decision: ExperimentOutcomeDecision
    reason: str
    decided_at: datetime
    actor: str
    canonical_hash: str
    paper_eligible: bool = False
    live_eligible: bool = False
    authority: str = "NONE"


def conservative_challenger_metric(
    metrics: tuple[ChallengerMetrics, ...], portfolio: ChallengerPortfolio
) -> ChallengerMetrics:
    try:
        return next(
            value
            for value in metrics
            if value.portfolio is portfolio
            and value.scenario is CostScenarioName.CONSERVATIVE_RESEARCH
        )
    except StopIteration as error:
        raise CryptoChallengerError("CRYPTO_CHALLENGER_CONSERVATIVE_METRIC_MISSING") from error


def challenger_window_passes(run: CryptoChallengerRun) -> bool:
    challenger = conservative_challenger_metric(run.metrics, ChallengerPortfolio.MONTHLY_MOMENTUM)
    baseline = conservative_challenger_metric(run.metrics, ChallengerPortfolio.BUY_AND_HOLD)
    return (
        run.state is ExperimentState.COMPLETED
        and run.complete
        and run.reproducibility_status is ReproducibilityStatus.REPRODUCIBLE
        and run.evidence_class is EvidenceClass.HISTORICAL_PUBLIC
        and challenger.net_return >= 0
        and challenger.maximum_drawdown < baseline.maximum_drawdown
        and challenger.days_long > 0
        and challenger.failed_or_skipped_rebalances == 0
    )


@dataclass(frozen=True, slots=True)
class CryptoChallengerDossier:
    dossier_id: str
    hypothesis_version_id: str
    strategy_specification_id: str
    strategy_semantics_hash: str
    semantic_comparison_hash: str
    material_change_rationale: str
    protocol_id: str
    protocol_hash: str
    window_run_ids: tuple[str, str]
    window_result_hashes: tuple[str, str]
    window_outcome_ids: tuple[str, str]
    prior_run_ids: tuple[str, str]
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
    total_modeled_costs: Decimal
    total_entries: int
    total_exits: int
    percentage_windows_beating_cash: Decimal
    percentage_windows_reducing_drawdown: Decimal
    consistency_status: str
    warnings: tuple[str, ...]
    membership_hash: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class CryptoChallengerConclusion:
    conclusion_id: str
    dossier_id: str
    conclusion: ChallengerConclusionType
    reason: str
    leading_challenger_status: str
    approved_champion_status: str
    decided_at: datetime
    actor: str
    canonical_hash: str
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    opportunity_authority: str = "NONE"
    order_authority: str = "NONE"
    authority: str = "NONE"
