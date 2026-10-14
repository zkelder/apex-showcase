"""Deterministic EXTERNAL benchmark strategies and their historical simulator.

Four simple, well-understood, publicly-conventional strategies -- buy-and-
hold, a dual moving-average trend rule, time-series momentum, and a mean-
reversion rule -- are defined here as ``StrategyDefinition`` records with
``source_kind=EXTERNAL``. Their parameters are round, conventional numbers
picked before any backtest ran (50/200-day "golden/death cross"; 90-day
momentum, matching the lookback Apex's own existing crypto reversal-regime
research already uses elsewhere; a symmetric 5% mean-reversion band) --
never tuned to this module's own results. They are BENCHMARKS: evidence for
comparison, never a claim of profitable edge (see
``STRATEGY_BENCHMARK_WARNING``).

``simulate_strategy_backtest`` is the one deterministic long-or-cash
simulator all four share, over genuinely retained ``CryptoDailyCandle``
history. It reuses the exact conservative cost assumptions Apex's own live
CRYPTO PAPER engine uses (``adapters/operating_cycle_tasks.py``'s
``CONSERVATIVE_CRYPTO_FEE_BPS``/``_SPREAD_BPS``/``_SLIPPAGE_BPS``) so a
benchmark's backtested economics and a genuine PAPER fill are directly
comparable, not separately tuned assumptions.

Every signal and every simulated trade uses only information strictly prior
to the decision day (the decision day's own close is always excluded --
matching the "excludes the decision close" convention already used
throughout Apex's crypto research modules) -- there is no lookahead.

Authority ``NONE`` throughout. This module creates no Opportunity, Order,
Position, PAPER trial, or LIVE authority; see
``apex.domain.strategy_evaluation`` for how a backtest result becomes a
comparable ``StrategyEvaluationResult``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import median as _median_of

from apex.domain.crypto_research import RESEARCH_METRIC_QUANTUM, CryptoDailyCandle
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_definition import (
    StrategyDefinition,
    StrategyEvidenceRequirements,
    StrategyHorizonKind,
    StrategyProvenance,
    StrategyRuleKind,
    StrategyRuleParameters,
    StrategySourceKind,
)
from apex.domain.values import content_digest

STRATEGY_BENCHMARK_WARNING = "STRATEGY BENCHMARK -- RESEARCH COMPARISON ONLY, NOT A PROFIT CLAIM"

BENCHMARK_INSTRUMENT = "BTC-USD"

# Identical to adapters/operating_cycle_tasks.py's CONSERVATIVE_CRYPTO_FEE_BPS
# / _SPREAD_BPS / _SLIPPAGE_BPS -- deliberately the same numbers, not a
# separately tuned research assumption, so a benchmark backtest and a
# genuine crypto PAPER fill share one conservative cost model.
BENCHMARK_FEE_BPS = Decimal("60")
BENCHMARK_SPREAD_BPS = Decimal("10")
BENCHMARK_SLIPPAGE_BPS = Decimal("10")

_STARTING_NOTIONAL_DEFAULT = Decimal("10000")


class StrategyBenchmarkError(ValueError):
    """A benchmark signal or simulation violated a determinism/evidence invariant."""


def _metric(value: Decimal) -> Decimal:
    return value.quantize(RESEARCH_METRIC_QUANTUM)


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


@dataclass(frozen=True, slots=True)
class StrategySignal:
    bucket_start: datetime
    desired_long: bool
    informed: bool
    information_through: datetime | None
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class CompletedTrade:
    entry_at: datetime
    entry_reference_price: Decimal
    entry_execution_price: Decimal
    exit_at: datetime
    exit_reference_price: Decimal
    exit_execution_price: Decimal
    gross_return: Decimal
    net_return: Decimal
    fees_paid: Decimal
    slippage_paid: Decimal
    spread_paid: Decimal
    forced_by_maximum_hold: bool


@dataclass(frozen=True, slots=True)
class StrategyBacktestResult:
    strategy_id: str
    strategy_version: int
    rule_kind: StrategyRuleKind
    window_start: datetime
    window_end: datetime
    candidate_count: int
    abstained_day_count: int
    sample_size: int
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: Decimal | None
    average_trade_return: Decimal | None
    median_trade_return: Decimal | None
    gross_return: Decimal
    net_return: Decimal
    maximum_drawdown: Decimal
    turnover: Decimal
    total_fees: Decimal
    total_slippage: Decimal
    total_spread: Decimal
    fee_bps: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    starting_notional: Decimal
    ending_notional: Decimal
    evidence_class: EvidenceClass
    evidence_provenance: str
    trades: tuple[CompletedTrade, ...]
    canonical_hash: str


def compute_daily_signal(
    definition: StrategyDefinition, candles: tuple[CryptoDailyCandle, ...]
) -> tuple[StrategySignal, ...]:
    """Pure, lookahead-free per-day long/cash signal for one EXTERNAL
    benchmark rule. Day ``i``'s decision only ever reads ``closes[:i]`` --
    strictly prior closes, never the decision day's own close."""

    if definition.source_kind is not StrategySourceKind.EXTERNAL:
        raise StrategyBenchmarkError(
            "compute_daily_signal only supports EXTERNAL strategy definitions"
        )
    ordered = tuple(sorted(candles, key=lambda item: item.bucket_start))
    if len(ordered) != len({item.bucket_start for item in ordered}):
        raise StrategyBenchmarkError("candles must not contain duplicate bucket_start values")

    kind = definition.rule_kind
    params = definition.rule_parameters
    closes = [item.close for item in ordered]
    signals: list[StrategySignal] = []
    for index, candle in enumerate(ordered):
        prior_closes = closes[:index]
        information_through = ordered[index - 1].bucket_start if index >= 1 else None
        if kind is StrategyRuleKind.BUY_AND_HOLD:
            desired, informed = True, True
        elif kind is StrategyRuleKind.MOVING_AVERAGE_TREND:
            assert params.fast_window_days is not None
            assert params.slow_window_days is not None
            if len(prior_closes) < params.slow_window_days:
                desired, informed = False, False
            else:
                fast_avg = _mean(prior_closes[-params.fast_window_days :])
                slow_avg = _mean(prior_closes[-params.slow_window_days :])
                desired, informed = fast_avg > slow_avg, True
        elif kind is StrategyRuleKind.TIME_SERIES_MOMENTUM:
            assert params.momentum_lookback_days is not None
            assert params.momentum_entry_threshold is not None
            lookback = params.momentum_lookback_days
            if len(prior_closes) < lookback + 1:
                desired, informed = False, False
            else:
                trailing_return = prior_closes[-1] / prior_closes[-1 - lookback] - Decimal(1)
                desired, informed = trailing_return > params.momentum_entry_threshold, True
        elif kind is StrategyRuleKind.MEAN_REVERSION:
            assert params.reversion_lookback_days is not None
            assert params.reversion_entry_threshold is not None
            lookback = params.reversion_lookback_days
            if len(prior_closes) < lookback:
                desired, informed = False, False
            else:
                window = prior_closes[-lookback:]
                reference = _mean(window)
                distance = (window[-1] - reference) / reference
                desired, informed = distance <= params.reversion_entry_threshold, True
        else:
            raise StrategyBenchmarkError(
                f"unsupported rule_kind for a benchmark simulation: {kind}"
            )
        payload = (
            definition.strategy_id,
            candle.bucket_start,
            desired,
            informed,
            information_through,
        )
        signals.append(
            StrategySignal(
                bucket_start=candle.bucket_start,
                desired_long=desired,
                informed=informed,
                information_through=information_through,
                canonical_hash=content_digest(payload),
            )
        )
    return tuple(signals)


