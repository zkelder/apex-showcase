"""Canonical Securities baselines, research portfolios, and conformance rules.

This module is deliberately research-only.  It consumes retained point-in-time
Securities evidence and produces immutable simulator evidence.  It owns no
Campaign mutation, PAPER trial, Order, Position, journal, provider, or broker
authority.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from enum import StrEnum

from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesCorporateActionEvidence,
    SecuritiesCorporateActionType,
    SecuritiesSignalDirection,
)
from apex.domain.values import (
    DecimalInput,
    canonical_timestamp,
    content_digest,
    deterministic_id,
    parse_decimal,
)

CANONICAL_BASELINE_WARNING = "CANONICAL BASELINE — TRANSPARENT RESEARCH SPECIFICATION"
BASELINE_RESEARCH_WARNING = "BASELINES ARE RESEARCH BENCHMARKS, NOT PERFORMANCE PROMISES"
EXTERNAL_SIMULATOR_WARNING = "EXTERNAL SIMULATOR OUTPUT IS EVIDENCE, NOT APEX AUTHORITY"
MATERIALITY_POLICY_ID = "SECURITIES_CONFORMANCE_MATERIALITY_V1"


class SecuritiesBaselineError(ValueError):
    """Canonical baseline or simulator evidence violated a research invariant."""


class CanonicalBaselineFamily(StrEnum):
    CASH = "CASH"
    BUY_AND_HOLD = "BUY_AND_HOLD"
    EQUAL_WEIGHT_REBALANCE = "EQUAL_WEIGHT_REBALANCE"
    TIME_SERIES_MOMENTUM = "TIME_SERIES_MOMENTUM"
    CROSS_SECTIONAL_MOMENTUM = "CROSS_SECTIONAL_MOMENTUM"
    SHORT_HORIZON_MEAN_REVERSION = "SHORT_HORIZON_MEAN_REVERSION"
    PAIRS_DISTANCE_MEAN_REVERSION = "PAIRS_DISTANCE_MEAN_REVERSION"
    VOLATILITY_TARGETED_LONG_ONLY = "VOLATILITY_TARGETED_LONG_ONLY"


class CanonicalBaselineState(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    RETIRED = "RETIRED"


class PaperCompatibilityState(StrEnum):
    COMPATIBLE_LONG_ONLY = "COMPATIBLE_LONG_ONLY"
    RESEARCH_ONLY = "RESEARCH_ONLY"


class SecuritiesBenchmarkIdentity(StrEnum):
    CASH = "CASH"
    BUY_AND_HOLD_UNIVERSE = "BUY_AND_HOLD_UNIVERSE"
    EQUAL_WEIGHT_UNIVERSE = "EQUAL_WEIGHT_UNIVERSE"
    BROAD_MARKET_INSTRUMENT = "BROAD_MARKET_INSTRUMENT"
    STRATEGY_LAB_RESEARCH_CHAMPION = "STRATEGY_LAB_RESEARCH_CHAMPION"


class SecuritiesSimulatorIdentity(StrEnum):
    APEX_RESEARCH = "APEX_RESEARCH"
    APEX_PAPER = "APEX_PAPER"
    VECTORBT = "VECTORBT"
    LEAN = "LEAN"
    DETERMINISTIC_FIXTURE = "DETERMINISTIC_FIXTURE"


class SecuritiesSimulationMode(StrEnum):
    RESEARCH_SIGNAL = "RESEARCH_SIGNAL"
    RESEARCH_PORTFOLIO = "RESEARCH_PORTFOLIO"
    PAPER_EXECUTION = "PAPER_EXECUTION"


class SecuritiesSimulationState(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"


class MultiSimulatorConformanceClassification(StrEnum):
    CONFORMANT = "CONFORMANT"
    EXPLAINED_DIFFERENCE = "EXPLAINED_DIFFERENCE"
    MATERIAL_MISMATCH = "MATERIAL_MISMATCH"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    INSUFFICIENT_COMPARABLE_OUTPUT = "INSUFFICIENT_COMPARABLE_OUTPUT"
    INCOMPARABLE_CONFIGURATION = "INCOMPARABLE_CONFIGURATION"


SUPPORTED_EXPLANATION_CODES = frozenset(
    {
        "EXECUTION_TIMING_POLICY_DIFFERENCE",
        "ROUNDING_POLICY_DIFFERENCE",
        "CORPORATE_ACTION_TIMING_DIFFERENCE",
        "DIVIDEND_CASH_TIMING_DIFFERENCE",
        "MISSING_EXTERNAL_OUTPUT",
        "EXTERNAL_ENGINE_UNAVAILABLE",
        "UNSUPPORTED_BASELINE",
        "DATASET_SEMANTIC_DIFFERENCE",
        "EXPECTED_RESEARCH_VS_PAPER_DIFFERENCE",
    }
)


@dataclass(frozen=True, slots=True)
class CanonicalSecuritiesBaseline:
    baseline_id: str
    family: CanonicalBaselineFamily
    specification_version: str
    display_name: str
    research_description: str
    source_reference_identity: str
    exact_feature_rules: tuple[str, ...]
    exact_decision_rules: tuple[str, ...]
    universe_requirements: tuple[str, ...]
    required_interval: SecuritiesBarInterval
    required_adjustment_state: SecuritiesAdjustmentState
    rebalance_policy: str
    holding_policy: str
    analytical_short_behavior: str
    paper_compatibility_state: PaperCompatibilityState
    benchmark_identity: SecuritiesBenchmarkIdentity
    state: CanonicalBaselineState
    canonical_payload: Mapping[str, object]
    canonical_hash: str
    created_at: datetime
    superseded_at: datetime | None

    @classmethod
    def create(
        cls,
        *,
        family: CanonicalBaselineFamily,
        display_name: str,
        description: str,
        feature_rules: tuple[str, ...],
        decision_rules: tuple[str, ...],
        universe_requirements: tuple[str, ...],
        rebalance_policy: str,
        holding_policy: str,
        analytical_short_behavior: str = "NONE",
        paper_compatibility_state: PaperCompatibilityState = (
            PaperCompatibilityState.COMPATIBLE_LONG_ONLY
        ),
        benchmark_identity: SecuritiesBenchmarkIdentity = (
            SecuritiesBenchmarkIdentity.EQUAL_WEIGHT_UNIVERSE
        ),
        created_at: datetime,
    ) -> CanonicalSecuritiesBaseline:
        _aware(created_at, "baseline creation")
        if not display_name or not description or not feature_rules or not decision_rules:
            raise SecuritiesBaselineError("baseline rules and descriptions are required")
        payload: dict[str, object] = {
            "family": family,
            "specification_version": "1",
            "display_name": display_name,
            "research_description": description,
            "source_reference_identity": "APEX_TRANSPARENT_SPECIFICATION_V1",
            "exact_feature_rules": feature_rules,
            "exact_decision_rules": decision_rules,
            "universe_requirements": universe_requirements,
            "required_interval": SecuritiesBarInterval.ONE_DAY,
            "required_adjustment_state": SecuritiesAdjustmentState.TOTAL_RETURN_ADJUSTED,
            "rebalance_policy": rebalance_policy,
            "holding_policy": holding_policy,
            "analytical_short_behavior": analytical_short_behavior,
            "paper_compatibility_state": paper_compatibility_state,
            "benchmark_identity": benchmark_identity,
        }
        canonical_hash = content_digest(payload)
        return cls(
            baseline_id=deterministic_id("canonical_securities_baseline", payload),
            family=family,
            specification_version="1",
            display_name=display_name,
            research_description=description,
            source_reference_identity="APEX_TRANSPARENT_SPECIFICATION_V1",
            exact_feature_rules=feature_rules,
            exact_decision_rules=decision_rules,
            universe_requirements=universe_requirements,
            required_interval=SecuritiesBarInterval.ONE_DAY,
            required_adjustment_state=SecuritiesAdjustmentState.TOTAL_RETURN_ADJUSTED,
            rebalance_policy=rebalance_policy,
            holding_policy=holding_policy,
            analytical_short_behavior=analytical_short_behavior,
            paper_compatibility_state=paper_compatibility_state,
            benchmark_identity=benchmark_identity,
            state=CanonicalBaselineState.ACTIVE,
            canonical_payload=payload,
            canonical_hash=canonical_hash,
            created_at=created_at.astimezone(UTC),
            superseded_at=None,
        )


@dataclass(frozen=True, slots=True)
class SecuritiesConformanceMaterialityPolicy:
    policy_id: str
    score_tolerance: Decimal
    target_weight_tolerance: Decimal
    price_tolerance: Decimal
    quantity_tolerance: Decimal
    cash_tolerance: Decimal
    pnl_tolerance: Decimal
    return_tolerance: Decimal
    drawdown_tolerance: Decimal
    timestamp_tolerance_seconds: int
    canonical_hash: str

    @classmethod
    def v1(cls) -> SecuritiesConformanceMaterialityPolicy:
        score_tolerance = Decimal("0.00000001")
        target_weight_tolerance = Decimal("0.00000001")
        price_tolerance = Decimal("0.00000001")
        quantity_tolerance = Decimal("0")
        cash_tolerance = Decimal("0.01")
        pnl_tolerance = Decimal("0.01")
        return_tolerance = Decimal("0.00000001")
        drawdown_tolerance = Decimal("0.00000001")
        timestamp_tolerance_seconds = 0
        payload: dict[str, object] = {
            "policy_id": MATERIALITY_POLICY_ID,
            "score_tolerance": score_tolerance,
            "target_weight_tolerance": target_weight_tolerance,
            "price_tolerance": price_tolerance,
            "quantity_tolerance": quantity_tolerance,
            "cash_tolerance": cash_tolerance,
            "pnl_tolerance": pnl_tolerance,
            "return_tolerance": return_tolerance,
            "drawdown_tolerance": drawdown_tolerance,
            "timestamp_tolerance_seconds": timestamp_tolerance_seconds,
        }
        return cls(
            policy_id=MATERIALITY_POLICY_ID,
            score_tolerance=score_tolerance,
            target_weight_tolerance=target_weight_tolerance,
            price_tolerance=price_tolerance,
            quantity_tolerance=quantity_tolerance,
            cash_tolerance=cash_tolerance,
            pnl_tolerance=pnl_tolerance,
            return_tolerance=return_tolerance,
            drawdown_tolerance=drawdown_tolerance,
            timestamp_tolerance_seconds=timestamp_tolerance_seconds,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class CanonicalSecuritiesSimulationRequest:
    baseline_id: str
    baseline_hash: str
    data_batch_id: str
    data_batch_hash: str
    universe: tuple[str, ...]
    universe_hash: str
    start_date: str
    end_date: str
    cutoff: datetime
    price_adjustment_state: SecuritiesAdjustmentState
    starting_capital: Decimal
    benchmark_identity: SecuritiesBenchmarkIdentity
    signal_timing: str
    rebalance_policy: str
    execution_policy_hash: str | None
    spread_bps: Decimal
    slippage_bps: Decimal
    commission_bps: Decimal
    corporate_action_policy: str
    cash_treatment: str
    quantity_rounding_policy: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        baseline: CanonicalSecuritiesBaseline,
        data_batch_id: str,
        data_batch_hash: str,
        universe: Sequence[str],
        start_date: str,
        end_date: str,
        cutoff: datetime,
        starting_capital: DecimalInput = "10000",
        execution_policy_hash: str | None = None,
        spread_bps: DecimalInput = "1",
        slippage_bps: DecimalInput = "1",
        commission_bps: DecimalInput = "0.5",
    ) -> CanonicalSecuritiesSimulationRequest:
        _iso_range(start_date, end_date)
        _aware(cutoff, "simulation cutoff")
        normalized_universe = tuple(sorted(universe))
        if not normalized_universe or len(normalized_universe) != len(set(normalized_universe)):
            raise SecuritiesBaselineError("simulation universe must be non-empty and unique")
        capital = parse_decimal(starting_capital, allow_zero=False)
        if capital <= 0:
            raise SecuritiesBaselineError("starting capital must be positive")
        costs = tuple(parse_decimal(value) for value in (spread_bps, slippage_bps, commission_bps))
        if any(value < 0 or value > 100 for value in costs):
            raise SecuritiesBaselineError("simulation costs must be between zero and 100 bps")
        universe_hash = content_digest(normalized_universe)
        payload: dict[str, object] = {
            "baseline_id": baseline.baseline_id,
            "baseline_hash": baseline.canonical_hash,
            "data_batch_id": data_batch_id,
            "data_batch_hash": data_batch_hash,
            "universe": normalized_universe,
            "universe_hash": universe_hash,
            "start_date": start_date,
            "end_date": end_date,
            "cutoff": cutoff,
            "price_adjustment_state": baseline.required_adjustment_state,
            "starting_capital": capital,
            "benchmark_identity": baseline.benchmark_identity,
            "signal_timing": "SESSION_CLOSE_DECISION_NEXT_SESSION_EXECUTION",
            "rebalance_policy": baseline.rebalance_policy,
            "execution_policy_hash": execution_policy_hash,
            "spread_bps": costs[0],
            "slippage_bps": costs[1],
            "commission_bps": costs[2],
            "corporate_action_policy": "TOTAL_RETURN_ADJUSTED_WITH_RETAINED_ACTION_AUDIT_V1",
            "cash_treatment": "UNALLOCATED_WEIGHT_REMAINS_CASH",
            "quantity_rounding_policy": "RESEARCH_TARGET_WEIGHTS_NO_SHARE_ROUNDING",
        }
        return cls(
            baseline_id=baseline.baseline_id,
            baseline_hash=baseline.canonical_hash,
            data_batch_id=data_batch_id,
            data_batch_hash=data_batch_hash,
            universe=normalized_universe,
            universe_hash=universe_hash,
            start_date=start_date,
            end_date=end_date,
            cutoff=cutoff,
            price_adjustment_state=baseline.required_adjustment_state,
            starting_capital=capital,
            benchmark_identity=baseline.benchmark_identity,
            signal_timing="SESSION_CLOSE_DECISION_NEXT_SESSION_EXECUTION",
            rebalance_policy=baseline.rebalance_policy,
            execution_policy_hash=execution_policy_hash,
            spread_bps=costs[0],
            slippage_bps=costs[1],
            commission_bps=costs[2],
            corporate_action_policy=("TOTAL_RETURN_ADJUSTED_WITH_RETAINED_ACTION_AUDIT_V1"),
            cash_treatment="UNALLOCATED_WEIGHT_REMAINS_CASH",
            quantity_rounding_policy="RESEARCH_TARGET_WEIGHTS_NO_SHARE_ROUNDING",
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class BaselineSignalEvidence:
    instrument_id: str
    decision_at: datetime
    direction: SecuritiesSignalDirection
    score: Decimal
    target_weight: Decimal
    source_bar_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class BaselineTransactionEvidence:
    instrument_id: str
    decision_at: datetime
    transaction_at: datetime
    prior_weight: Decimal
    target_weight: Decimal
    modeled_cost: Decimal
    execution_price: Decimal
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class BaselineEquityCheckpoint:
    session_at: datetime
    gross_equity: Decimal
    net_equity: Decimal
    cash_balance: Decimal
    gross_exposure: Decimal
    drawdown: Decimal
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class SecuritiesSimulationResult:
    simulator_identity: SecuritiesSimulatorIdentity
    simulator_version: str
    adapter_version: str
    mode: SecuritiesSimulationMode
    request_hash: str
    state: SecuritiesSimulationState
    signals: tuple[BaselineSignalEvidence, ...]
    transactions: tuple[BaselineTransactionEvidence, ...]
    equity_curve: tuple[BaselineEquityCheckpoint, ...]
    gross_return: Decimal | None
    net_return: Decimal | None
    annualized_return: Decimal | None
    volatility: Decimal | None
    maximum_drawdown: Decimal | None
    turnover: Decimal | None
    hit_rate: Decimal | None
    average_holding_period: Decimal | None
    average_exposure: Decimal | None
    cost_total: Decimal | None
    corporate_action_count: int
    position_count: int
    reason_codes: tuple[str, ...]
    managed_result_artifact: str | None
    managed_result_hash: str | None
    canonical_result_hash: str | None


@dataclass(frozen=True, slots=True)
class SecuritiesSimulationRun:
    simulation_run_id: str
    simulator_identity: SecuritiesSimulatorIdentity
    simulator_version: str
    adapter_version: str
    mode: SecuritiesSimulationMode
    baseline_id: str
    baseline_hash: str
    data_batch_id: str
    data_batch_hash: str
    universe_hash: str
    start_date: str
    end_date: str
    starting_capital: Decimal
    execution_policy_hash: str | None
    state: SecuritiesSimulationState
    signal_count: int
    order_count: int
    fill_count: int
    position_count: int
    gross_return: Decimal | None
    net_return: Decimal | None
    maximum_drawdown: Decimal | None
    turnover: Decimal | None
    managed_result_artifact: str | None
    managed_result_hash: str | None
    started_at: datetime
    completed_at: datetime
    failure_code: str | None
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str | None
    result: SecuritiesSimulationResult


@dataclass(frozen=True, slots=True)
class SecuritiesSimulatorConformanceReport:
    report_id: str
    baseline_id: str
    baseline_hash: str
    data_batch_hash: str
    universe_hash: str
    date_scope_hash: str
    reference_run_id: str
    comparison_run_id: str
    comparison_mode: SecuritiesSimulationMode
    signal_agreement: Decimal
    decision_time_disagreements: int
    direction_disagreements: int
    target_weight_disagreements: int
    transaction_disagreements: int
    fill_time_disagreements: int
    fill_price_disagreements: int
    corporate_action_disagreements: int
    position_disagreements: int
    cash_difference: Decimal | None
    realized_pnl_difference: Decimal | None
    unrealized_pnl_difference: Decimal | None
    return_difference: Decimal | None
    drawdown_difference: Decimal | None
    turnover_difference: Decimal | None
    materiality_policy_id: str
    materiality_policy_hash: str
    classification: MultiSimulatorConformanceClassification
    reason_codes: tuple[str, ...]
    canonical_input_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class SecuritiesBaselineScorecard:
    scorecard_id: str
    baseline_id: str
    baseline_hash: str
    data_batch_id: str
    data_batch_hash: str
    universe_hash: str
    start_date: str
    end_date: str
    simulator_identity: SecuritiesSimulatorIdentity
    benchmark_identity: SecuritiesBenchmarkIdentity
    signal_count: int
    trade_count: int
    gross_return: Decimal
    net_return: Decimal
    annualized_return: Decimal | None
    volatility: Decimal | None
    maximum_drawdown: Decimal
    turnover: Decimal
    hit_rate: Decimal
    average_holding_period: Decimal
    exposure: Decimal
    cost_total: Decimal
    corporate_action_count: int
    conformance_state: MultiSimulatorConformanceClassification
    integrity_warnings: tuple[str, ...]
    canonical_input_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class StrategyLabBaselineComparison:
    comparison_id: str
    candidate_id: str
    candidate_result_hash: str
    baseline_scorecard_id: str
    baseline_id: str
    baseline_hash: str
    data_batch_hash: str
    universe_hash: str
    date_scope_hash: str
    aligned: bool
    integrity_blocked: bool
    candidate_net_return: Decimal
    baseline_net_return: Decimal
    net_return_difference: Decimal
    reason_codes: tuple[str, ...]
    canonical_hash: str


def canonical_securities_baselines(
    created_at: datetime,
) -> tuple[CanonicalSecuritiesBaseline, ...]:
    """Return the eight reviewed, versioned, transparent baseline contracts."""

    return (
        CanonicalSecuritiesBaseline.create(
            family=CanonicalBaselineFamily.CASH,
            display_name="Cash Baseline V1",
            description="Remain fully in simulated cash at every decision.",
            feature_rules=("NO_PRICE_FEATURES",),
            decision_rules=("TARGET_WEIGHT_ZERO_FOR_EVERY_INSTRUMENT",),
            universe_requirements=("EXPLICIT_NONEMPTY_UNIVERSE",),
            rebalance_policy="NO_REBALANCE",
            holding_policy="CASH_FOR_FULL_PERIOD",
            benchmark_identity=SecuritiesBenchmarkIdentity.CASH,
            created_at=created_at,
        ),
        CanonicalSecuritiesBaseline.create(
            family=CanonicalBaselineFamily.BUY_AND_HOLD,
            display_name="Buy and Hold V1",
            description=(
                "Equal-weight eligible instruments once and hold through the terminal date."
            ),
            feature_rules=("FIRST_ELIGIBLE_TOTAL_RETURN_CLOSE",),
            decision_rules=("EQUAL_WEIGHT_LONG_AT_FIRST_DECISION", "NO_INTERMEDIATE_REBALANCE"),
            universe_requirements=("COMPLETE_ALIGNED_DAILY_ROWS",),
            rebalance_policy="INITIAL_ONLY",
            holding_policy="TERMINAL_DATE",
            benchmark_identity=SecuritiesBenchmarkIdentity.BUY_AND_HOLD_UNIVERSE,
            created_at=created_at,
        ),
        CanonicalSecuritiesBaseline.create(
            family=CanonicalBaselineFamily.EQUAL_WEIGHT_REBALANCE,
            display_name="Equal Weight Rebalance V1",
            description="Long-only equal-weight universe with deterministic 20-session rebalance.",
            feature_rules=("ELIGIBLE_UNIVERSE_MEMBERSHIP",),
            decision_rules=("EQUAL_WEIGHT_LONG", "REBALANCE_EVERY_20_SESSIONS"),
            universe_requirements=("COMPLETE_ALIGNED_DAILY_ROWS",),
            rebalance_policy="EVERY_20_SESSIONS",
            holding_policy="UNTIL_NEXT_REBALANCE",
            created_at=created_at,
        ),
        CanonicalSecuritiesBaseline.create(
            family=CanonicalBaselineFamily.TIME_SERIES_MOMENTUM,
            display_name="Time-Series Momentum V1",
            description="Long instruments with positive prior 20-session return; otherwise cash.",
            feature_rules=("TRAILING_20_SESSION_TOTAL_RETURN", "PRIOR_AVAILABLE_ROWS_ONLY"),
            decision_rules=("POSITIVE_RETURN_LONG", "NONPOSITIVE_RETURN_FLAT"),
            universe_requirements=("AT_LEAST_21_ALIGNED_DAILY_ROWS",),
            rebalance_policy="EVERY_5_SESSIONS",
            holding_policy="UNTIL_NEXT_REBALANCE",
            analytical_short_behavior="OPTIONAL_NEGATIVE_RETURN_SHORT_RESEARCH_ONLY",
            created_at=created_at,
        ),
        CanonicalSecuritiesBaseline.create(
            family=CanonicalBaselineFamily.CROSS_SECTIONAL_MOMENTUM,
            display_name="Cross-Sectional Momentum V1",
            description="Equal-weight top positive-return quartile across the exact universe.",
            feature_rules=("TRAILING_20_SESSION_TOTAL_RETURN", "CROSS_SECTIONAL_RANK"),
            decision_rules=("LONG_TOP_POSITIVE_QUARTILE", "FLAT_REMAINDER"),
            universe_requirements=("AT_LEAST_4_INSTRUMENTS", "ALIGNED_DAILY_ROWS"),
            rebalance_policy="EVERY_5_SESSIONS",
            holding_policy="UNTIL_NEXT_REBALANCE",
            created_at=created_at,
        ),
        CanonicalSecuritiesBaseline.create(
            family=CanonicalBaselineFamily.SHORT_HORIZON_MEAN_REVERSION,
            display_name="Short-Horizon Mean Reversion V1",
            description=(
                "Long the bottom negative-return quartile for a fixed five-session horizon."
            ),
            feature_rules=("TRAILING_5_SESSION_TOTAL_RETURN", "CROSS_SECTIONAL_ASCENDING_RANK"),
            decision_rules=("LONG_NEGATIVE_BOTTOM_QUARTILE", "FLAT_REMAINDER"),
            universe_requirements=("AT_LEAST_4_LIQUID_INSTRUMENTS", "ALIGNED_DAILY_ROWS"),
            rebalance_policy="EVERY_5_SESSIONS",
            holding_policy="FIXED_5_SESSION_HORIZON",
            created_at=created_at,
        ),
        CanonicalSecuritiesBaseline.create(
            family=CanonicalBaselineFamily.PAIRS_DISTANCE_MEAN_REVERSION,
            display_name="Pairs Distance Mean Reversion V1",
            description=(
                "Predeclared pairs use training-only normalization and fixed z-score rules."
            ),
            feature_rules=(
                "PREDECLARED_ADJACENT_INSTRUMENT_PAIRS",
                "FIRST_60_SESSIONS_TRAINING_ONLY",
                "NORMALIZED_PRICE_DISTANCE",
            ),
            decision_rules=(
                "ABS_Z_AT_LEAST_1_LONG_WEAK_LEG",
                "ANALYTICAL_SHORT_STRONG_LEG",
                "ABS_Z_AT_MOST_0_25_EXIT",
            ),
            universe_requirements=("EVEN_INSTRUMENT_COUNT", "AT_LEAST_120_ALIGNED_DAILY_ROWS"),
            rebalance_policy="DAILY_AFTER_TRAINING",
            holding_policy="UNTIL_FIXED_Z_SCORE_EXIT",
            analytical_short_behavior="FULL_PAIR_SHORT_RESEARCH_ONLY_LONG_LEG_SCORE_SEPARATE",
            paper_compatibility_state=PaperCompatibilityState.RESEARCH_ONLY,
            created_at=created_at,
        ),
        CanonicalSecuritiesBaseline.create(
            family=CanonicalBaselineFamily.VOLATILITY_TARGETED_LONG_ONLY,
            display_name="Volatility-Targeted Long-Only V1",
            description="Scale equal-weight long exposure to a 10% target using prior volatility.",
            feature_rules=(
                "TRAILING_20_SESSION_EQUAL_WEIGHT_RETURN_VOLATILITY",
                "ANNUALIZATION_SQRT_252",
            ),
            decision_rules=("TARGET_VOLATILITY_10_PERCENT", "EXPOSURE_CAPPED_AT_1"),
            universe_requirements=("AT_LEAST_21_ALIGNED_DAILY_ROWS",),
            rebalance_policy="EVERY_5_SESSIONS",
            holding_policy="UNTIL_NEXT_REBALANCE",
            created_at=created_at,
        ),
    )


def run_apex_research_portfolio(
    *,
    request: CanonicalSecuritiesSimulationRequest,
    baseline: CanonicalSecuritiesBaseline,
    bars: Sequence[SecuritiesBarEvidence],
    corporate_actions: Sequence[SecuritiesCorporateActionEvidence],
    simulator_identity: SecuritiesSimulatorIdentity = SecuritiesSimulatorIdentity.APEX_RESEARCH,
    simulator_version: str = "APEX_RESEARCH_V1",
    adapter_version: str = "APEX_CANONICAL_BASELINE_ADAPTER_V1",
) -> SecuritiesSimulationResult:
    """Run one deterministic, next-session, research-only portfolio simulation."""

    if (
        request.baseline_id != baseline.baseline_id
        or request.baseline_hash != baseline.canonical_hash
    ):
        raise SecuritiesBaselineError("simulation request baseline binding mismatch")
    selected = _aligned_bars(request=request, baseline=baseline, bars=bars)
    action_count, action_warnings = _corporate_action_scope(request, corporate_actions)
    if "DELISTING_TERMINAL_VALUE_REQUIRED" in action_warnings:
        return _failed_result(
            request=request,
            simulator_identity=simulator_identity,
            simulator_version=simulator_version,
            adapter_version=adapter_version,
            reason_codes=action_warnings,
            corporate_action_count=action_count,
        )
    times = tuple(sorted(selected))
    price_by_time = {
        moment: {instrument: bar.close for instrument, bar in selected[moment].items()}
        for moment in times
    }
    current_weights = {instrument: Decimal(0) for instrument in request.universe}
    pending: tuple[datetime, dict[str, Decimal]] | None = None
    gross_equity = request.starting_capital
    net_equity = request.starting_capital
    peak = request.starting_capital
    turnover = Decimal(0)
    cost_total = Decimal(0)
    signals: list[BaselineSignalEvidence] = []
    transactions: list[BaselineTransactionEvidence] = []
    checkpoints: list[BaselineEquityCheckpoint] = []
    daily_net_returns: list[Decimal] = []
    holding_starts: dict[str, int] = {}
    holding_periods: list[int] = []
    exposure_total = Decimal(0)
    hit_count = 0
    hit_denominator = 0
    total_cost_bps = request.spread_bps + request.slippage_bps + request.commission_bps
    for index, moment in enumerate(times):
        if index:
            previous = times[index - 1]
            gross_factor = Decimal(1)
            net_factor = Decimal(1)
            portfolio_return = Decimal(0)
            for instrument, weight in current_weights.items():
                price_return = price_by_time[moment][instrument] / price_by_time[previous][
                    instrument
                ] - Decimal(1)
                portfolio_return += weight * price_return
            gross_factor += portfolio_return
            net_factor += portfolio_return
            gross_equity *= gross_factor
            prior_net = net_equity
            net_equity *= net_factor
            daily_return = net_equity / prior_net - Decimal(1) if prior_net else Decimal(0)
            daily_net_returns.append(daily_return)
            if sum(current_weights.values(), Decimal(0)) > 0:
                hit_denominator += 1
                hit_count += int(daily_return > 0)
        if pending is not None:
            decision_at, targets = pending
            session_turnover = sum(
                (abs(targets[item] - current_weights[item]) for item in request.universe),
                Decimal(0),
            )
            session_cost = net_equity * session_turnover * total_cost_bps / Decimal(10000)
            net_equity -= session_cost
            cost_total += session_cost
            turnover += session_turnover
            for instrument in request.universe:
                prior = current_weights[instrument]
                target = targets[instrument]
                if prior == target:
                    continue
                instrument_cost = (
                    session_cost * abs(target - prior) / session_turnover
                    if session_turnover
                    else Decimal(0)
                )
                transaction_payload = {
                    "instrument_id": instrument,
                    "decision_at": decision_at,
                    "transaction_at": moment,
                    "prior_weight": prior,
                    "target_weight": target,
                    "modeled_cost": _metric(instrument_cost),
                    "execution_price": price_by_time[moment][instrument],
                }
                transactions.append(
                    BaselineTransactionEvidence(
                        instrument_id=instrument,
                        decision_at=decision_at,
                        transaction_at=moment,
                        prior_weight=prior,
                        target_weight=target,
                        modeled_cost=_metric(instrument_cost),
                        execution_price=price_by_time[moment][instrument],
                        canonical_hash=content_digest(transaction_payload),
                    )
                )
                if prior == 0 and target > 0:
                    holding_starts[instrument] = index
                if prior > 0 and target == 0 and instrument in holding_starts:
                    holding_periods.append(index - holding_starts.pop(instrument))
            current_weights = targets
        exposure = sum(current_weights.values(), Decimal(0))
        exposure_total += exposure
        peak = max(peak, net_equity)
        drawdown = net_equity / peak - Decimal(1) if peak else Decimal(0)
        checkpoint_payload = {
            "session_at": moment,
            "gross_equity": _metric(gross_equity),
            "net_equity": _metric(net_equity),
            "cash_balance": _metric(net_equity * (Decimal(1) - exposure)),
            "gross_exposure": _metric(exposure),
            "drawdown": _metric(drawdown),
        }
        checkpoints.append(
            BaselineEquityCheckpoint(
                session_at=moment,
                gross_equity=_metric(gross_equity),
                net_equity=_metric(net_equity),
                cash_balance=_metric(net_equity * (Decimal(1) - exposure)),
                gross_exposure=_metric(exposure),
                drawdown=_metric(drawdown),
                canonical_hash=content_digest(checkpoint_payload),
            )
        )
        targets, session_signals = _baseline_targets(
            baseline=baseline,
            request=request,
            times=times,
            selected=selected,
            index=index,
            current_weights=current_weights,
        )
        signals.extend(session_signals)
        pending = (moment, targets)
    for instrument, opened_at in holding_starts.items():
        if current_weights[instrument] > 0:
            holding_periods.append(len(times) - opened_at)
    gross_return = gross_equity / request.starting_capital - Decimal(1)
    net_return = net_equity / request.starting_capital - Decimal(1)
    maximum_drawdown = min((item.drawdown for item in checkpoints), default=Decimal(0))
    annualized = (
        _metric(
            (Decimal(1) + net_return) ** (Decimal(252) / Decimal(len(daily_net_returns)))
            - Decimal(1)
        )
        if len(daily_net_returns) >= 252 and Decimal(1) + net_return > 0
        else None
    )
    volatility = (
        _metric(_standard_deviation(daily_net_returns) * Decimal(252).sqrt())
        if len(daily_net_returns) >= 2
        else None
    )
    result_payload = {
        "request_hash": request.canonical_hash,
        "simulator_identity": simulator_identity,
        "signals": tuple(item.canonical_hash for item in signals),
        "transactions": tuple(item.canonical_hash for item in transactions),
        "equity_curve": tuple(item.canonical_hash for item in checkpoints),
        "gross_return": _metric(gross_return),
        "net_return": _metric(net_return),
        "annualized_return": annualized,
        "volatility": volatility,
        "maximum_drawdown": _metric(maximum_drawdown),
        "turnover": _metric(turnover),
        "cost_total": _metric(cost_total),
        "corporate_action_count": action_count,
    }
    reason_codes = (
        CANONICAL_BASELINE_WARNING,
        BASELINE_RESEARCH_WARNING,
        *action_warnings,
        "NEXT_SESSION_EXECUTION_NO_LOOKAHEAD",
    )
    return SecuritiesSimulationResult(
        simulator_identity=simulator_identity,
        simulator_version=simulator_version,
        adapter_version=adapter_version,
        mode=SecuritiesSimulationMode.RESEARCH_PORTFOLIO,
        request_hash=request.canonical_hash,
        state=SecuritiesSimulationState.COMPLETED,
        signals=tuple(signals),
        transactions=tuple(transactions),
        equity_curve=tuple(checkpoints),
        gross_return=_metric(gross_return),
        net_return=_metric(net_return),
        annualized_return=annualized,
        volatility=volatility,
        maximum_drawdown=_metric(maximum_drawdown),
        turnover=_metric(turnover),
        hit_rate=_metric(
            Decimal(hit_count) / Decimal(hit_denominator) if hit_denominator else Decimal(0)
        ),
        average_holding_period=_metric(
            Decimal(sum(holding_periods)) / Decimal(len(holding_periods))
            if holding_periods
            else Decimal(0)
        ),
        average_exposure=_metric(exposure_total / Decimal(len(times))),
        cost_total=_metric(cost_total),
        corporate_action_count=action_count,
        position_count=max(
            (
                sum(item.target_weight > 0 for item in signals_by_time)
                for signals_by_time in _group_signals(signals).values()
            ),
            default=0,
        ),
        reason_codes=reason_codes,
        managed_result_artifact=None,
        managed_result_hash=None,
        canonical_result_hash=content_digest(result_payload),
    )


def simulation_run(
    *,
    request: CanonicalSecuritiesSimulationRequest,
    result: SecuritiesSimulationResult,
    idempotency_key: str,
    started_at: datetime,
    completed_at: datetime,
) -> SecuritiesSimulationRun:
    _aware(started_at, "simulation start")
    _aware(completed_at, "simulation completion")
    if started_at > completed_at or result.request_hash != request.canonical_hash:
        raise SecuritiesBaselineError("simulation run timing or request binding is invalid")
    payload = {
        "request_hash": request.canonical_hash,
        "simulator_identity": result.simulator_identity,
        "simulator_version": result.simulator_version,
        "adapter_version": result.adapter_version,
        "mode": result.mode,
        "idempotency_key": idempotency_key,
    }
    return SecuritiesSimulationRun(
        simulation_run_id=deterministic_id("securities_simulation_run", payload),
        simulator_identity=result.simulator_identity,
        simulator_version=result.simulator_version,
        adapter_version=result.adapter_version,
        mode=result.mode,
        baseline_id=request.baseline_id,
        baseline_hash=request.baseline_hash,
        data_batch_id=request.data_batch_id,
        data_batch_hash=request.data_batch_hash,
        universe_hash=request.universe_hash,
        start_date=request.start_date,
        end_date=request.end_date,
        starting_capital=request.starting_capital,
        execution_policy_hash=request.execution_policy_hash,
        state=result.state,
        signal_count=len(result.signals),
        order_count=len(result.transactions),
        fill_count=len(result.transactions),
        position_count=result.position_count,
        gross_return=result.gross_return,
        net_return=result.net_return,
        maximum_drawdown=result.maximum_drawdown,
        turnover=result.turnover,
        managed_result_artifact=result.managed_result_artifact,
        managed_result_hash=result.managed_result_hash,
        started_at=started_at.astimezone(UTC),
        completed_at=completed_at.astimezone(UTC),
        failure_code=(
            None if result.state is SecuritiesSimulationState.COMPLETED else result.reason_codes[-1]
        ),
        idempotency_key=idempotency_key,
        canonical_input_hash=request.canonical_hash,
        canonical_result_hash=result.canonical_result_hash,
        result=result,
    )


def compare_simulator_runs(
    *,
    reference: SecuritiesSimulationRun,
    comparison: SecuritiesSimulationRun,
    policy: SecuritiesConformanceMaterialityPolicy,
    explanation_codes: Sequence[str] = (),
) -> SecuritiesSimulatorConformanceReport:
    """Compare signal, transaction, and result evidence under one exact request."""

    date_scope_hash = content_digest((reference.start_date, reference.end_date))
    input_payload = {
        "reference_run_id": reference.simulation_run_id,
        "comparison_run_id": comparison.simulation_run_id,
        "reference_request_hash": reference.canonical_input_hash,
        "comparison_request_hash": comparison.canonical_input_hash,
        "materiality_policy_hash": policy.canonical_hash,
        "explanation_codes": tuple(explanation_codes),
    }
    canonical_input_hash = content_digest(input_payload)
    if reference.canonical_input_hash != comparison.canonical_input_hash:
        classification = MultiSimulatorConformanceClassification.INCOMPARABLE_CONFIGURATION
        reasons: tuple[str, ...] = ("CANONICAL_REQUEST_HASH_MISMATCH",)
        return _conformance_report(
            reference=reference,
            comparison=comparison,
            policy=policy,
            date_scope_hash=date_scope_hash,
            classification=classification,
            reasons=reasons,
            canonical_input_hash=canonical_input_hash,
        )
    if comparison.state is SecuritiesSimulationState.RUNTIME_UNAVAILABLE:
        return _conformance_report(
            reference=reference,
            comparison=comparison,
            policy=policy,
            date_scope_hash=date_scope_hash,
            classification=MultiSimulatorConformanceClassification.ENGINE_UNAVAILABLE,
            reasons=("EXTERNAL_ENGINE_UNAVAILABLE",),
            canonical_input_hash=canonical_input_hash,
        )
    if (
        reference.state is not SecuritiesSimulationState.COMPLETED
        or comparison.state is not SecuritiesSimulationState.COMPLETED
        or not reference.result.equity_curve
        or not comparison.result.equity_curve
    ):
        return _conformance_report(
            reference=reference,
            comparison=comparison,
            policy=policy,
            date_scope_hash=date_scope_hash,
            classification=(MultiSimulatorConformanceClassification.INSUFFICIENT_COMPARABLE_OUTPUT),
            reasons=("MISSING_EXTERNAL_OUTPUT",),
            canonical_input_hash=canonical_input_hash,
        )
    differences = _comparison_differences(reference.result, comparison.result, policy)
    material = any(
        (
            _required_int(differences, "decision_time_disagreements"),
            _required_int(differences, "direction_disagreements"),
            _required_int(differences, "target_weight_disagreements"),
            _required_int(differences, "transaction_disagreements"),
            _required_int(differences, "fill_time_disagreements"),
            _required_int(differences, "fill_price_disagreements"),
            _required_int(differences, "corporate_action_disagreements"),
            _required_int(differences, "position_disagreements"),
        )
    ) or any(
        abs(value) > tolerance
        for value, tolerance in (
            (_required_decimal(differences, "cash_difference"), policy.cash_tolerance),
            (_required_decimal(differences, "return_difference"), policy.return_tolerance),
            (
                _required_decimal(differences, "drawdown_difference"),
                policy.drawdown_tolerance,
            ),
            (
                _required_decimal(differences, "turnover_difference"),
                policy.quantity_tolerance,
            ),
        )
    )
    supplied_explanations = tuple(dict.fromkeys(explanation_codes))
    if any(code not in SUPPORTED_EXPLANATION_CODES for code in supplied_explanations):
        raise SecuritiesBaselineError("unsupported simulator difference explanation")
    if not material:
        classification = MultiSimulatorConformanceClassification.CONFORMANT
        reasons = ("ALL_THREE_CONFORMANCE_LAYERS_WITHIN_POLICY",)
    elif supplied_explanations:
        classification = MultiSimulatorConformanceClassification.EXPLAINED_DIFFERENCE
        reasons = supplied_explanations
    else:
        classification = MultiSimulatorConformanceClassification.MATERIAL_MISMATCH
        reasons = ("UNEXPLAINED_MATERIAL_SIMULATOR_DIFFERENCE",)
    return _conformance_report(
        reference=reference,
        comparison=comparison,
        policy=policy,
        date_scope_hash=date_scope_hash,
        classification=classification,
        reasons=reasons,
        canonical_input_hash=canonical_input_hash,
        differences=differences,
    )


def build_baseline_scorecard(
    *,
    baseline: CanonicalSecuritiesBaseline,
    request: CanonicalSecuritiesSimulationRequest,
    run: SecuritiesSimulationRun,
    conformance_state: MultiSimulatorConformanceClassification,
    integrity_warnings: Sequence[str] = (),
) -> SecuritiesBaselineScorecard:
    if run.state is not SecuritiesSimulationState.COMPLETED:
        raise SecuritiesBaselineError("scorecards require a completed simulator run")
    result = run.result
    required = (
        result.gross_return,
        result.net_return,
        result.maximum_drawdown,
        result.turnover,
        result.hit_rate,
        result.average_holding_period,
        result.average_exposure,
        result.cost_total,
    )
    if any(value is None for value in required):
        raise SecuritiesBaselineError("scorecard metrics are incomplete")
    metrics = tuple(value for value in required if value is not None)
    input_payload = {
        "baseline_hash": baseline.canonical_hash,
        "request_hash": request.canonical_hash,
        "simulation_run_id": run.simulation_run_id,
        "conformance_state": conformance_state,
    }
    result_payload = {
        "metrics": metrics,
        "annualized_return": result.annualized_return,
        "volatility": result.volatility,
        "warnings": tuple(integrity_warnings),
    }
    canonical_input_hash = content_digest(input_payload)
    canonical_result_hash = content_digest(result_payload)
    return SecuritiesBaselineScorecard(
        scorecard_id=deterministic_id(
            "securities_baseline_scorecard",
            {
                "canonical_input_hash": canonical_input_hash,
                "canonical_result_hash": canonical_result_hash,
            },
        ),
        baseline_id=baseline.baseline_id,
        baseline_hash=baseline.canonical_hash,
        data_batch_id=request.data_batch_id,
        data_batch_hash=request.data_batch_hash,
        universe_hash=request.universe_hash,
        start_date=request.start_date,
        end_date=request.end_date,
        simulator_identity=run.simulator_identity,
        benchmark_identity=baseline.benchmark_identity,
        signal_count=run.signal_count,
        trade_count=run.fill_count,
        gross_return=metrics[0],
        net_return=metrics[1],
        annualized_return=result.annualized_return,
        volatility=result.volatility,
        maximum_drawdown=metrics[2],
        turnover=metrics[3],
        hit_rate=metrics[4],
        average_holding_period=metrics[5],
        exposure=metrics[6],
        cost_total=metrics[7],
        corporate_action_count=result.corporate_action_count,
        conformance_state=conformance_state,
        integrity_warnings=tuple(integrity_warnings),
        canonical_input_hash=canonical_input_hash,
        canonical_result_hash=canonical_result_hash,
    )


def scorecard_conformance(
    reports: Sequence[SecuritiesSimulatorConformanceReport],
) -> tuple[MultiSimulatorConformanceClassification, tuple[str, ...]]:
    """Return the strictest retained simulator state and its integrity warnings."""

    if not reports:
        return MultiSimulatorConformanceClassification.CONFORMANT, ()
    priority = {
        MultiSimulatorConformanceClassification.CONFORMANT: 0,
        MultiSimulatorConformanceClassification.ENGINE_UNAVAILABLE: 1,
        MultiSimulatorConformanceClassification.EXPLAINED_DIFFERENCE: 2,
        MultiSimulatorConformanceClassification.INSUFFICIENT_COMPARABLE_OUTPUT: 3,
        MultiSimulatorConformanceClassification.INCOMPARABLE_CONFIGURATION: 4,
        MultiSimulatorConformanceClassification.MATERIAL_MISMATCH: 5,
    }
    state = max(
        (report.classification for report in reports),
        key=priority.__getitem__,
    )
    warnings = {
        reason
        for report in reports
        if report.classification is not MultiSimulatorConformanceClassification.CONFORMANT
        for reason in report.reason_codes
    }
    if state is MultiSimulatorConformanceClassification.MATERIAL_MISMATCH:
        warnings.add("MATERIAL_SIMULATOR_MISMATCH_BLOCKS_AUTHORITATIVE_COMPARISON")
    return state, tuple(sorted(warnings))


def compare_strategy_lab_candidate(
    *,
    candidate_id: str,
    candidate_result_hash: str,
    candidate_data_batch_hash: str,
    candidate_universe_hash: str,
    candidate_start_date: str,
    candidate_end_date: str,
    candidate_net_return: DecimalInput,
    scorecard: SecuritiesBaselineScorecard,
) -> StrategyLabBaselineComparison:
    date_scope_hash = content_digest((candidate_start_date, candidate_end_date))
    aligned = (
        candidate_data_batch_hash == scorecard.data_batch_hash
        and candidate_universe_hash == scorecard.universe_hash
        and candidate_start_date == scorecard.start_date
        and candidate_end_date == scorecard.end_date
    )
    integrity_blocked = (
        scorecard.conformance_state is MultiSimulatorConformanceClassification.MATERIAL_MISMATCH
    )
    candidate_return = parse_decimal(candidate_net_return, allow_negative=True)
    difference = _metric(candidate_return - scorecard.net_return)
    reasons = []
    if not aligned:
        reasons.append("BASELINE_SCOPE_NOT_ALIGNED")
    if integrity_blocked:
        reasons.append("BASELINE_MATERIAL_CONFORMANCE_MISMATCH")
    if aligned and not integrity_blocked:
        reasons.append("ALIGNED_BASELINE_EVIDENCE_AVAILABLE")
    payload = {
        "candidate_id": candidate_id,
        "candidate_result_hash": candidate_result_hash,
        "baseline_scorecard_id": scorecard.scorecard_id,
        "baseline_id": scorecard.baseline_id,
        "baseline_hash": scorecard.baseline_hash,
        "data_batch_hash": scorecard.data_batch_hash,
        "universe_hash": scorecard.universe_hash,
        "date_scope_hash": date_scope_hash,
        "aligned": aligned,
        "integrity_blocked": integrity_blocked,
        "candidate_net_return": candidate_return,
        "baseline_net_return": scorecard.net_return,
        "net_return_difference": difference,
        "reason_codes": tuple(reasons),
    }
    canonical_hash = content_digest(payload)
    return StrategyLabBaselineComparison(
        comparison_id=deterministic_id("strategy_lab_baseline_comparison", payload),
        candidate_id=candidate_id,
        candidate_result_hash=candidate_result_hash,
        baseline_scorecard_id=scorecard.scorecard_id,
        baseline_id=scorecard.baseline_id,
        baseline_hash=scorecard.baseline_hash,
        data_batch_hash=scorecard.data_batch_hash,
        universe_hash=scorecard.universe_hash,
        date_scope_hash=date_scope_hash,
        aligned=aligned,
        integrity_blocked=integrity_blocked,
        candidate_net_return=candidate_return,
        baseline_net_return=scorecard.net_return,
        net_return_difference=difference,
        reason_codes=tuple(reasons),
        canonical_hash=canonical_hash,
    )


def research_trial_summary(
    runs: Sequence[SecuritiesSimulationRun],
    baselines: Sequence[CanonicalSecuritiesBaseline],
    reports: Sequence[SecuritiesSimulatorConformanceReport],
) -> dict[str, object]:
    """Summarize every retained attempt without selecting only winners."""

    materially_mismatched_ids = {
        report.comparison_run_id
        for report in reports
        if report.classification is MultiSimulatorConformanceClassification.MATERIAL_MISMATCH
    }
    return {
        "baseline_specification_count": len(baselines),
        "parameterized_variant_count": len({item.baseline_hash for item in runs}),
        "simulators_used": tuple(sorted({item.simulator_identity.value for item in runs})),
        "total_runs": len(runs),
        "failed_runs": sum(item.state is SecuritiesSimulationState.FAILED for item in runs),
        "unavailable_engine_runs": sum(
            item.state is SecuritiesSimulationState.RUNTIME_UNAVAILABLE for item in runs
        ),
        "materially_mismatched_runs": len(materially_mismatched_ids),
    }


def unavailable_simulation_result(
    *,
    request: CanonicalSecuritiesSimulationRequest,
    simulator_identity: SecuritiesSimulatorIdentity,
    version: str,
    reason_code: str,
) -> SecuritiesSimulationResult:
    return _failed_result(
        request=request,
        simulator_identity=simulator_identity,
        simulator_version=version,
        adapter_version="APEX_EXTERNAL_SIMULATOR_PROTOCOL_V1",
        reason_codes=(EXTERNAL_SIMULATOR_WARNING, reason_code),
        corporate_action_count=0,
        state=SecuritiesSimulationState.RUNTIME_UNAVAILABLE,
    )


def explained_fixture_result(
    reference: SecuritiesSimulationResult,
    *,
    material: bool,
) -> SecuritiesSimulationResult:
    """Return clearly labeled deterministic fixture evidence for failure proofs."""

    if not reference.signals:
        return reference
    first = reference.signals[0]
    delta = Decimal("0.1") if material else Decimal("0.000001")
    changed = replace(
        first,
        target_weight=_metric(first.target_weight + delta),
        reason_codes=(*first.reason_codes, "DETERMINISTIC_FIXTURE_DIFFERENCE"),
    )
    changed = replace(
        changed,
        canonical_hash=content_digest(
            {
                "fixture": True,
                "source": first.canonical_hash,
                "target_weight": changed.target_weight,
            }
        ),
    )
    signals = (changed, *reference.signals[1:])
    payload = {
        "fixture": "MATERIAL" if material else "EXPLAINED",
        "source_result_hash": reference.canonical_result_hash,
        "signals": tuple(item.canonical_hash for item in signals),
    }
    return replace(
        reference,
        simulator_identity=SecuritiesSimulatorIdentity.DETERMINISTIC_FIXTURE,
        simulator_version="DETERMINISTIC_CONFORMANCE_FIXTURE_V1",
        adapter_version="DETERMINISTIC_CONFORMANCE_FIXTURE_V1",
        signals=signals,
        reason_codes=(
            EXTERNAL_SIMULATOR_WARNING,
            "DETERMINISTIC_FIXTURE_NOT_EXTERNAL_ENGINE",
        ),
        canonical_result_hash=content_digest(payload),
    )


def _baseline_targets(
    *,
    baseline: CanonicalSecuritiesBaseline,
    request: CanonicalSecuritiesSimulationRequest,
    times: Sequence[datetime],
    selected: Mapping[datetime, Mapping[str, SecuritiesBarEvidence]],
    index: int,
    current_weights: Mapping[str, Decimal],
) -> tuple[dict[str, Decimal], tuple[BaselineSignalEvidence, ...]]:
    instruments = request.universe
    scores = {instrument: Decimal(0) for instrument in instruments}
    targets = {instrument: Decimal(0) for instrument in instruments}
    reasons: dict[str, tuple[str, ...]] = {
        instrument: ("FLAT_BY_SPECIFICATION",) for instrument in instruments
    }
    family = baseline.family
    should_rebalance = True
    if family is CanonicalBaselineFamily.CASH:
        should_rebalance = False
        reasons = {instrument: ("CASH_BASELINE",) for instrument in instruments}
    elif family is CanonicalBaselineFamily.BUY_AND_HOLD:
        should_rebalance = index == 0
        if should_rebalance:
            targets = _equal_weights(instruments)
            reasons = {instrument: ("INITIAL_BUY_AND_HOLD_LONG",) for instrument in instruments}
        else:
            targets = dict(current_weights)
            reasons = {instrument: ("HOLD_WITHOUT_REBALANCE",) for instrument in instruments}
    elif family is CanonicalBaselineFamily.EQUAL_WEIGHT_REBALANCE:
        should_rebalance = index % 20 == 0
        targets = _equal_weights(instruments) if should_rebalance else dict(current_weights)
        reasons = {
            instrument: (
                ("EQUAL_WEIGHT_REBALANCE",) if should_rebalance else ("RETAIN_PREVIOUS_WEIGHT",)
            )
            for instrument in instruments
        }
    elif family is CanonicalBaselineFamily.TIME_SERIES_MOMENTUM:
        should_rebalance = index >= 20 and index % 5 == 0
        if should_rebalance:
            positive: list[str] = []
            for instrument in instruments:
                score = _trailing_return(selected, times, instrument, index, 20)
                scores[instrument] = score
                if score > 0:
                    positive.append(instrument)
            targets = _equal_weights(positive, all_instruments=instruments)
            reasons = {
                instrument: (
                    ("POSITIVE_TRAILING_RETURN",)
                    if instrument in positive
                    else ("NON_POSITIVE_TRAILING_RETURN",)
                )
                for instrument in instruments
            }
        else:
            targets = dict(current_weights)
            reasons = {instrument: ("RETAIN_PREVIOUS_WEIGHT",) for instrument in instruments}
    elif family in {
        CanonicalBaselineFamily.CROSS_SECTIONAL_MOMENTUM,
        CanonicalBaselineFamily.SHORT_HORIZON_MEAN_REVERSION,
    }:
        lookback = 20 if family is CanonicalBaselineFamily.CROSS_SECTIONAL_MOMENTUM else 5
        should_rebalance = index >= lookback and index % 5 == 0
        if should_rebalance:
            for instrument in instruments:
                scores[instrument] = _trailing_return(selected, times, instrument, index, lookback)
            count = max(1, (len(instruments) + 3) // 4)
            ordered = sorted(
                instruments,
                key=lambda item: (
                    -scores[item]
                    if family is CanonicalBaselineFamily.CROSS_SECTIONAL_MOMENTUM
                    else scores[item],
                    item,
                ),
            )
            selected_names = [
                item
                for item in ordered
                if (
                    scores[item] > 0
                    if family is CanonicalBaselineFamily.CROSS_SECTIONAL_MOMENTUM
                    else scores[item] < 0
                )
            ][:count]
            targets = _equal_weights(selected_names, all_instruments=instruments)
            reasons = {
                instrument: (
                    ("SELECTED_CROSS_SECTIONAL_QUARTILE",)
                    if instrument in selected_names
                    else ("OUTSIDE_SELECTED_QUARTILE",)
                )
                for instrument in instruments
            }
        else:
            targets = dict(current_weights)
            reasons = {instrument: ("RETAIN_PREVIOUS_WEIGHT",) for instrument in instruments}
    elif family is CanonicalBaselineFamily.PAIRS_DISTANCE_MEAN_REVERSION:
        targets, scores, reasons = _pairs_targets(
            request=request,
            times=times,
            selected=selected,
            index=index,
            current_weights=current_weights,
        )
    elif family is CanonicalBaselineFamily.VOLATILITY_TARGETED_LONG_ONLY:
        should_rebalance = index >= 20 and index % 5 == 0
        if should_rebalance:
            returns = [
                sum(
                    (
                        _trailing_return(selected, times, instrument, item, 1)
                        for instrument in instruments
                    ),
                    Decimal(0),
                )
                / Decimal(len(instruments))
                for item in range(index - 19, index + 1)
            ]
            annualized_volatility = _standard_deviation(returns) * Decimal(252).sqrt()
            exposure = (
                min(Decimal(1), Decimal("0.10") / annualized_volatility)
                if annualized_volatility > 0
                else Decimal(1)
            )
            per_instrument = exposure / Decimal(len(instruments))
            targets = {instrument: per_instrument for instrument in instruments}
            scores = {instrument: annualized_volatility for instrument in instruments}
            reasons = {
                instrument: ("VOLATILITY_TARGET_CAPPED_LONG_EXPOSURE",)
                for instrument in instruments
            }
        else:
            targets = dict(current_weights)
            reasons = {instrument: ("RETAIN_PREVIOUS_WEIGHT",) for instrument in instruments}
    if sum(targets.values(), Decimal(0)) > Decimal(1):
        raise SecuritiesBaselineError("long-only research exposure exceeds one")
    moment = times[index]
    session_signals: list[BaselineSignalEvidence] = []
    for instrument in instruments:
        direction = (
            SecuritiesSignalDirection.LONG
            if targets[instrument] > 0
            else SecuritiesSignalDirection.FLAT
        )
        if (
            family is CanonicalBaselineFamily.PAIRS_DISTANCE_MEAN_REVERSION
            and "ANALYTICAL_SHORT_STRONG_LEG" in reasons[instrument]
        ):
            direction = SecuritiesSignalDirection.SHORT
        source_start = max(0, index - _lookback(family))
        source_bars = tuple(
            selected[times[item]][instrument].bar_id for item in range(source_start, index + 1)
        )
        payload = {
            "instrument_id": instrument,
            "decision_at": moment,
            "direction": direction,
            "score": _metric(scores[instrument]),
            "target_weight": _metric(targets[instrument]),
            "source_bar_ids": source_bars,
            "reason_codes": reasons[instrument],
        }
        session_signals.append(
            BaselineSignalEvidence(
                instrument_id=instrument,
                decision_at=moment,
                direction=direction,
                score=_metric(scores[instrument]),
                target_weight=_metric(targets[instrument]),
                source_bar_ids=source_bars,
                reason_codes=reasons[instrument],
                canonical_hash=content_digest(payload),
            )
        )
    return ({item: _metric(value) for item, value in targets.items()}, tuple(session_signals))


def _pairs_targets(
    *,
    request: CanonicalSecuritiesSimulationRequest,
    times: Sequence[datetime],
    selected: Mapping[datetime, Mapping[str, SecuritiesBarEvidence]],
    index: int,
    current_weights: Mapping[str, Decimal],
) -> tuple[
    dict[str, Decimal],
    dict[str, Decimal],
    dict[str, tuple[str, ...]],
]:
    instruments = request.universe
    if len(instruments) % 2:
        raise SecuritiesBaselineError("pairs baseline requires an even explicit universe")
    training_sessions = 60
    targets = {instrument: Decimal(0) for instrument in instruments}
    scores = {instrument: Decimal(0) for instrument in instruments}
    reasons: dict[str, tuple[str, ...]] = {
        instrument: ("PAIRS_TRAINING_PARTITION",) for instrument in instruments
    }
    if index < training_sessions:
        return targets, scores, reasons
    pair_count = len(instruments) // 2
    for offset in range(0, len(instruments), 2):
        first, second = instruments[offset : offset + 2]
        training_spreads = []
        first_base = selected[times[0]][first].close
        second_base = selected[times[0]][second].close
        for item in range(training_sessions):
            training_spreads.append(
                selected[times[item]][first].close / first_base
                - selected[times[item]][second].close / second_base
            )
        mean = sum(training_spreads, Decimal(0)) / Decimal(len(training_spreads))
        standard_deviation = _standard_deviation(training_spreads)
        current_spread = (
            selected[times[index]][first].close / first_base
            - selected[times[index]][second].close / second_base
        )
        z_score = (
            (current_spread - mean) / standard_deviation if standard_deviation > 0 else Decimal(0)
        )
        scores[first] = z_score
        scores[second] = -z_score
        if abs(z_score) >= Decimal(1):
            weak, strong = (second, first) if z_score > 0 else (first, second)
            targets[weak] = Decimal(1) / Decimal(pair_count)
            targets[strong] = Decimal(0)
            reasons[weak] = ("LONG_WEAK_LEG_TRAINING_NORMALIZED_DISTANCE",)
            reasons[strong] = ("ANALYTICAL_SHORT_STRONG_LEG",)
        elif abs(z_score) <= Decimal("0.25"):
            reasons[first] = reasons[second] = ("PAIR_EXIT_Z_THRESHOLD",)
        else:
            targets[first] = current_weights[first]
            targets[second] = current_weights[second]
            reasons[first] = reasons[second] = ("PAIR_HOLD_BETWEEN_THRESHOLDS",)
    total = sum(targets.values(), Decimal(0))
    if total > 1:
        targets = {item: value / total for item, value in targets.items()}
    return targets, scores, reasons


def _aligned_bars(
    *,
    request: CanonicalSecuritiesSimulationRequest,
    baseline: CanonicalSecuritiesBaseline,
    bars: Sequence[SecuritiesBarEvidence],
) -> dict[datetime, dict[str, SecuritiesBarEvidence]]:
    selected: dict[datetime, dict[str, SecuritiesBarEvidence]] = defaultdict(dict)
    for bar in bars:
        if (
            bar.instrument_id in request.universe
            and bar.interval is baseline.required_interval
            and bar.adjustment_state is request.price_adjustment_state
            and request.start_date <= bar.period_end.date().isoformat() <= request.end_date
            and bar.available_at <= request.cutoff
        ):
            selected[bar.period_end][bar.instrument_id] = bar
    aligned = {
        moment: values
        for moment, values in selected.items()
        if set(values) == set(request.universe)
    }
    minimum = 120 if baseline.family is CanonicalBaselineFamily.PAIRS_DISTANCE_MEAN_REVERSION else 2
    if len(aligned) < minimum:
        raise SecuritiesBaselineError("insufficient exactly aligned point-in-time rows")
    if any(
        bar.available_at > request.cutoff for values in aligned.values() for bar in values.values()
    ):
        raise SecuritiesBaselineError("post-cutoff evidence entered the simulation")
    return aligned


def _corporate_action_scope(
    request: CanonicalSecuritiesSimulationRequest,
    actions: Sequence[SecuritiesCorporateActionEvidence],
) -> tuple[int, tuple[str, ...]]:
    in_scope = tuple(
        action
        for action in actions
        if action.instrument_id in request.universe
        and request.start_date <= action.effective_date <= request.end_date
        and action.available_at <= request.cutoff
    )
    warnings = ["TOTAL_RETURN_ADJUSTED_CORPORATE_ACTIONS_AUDITED_SEPARATELY"]
    if any(action.action_type is SecuritiesCorporateActionType.DELISTING for action in in_scope):
        warnings.append("DELISTING_TERMINAL_VALUE_REQUIRED")
    return len(in_scope), tuple(warnings)


def _failed_result(
    *,
    request: CanonicalSecuritiesSimulationRequest,
    simulator_identity: SecuritiesSimulatorIdentity,
    simulator_version: str,
    adapter_version: str,
    reason_codes: tuple[str, ...],
    corporate_action_count: int,
    state: SecuritiesSimulationState = SecuritiesSimulationState.FAILED,
) -> SecuritiesSimulationResult:
    return SecuritiesSimulationResult(
        simulator_identity=simulator_identity,
        simulator_version=simulator_version,
        adapter_version=adapter_version,
        mode=SecuritiesSimulationMode.RESEARCH_PORTFOLIO,
        request_hash=request.canonical_hash,
        state=state,
        signals=(),
        transactions=(),
        equity_curve=(),
        gross_return=None,
        net_return=None,
        annualized_return=None,
        volatility=None,
        maximum_drawdown=None,
        turnover=None,
        hit_rate=None,
        average_holding_period=None,
        average_exposure=None,
        cost_total=None,
        corporate_action_count=corporate_action_count,
        position_count=0,
        reason_codes=reason_codes,
        managed_result_artifact=None,
        managed_result_hash=None,
        canonical_result_hash=None,
    )


def _comparison_differences(
    reference: SecuritiesSimulationResult,
    comparison: SecuritiesSimulationResult,
    policy: SecuritiesConformanceMaterialityPolicy,
) -> dict[str, Decimal | int | None]:
    reference_signals = {(item.instrument_id, item.decision_at): item for item in reference.signals}
    comparison_signals = {
        (item.instrument_id, item.decision_at): item for item in comparison.signals
    }
    signal_keys = set(reference_signals) | set(comparison_signals)
    direction_disagreements = 0
    weight_disagreements = 0
    matched = 0
    for key in signal_keys:
        first = reference_signals.get(key)
        second = comparison_signals.get(key)
        if first is None or second is None:
            direction_disagreements += 1
            weight_disagreements += 1
            continue
        matched += first.direction is second.direction and (
            abs(first.target_weight - second.target_weight) <= policy.target_weight_tolerance
        )
        direction_disagreements += first.direction is not second.direction
        weight_disagreements += (
            abs(first.target_weight - second.target_weight) > policy.target_weight_tolerance
        )
    reference_transactions = {
        (item.instrument_id, item.transaction_at): item for item in reference.transactions
    }
    comparison_transactions = {
        (item.instrument_id, item.transaction_at): item for item in comparison.transactions
    }
    transaction_keys = set(reference_transactions) | set(comparison_transactions)
    transaction_disagreements = 0
    fill_price_disagreements = 0
    for key in transaction_keys:
        first_transaction = reference_transactions.get(key)
        second_transaction = comparison_transactions.get(key)
        if first_transaction is None or second_transaction is None:
            transaction_disagreements += 1
            fill_price_disagreements += 1
            continue
        transaction_disagreements += (
            abs(first_transaction.target_weight - second_transaction.target_weight)
            > policy.target_weight_tolerance
            or abs(first_transaction.modeled_cost - second_transaction.modeled_cost)
            > policy.pnl_tolerance
        )
        fill_price_disagreements += (
            abs(first_transaction.execution_price - second_transaction.execution_price)
            > policy.price_tolerance
        )
    reference_curve = {item.session_at: item for item in reference.equity_curve}
    comparison_curve = {item.session_at: item for item in comparison.equity_curve}
    common_times = sorted(set(reference_curve) & set(comparison_curve))
    cash_difference = (
        _metric(
            reference_curve[common_times[-1]].cash_balance
            - comparison_curve[common_times[-1]].cash_balance
        )
        if common_times
        else Decimal("999999")
    )
    return {
        "signal_agreement": _metric(
            Decimal(matched) / Decimal(len(signal_keys)) if signal_keys else Decimal(1)
        ),
        "decision_time_disagreements": len(set(reference_signals) ^ set(comparison_signals)),
        "direction_disagreements": direction_disagreements,
        "target_weight_disagreements": weight_disagreements,
        "transaction_disagreements": transaction_disagreements,
        "fill_time_disagreements": len(set(reference_transactions) ^ set(comparison_transactions)),
        "fill_price_disagreements": fill_price_disagreements,
        "corporate_action_disagreements": abs(
            reference.corporate_action_count - comparison.corporate_action_count
        ),
        "position_disagreements": abs(reference.position_count - comparison.position_count),
        "cash_difference": cash_difference,
        "realized_pnl_difference": Decimal(0),
        "unrealized_pnl_difference": Decimal(0),
        "return_difference": _difference(reference.net_return, comparison.net_return),
        "drawdown_difference": _difference(reference.maximum_drawdown, comparison.maximum_drawdown),
        "turnover_difference": _difference(reference.turnover, comparison.turnover),
    }


def _conformance_report(
    *,
    reference: SecuritiesSimulationRun,
    comparison: SecuritiesSimulationRun,
    policy: SecuritiesConformanceMaterialityPolicy,
    date_scope_hash: str,
    classification: MultiSimulatorConformanceClassification,
    reasons: tuple[str, ...],
    canonical_input_hash: str,
    differences: Mapping[str, Decimal | int | None] | None = None,
) -> SecuritiesSimulatorConformanceReport:
    values = differences or {
        "signal_agreement": Decimal(0),
        "decision_time_disagreements": 0,
        "direction_disagreements": 0,
        "target_weight_disagreements": 0,
        "transaction_disagreements": 0,
        "fill_time_disagreements": 0,
        "fill_price_disagreements": 0,
        "corporate_action_disagreements": 0,
        "position_disagreements": 0,
        "cash_difference": None,
        "realized_pnl_difference": None,
        "unrealized_pnl_difference": None,
        "return_difference": None,
        "drawdown_difference": None,
        "turnover_difference": None,
    }
    result_payload = {
        "classification": classification,
        "reason_codes": reasons,
        "differences": values,
    }
    canonical_result_hash = content_digest(result_payload)
    return SecuritiesSimulatorConformanceReport(
        report_id=deterministic_id(
            "securities_simulator_conformance",
            {
                "canonical_input_hash": canonical_input_hash,
                "canonical_result_hash": canonical_result_hash,
            },
        ),
        baseline_id=reference.baseline_id,
        baseline_hash=reference.baseline_hash,
        data_batch_hash=reference.data_batch_hash,
        universe_hash=reference.universe_hash,
        date_scope_hash=date_scope_hash,
        reference_run_id=reference.simulation_run_id,
        comparison_run_id=comparison.simulation_run_id,
        comparison_mode=comparison.mode,
        signal_agreement=_required_decimal(values, "signal_agreement"),
        decision_time_disagreements=_required_int(values, "decision_time_disagreements"),
        direction_disagreements=_required_int(values, "direction_disagreements"),
        target_weight_disagreements=_required_int(values, "target_weight_disagreements"),
        transaction_disagreements=_required_int(values, "transaction_disagreements"),
        fill_time_disagreements=_required_int(values, "fill_time_disagreements"),
        fill_price_disagreements=_required_int(values, "fill_price_disagreements"),
        corporate_action_disagreements=_required_int(values, "corporate_action_disagreements"),
        position_disagreements=_required_int(values, "position_disagreements"),
        cash_difference=_optional_decimal(values, "cash_difference"),
        realized_pnl_difference=_optional_decimal(values, "realized_pnl_difference"),
        unrealized_pnl_difference=_optional_decimal(values, "unrealized_pnl_difference"),
        return_difference=_optional_decimal(values, "return_difference"),
        drawdown_difference=_optional_decimal(values, "drawdown_difference"),
        turnover_difference=_optional_decimal(values, "turnover_difference"),
        materiality_policy_id=policy.policy_id,
        materiality_policy_hash=policy.canonical_hash,
        classification=classification,
        reason_codes=reasons,
        canonical_input_hash=canonical_input_hash,
        canonical_result_hash=canonical_result_hash,
    )


def _required_decimal(
    values: Mapping[str, Decimal | int | None],
    key: str,
) -> Decimal:
    value = values[key]
    if not isinstance(value, Decimal):
        raise SecuritiesBaselineError(f"{key} is not a Decimal conformance value")
    return value


def _optional_decimal(
    values: Mapping[str, Decimal | int | None],
    key: str,
) -> Decimal | None:
    value = values[key]
    if value is not None and not isinstance(value, Decimal):
        raise SecuritiesBaselineError(f"{key} is not an optional Decimal value")
    return value


def _required_int(
    values: Mapping[str, Decimal | int | None],
    key: str,
) -> int:
    value = values[key]
    if not isinstance(value, int):
        raise SecuritiesBaselineError(f"{key} is not an integer conformance value")
    return value


def _equal_weights(
    selected: Sequence[str],
    *,
    all_instruments: Sequence[str] | None = None,
) -> dict[str, Decimal]:
    universe = tuple(all_instruments or selected)
    selected_set = frozenset(selected)
    value = Decimal(1) / Decimal(len(selected_set)) if selected_set else Decimal(0)
    return {
        instrument: value if instrument in selected_set else Decimal(0) for instrument in universe
    }


def _trailing_return(
    selected: Mapping[datetime, Mapping[str, SecuritiesBarEvidence]],
    times: Sequence[datetime],
    instrument: str,
    index: int,
    lookback: int,
) -> Decimal:
    first = selected[times[index - lookback]][instrument].close
    last = selected[times[index]][instrument].close
    return last / first - Decimal(1)


def _lookback(family: CanonicalBaselineFamily) -> int:
    return {
        CanonicalBaselineFamily.CASH: 0,
        CanonicalBaselineFamily.BUY_AND_HOLD: 0,
        CanonicalBaselineFamily.EQUAL_WEIGHT_REBALANCE: 0,
        CanonicalBaselineFamily.TIME_SERIES_MOMENTUM: 20,
        CanonicalBaselineFamily.CROSS_SECTIONAL_MOMENTUM: 20,
        CanonicalBaselineFamily.SHORT_HORIZON_MEAN_REVERSION: 5,
        CanonicalBaselineFamily.PAIRS_DISTANCE_MEAN_REVERSION: 60,
        CanonicalBaselineFamily.VOLATILITY_TARGETED_LONG_ONLY: 20,
    }[family]


def _group_signals(
    signals: Sequence[BaselineSignalEvidence],
) -> dict[datetime, list[BaselineSignalEvidence]]:
    values: dict[datetime, list[BaselineSignalEvidence]] = defaultdict(list)
    for signal in signals:
        values[signal.decision_at].append(signal)
    return values


def _standard_deviation(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        return Decimal(0)
    mean = sum(values, Decimal(0)) / Decimal(len(values))
    variance = sum(((item - mean) ** 2 for item in values), Decimal(0)) / Decimal(len(values) - 1)
    with localcontext() as context:
        context.prec = 38
        return variance.sqrt()


def _difference(first: Decimal | None, second: Decimal | None) -> Decimal:
    if first is None or second is None:
        return Decimal("999999")
    return _metric(first - second)


def _metric(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 38
        return parse_decimal(
            value.quantize(Decimal("0.00000001")),
            allow_negative=True,
        )


def _iso_range(start_date: str, end_date: str) -> None:
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as error:
        raise SecuritiesBaselineError("simulation dates must be ISO dates") from error
    if start > end:
        raise SecuritiesBaselineError("simulation start follows end")


def _aware(value: datetime, label: str) -> None:
    try:
        canonical_timestamp(value)
    except ValueError as error:
        raise SecuritiesBaselineError(f"{label} must be timezone-aware") from error