def simulate_strategy_backtest(
    definition: StrategyDefinition,
    candles: tuple[CryptoDailyCandle, ...],
    *,
    evidence_class: EvidenceClass,
    evidence_provenance: str,
    starting_notional: Decimal = _STARTING_NOTIONAL_DEFAULT,
) -> StrategyBacktestResult:
    """Simulate one EXTERNAL benchmark over genuinely retained daily candles.

    Execution is always at the *next* candle's open (never the signal day's
    own close), consistent with Apex's existing "next eligible daily open"
    convention. A position forced closed by ``maximum_holding_days`` exits
    the same way a signal-driven exit does.
    """

    if definition.source_kind is not StrategySourceKind.EXTERNAL:
        raise StrategyBenchmarkError(
            "simulate_strategy_backtest only supports EXTERNAL strategy definitions"
        )
    ordered = tuple(sorted(candles, key=lambda item: item.bucket_start))
    if len(ordered) < definition.evidence_requirements.minimum_history_days:
        raise StrategyBenchmarkError("STRATEGY_BACKTEST_INSUFFICIENT_HISTORY")
    if not evidence_provenance.strip():
        raise StrategyBenchmarkError("evidence_provenance is required")

    signals = compute_daily_signal(definition, ordered)
    max_hold = definition.rule_parameters.maximum_holding_days

    fee_rate = BENCHMARK_FEE_BPS / Decimal(10_000)
    spread_rate = BENCHMARK_SPREAD_BPS / Decimal(10_000)
    slippage_rate = BENCHMARK_SLIPPAGE_BPS / Decimal(10_000)

    cash = starting_notional
    units = Decimal(0)
    entry_at: datetime | None = None
    entry_reference_price: Decimal | None = None
    entry_execution_price: Decimal | None = None
    cost_basis: Decimal | None = None
    held_days = 0

    trades: list[CompletedTrade] = []
    marked_values: list[Decimal] = [starting_notional]
    turnover_notional = Decimal(0)
    total_fees = Decimal(0)
    total_slippage = Decimal(0)
    total_spread = Decimal(0)
    candidate_count = 0
    abstained_day_count = 0
    gross_multiplier = Decimal(1)

    def _close_position(candle: CryptoDailyCandle, *, forced: bool) -> None:
        nonlocal cash, units, entry_at, entry_reference_price, entry_execution_price
        nonlocal cost_basis, held_days, total_fees, total_slippage, total_spread, turnover_notional
        nonlocal gross_multiplier
        assert entry_at is not None
        assert entry_reference_price is not None
        assert entry_execution_price is not None
        assert cost_basis is not None
        execution_price = candle.open * (Decimal(1) - spread_rate - slippage_rate)
        gross_proceeds = units * execution_price
        fee = gross_proceeds * fee_rate
        proceeds = gross_proceeds - fee
        slippage_cost = units * candle.open * slippage_rate
        spread_cost = units * candle.open * spread_rate
        total_fees += fee
        total_slippage += slippage_cost
        total_spread += spread_cost
        turnover_notional += gross_proceeds
        gross_trade_return = _metric(candle.open / entry_reference_price - Decimal(1))
        net_trade_return = _metric(proceeds / cost_basis - Decimal(1))
        gross_multiplier *= Decimal(1) + gross_trade_return
        trades.append(
            CompletedTrade(
                entry_at=entry_at,
                entry_reference_price=entry_reference_price,
                entry_execution_price=entry_execution_price,
                exit_at=candle.bucket_start,
                exit_reference_price=candle.open,
                exit_execution_price=execution_price,
                gross_return=gross_trade_return,
                net_return=net_trade_return,
                fees_paid=_metric(fee),
                slippage_paid=_metric(slippage_cost),
                spread_paid=_metric(spread_cost),
                forced_by_maximum_hold=forced,
            )
        )
        cash = proceeds
        units = Decimal(0)
        entry_at = entry_reference_price = entry_execution_price = cost_basis = None
        held_days = 0

    for candle, signal in zip(ordered, signals, strict=True):
        candidate_count += 1
        if not signal.informed:
            abstained_day_count += 1
        desired = signal.desired_long
        forced_exit = units > 0 and max_hold is not None and held_days >= max_hold
        if forced_exit:
            desired = False

        if desired and units == 0:
            execution_price = candle.open * (Decimal(1) + spread_rate + slippage_rate)
            fee = cash * fee_rate
            spendable = cash - fee
            units = spendable / execution_price
            total_fees += fee
            total_slippage += units * candle.open * slippage_rate
            total_spread += units * candle.open * spread_rate
            turnover_notional += units * execution_price
            entry_at = candle.bucket_start
            entry_reference_price = candle.open
            entry_execution_price = execution_price
            cost_basis = spendable
            cash = Decimal(0)
            held_days = 1
        elif not desired and units > 0:
            _close_position(candle, forced=forced_exit)
        elif units > 0:
            held_days += 1
        marked_values.append(cash + units * candle.close)

    if units > 0:
        _close_position(ordered[-1], forced=False)
        marked_values[-1] = cash

    ending_notional = _metric(cash)
    net_return = _metric(ending_notional / starting_notional - Decimal(1))
    gross_return = _metric(gross_multiplier - Decimal(1))
    peak = marked_values[0]
    max_drawdown = Decimal(0)
    for value in marked_values:
        peak = max(peak, value)
        if peak:
            max_drawdown = max(max_drawdown, (peak - value) / peak)

    trade_returns = [item.net_return for item in trades]
    win_count = sum(1 for item in trade_returns if item > 0)
    loss_count = sum(1 for item in trade_returns if item < 0)
    sample_size = candidate_count - abstained_day_count

    payload = (
        definition.strategy_id,
        definition.version,
        ordered[0].bucket_start,
        ordered[-1].bucket_start,
        tuple(item.canonical_hash for item in signals),
        starting_notional,
        ending_notional,
        evidence_provenance,
    )
    return StrategyBacktestResult(
        strategy_id=definition.strategy_id,
        strategy_version=definition.version,
        rule_kind=definition.rule_kind,
        window_start=ordered[0].bucket_start,
        window_end=ordered[-1].bucket_start,
        candidate_count=candidate_count,
        abstained_day_count=abstained_day_count,
        sample_size=sample_size,
        trade_count=len(trades),
        win_count=win_count,
        loss_count=loss_count,
        win_rate=_metric(Decimal(win_count) / Decimal(len(trades))) if trades else None,
        average_trade_return=_metric(_mean(trade_returns)) if trade_returns else None,
        median_trade_return=_metric(Decimal(str(_median_of(trade_returns))))
        if trade_returns
        else None,
        gross_return=gross_return,
        net_return=net_return,
        maximum_drawdown=_metric(max_drawdown),
        turnover=_metric(turnover_notional / starting_notional),
        total_fees=_metric(total_fees),
        total_slippage=_metric(total_slippage),
        total_spread=_metric(total_spread),
        fee_bps=BENCHMARK_FEE_BPS,
        spread_bps=BENCHMARK_SPREAD_BPS,
        slippage_bps=BENCHMARK_SLIPPAGE_BPS,
        starting_notional=starting_notional,
        ending_notional=ending_notional,
        evidence_class=evidence_class,
        evidence_provenance=evidence_provenance,
        trades=tuple(trades),
        canonical_hash=content_digest(payload),
    )


def _evidence_requirements(minimum_history_days: int) -> StrategyEvidenceRequirements:
    return StrategyEvidenceRequirements(
        minimum_history_days=minimum_history_days,
        requires_contemporaneous_observation=True,
        maximum_observation_staleness_seconds=1800,
    )


def canonical_strategy_benchmarks(*, created_at: datetime) -> tuple[StrategyDefinition, ...]:
    """The reviewed catalog of EXTERNAL benchmark strategies. Deterministic
    and versioned like ``securities_baselines.canonical_securities_baselines``
    -- a fixed, code-reviewed set, never constructed ad hoc by a caller."""

    buy_and_hold = StrategyDefinition.create(
        source_kind=StrategySourceKind.EXTERNAL,
        display_name="Buy and Hold BTC-USD",
        venue=SharedVenue.CRYPTO,
        instrument_ids=(BENCHMARK_INSTRUMENT,),
        horizon_kind=StrategyHorizonKind.DAILY_SIGNAL_INDEFINITE_HOLD,
        rule_kind=StrategyRuleKind.BUY_AND_HOLD,
        rule_parameters=StrategyRuleParameters(),
        evidence_requirements=_evidence_requirements(1),
        provenance=StrategyProvenance(
            reference_kind="PUBLISHED_CONVENTION",
            reference_description="Buy-and-hold: acquire on day one and never sell.",
            origin_note=(
                "The canonical passive baseline every active strategy is conventionally "
                "measured against; not attributed to any single publication."
            ),
        ),
        created_at=created_at,
    )
    moving_average_trend = StrategyDefinition.create(
        source_kind=StrategySourceKind.EXTERNAL,
        display_name="50/200-Day Moving Average Trend (Golden/Death Cross)",
        venue=SharedVenue.CRYPTO,
        instrument_ids=(BENCHMARK_INSTRUMENT,),
        horizon_kind=StrategyHorizonKind.DAILY_SIGNAL_INDEFINITE_HOLD,
        rule_kind=StrategyRuleKind.MOVING_AVERAGE_TREND,
        rule_parameters=StrategyRuleParameters(fast_window_days=50, slow_window_days=200),
        evidence_requirements=_evidence_requirements(201),
        provenance=StrategyProvenance(
            reference_kind="PUBLISHED_CONVENTION",
            reference_description=(
                "Dual moving-average crossover: long while the 50-day average is above "
                "the 200-day average ('golden cross'), flat otherwise ('death cross')."
            ),
            origin_note=(
                "One of the most widely published technical-analysis trend rules; "
                "50/200 is its standard convention, not fit to this data."
            ),
        ),
        created_at=created_at,
    )
    time_series_momentum = StrategyDefinition.create(
        source_kind=StrategySourceKind.EXTERNAL,
        display_name="90-Day Time-Series Momentum",
        venue=SharedVenue.CRYPTO,
        instrument_ids=(BENCHMARK_INSTRUMENT,),
        horizon_kind=StrategyHorizonKind.DAILY_SIGNAL_BOUNDED_HOLD,
        rule_kind=StrategyRuleKind.TIME_SERIES_MOMENTUM,
        rule_parameters=StrategyRuleParameters(
            momentum_lookback_days=90,
            momentum_entry_threshold=Decimal("0"),
            maximum_holding_days=90,
        ),
        evidence_requirements=_evidence_requirements(91),
        provenance=StrategyProvenance(
            reference_kind="PUBLISHED_CONVENTION",
            reference_description=(
                "Time-series momentum: long while the trailing 90-day return is "
                "positive, flat otherwise (Moskowitz/Ooi/Pedersen-style convention)."
            ),
            origin_note=(
                "90 days matches the lookback Apex's own existing "
                "CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1 research already uses "
                "elsewhere for a crypto regime window -- reused for consistency, "
                "not chosen for this backtest's result."
            ),
        ),
        created_at=created_at,
    )
    mean_reversion = StrategyDefinition.create(
        source_kind=StrategySourceKind.EXTERNAL,
        display_name="20-Day Mean Reversion (5% Band)",
        venue=SharedVenue.CRYPTO,
        instrument_ids=(BENCHMARK_INSTRUMENT,),
        horizon_kind=StrategyHorizonKind.DAILY_SIGNAL_BOUNDED_HOLD,
        rule_kind=StrategyRuleKind.MEAN_REVERSION,
        rule_parameters=StrategyRuleParameters(
            reversion_lookback_days=20,
            reversion_entry_threshold=Decimal("-0.05"),
            maximum_holding_days=20,
        ),
        evidence_requirements=_evidence_requirements(21),
        provenance=StrategyProvenance(
            reference_kind="PUBLISHED_CONVENTION",
            reference_description=(
                "Simple mean reversion: long while price is at least 5% below its "
                "own trailing 20-day average, flat otherwise (a simplified Bollinger-"
                "band-style 'buy the dip' convention)."
            ),
            origin_note="A conventional, symmetric, round-number band -- not tuned to this data.",
        ),
        created_at=created_at,
    )
    return (buy_and_hold, moving_average_trend, time_series_momentum, mean_reversion)
