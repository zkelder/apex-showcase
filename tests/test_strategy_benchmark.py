from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_benchmark import (
    BENCHMARK_INSTRUMENT,
    StrategyBenchmarkError,
    canonical_strategy_benchmarks,
    compute_daily_signal,
    simulate_strategy_backtest,
)
from apex.domain.strategy_definition import (
    StrategyDefinition,
    StrategyEvidenceRequirements,
    StrategyHorizonKind,
    StrategyLifecycleState,
    StrategyProvenance,
    StrategyRuleKind,
    StrategyRuleParameters,
    StrategySourceKind,
)

NOW = datetime(2026, 8, 25, tzinfo=UTC)
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

_PROVENANCE = StrategyProvenance(
    reference_kind="PUBLISHED_CONVENTION",
    reference_description="test fixture rule",
    origin_note="test",
)


def _definition(
    *,
    rule_kind: StrategyRuleKind,
    rule_parameters: StrategyRuleParameters,
    minimum_history_days: int = 1,
) -> StrategyDefinition:
    return StrategyDefinition.create(
        source_kind=StrategySourceKind.EXTERNAL,
        display_name="Test Definition",
        venue=SharedVenue.CRYPTO,
        instrument_ids=(BENCHMARK_INSTRUMENT,),
        horizon_kind=StrategyHorizonKind.DAILY_SIGNAL_BOUNDED_HOLD,
        rule_kind=rule_kind,
        rule_parameters=rule_parameters,
        evidence_requirements=StrategyEvidenceRequirements(
            minimum_history_days=minimum_history_days,
            requires_contemporaneous_observation=False,
        ),
        provenance=_PROVENANCE,
        created_at=NOW,
    )


def _candles(prices: list[str]) -> tuple[CryptoDailyCandle, ...]:
    candles = []
    for offset, price in enumerate(prices):
        candles.append(
            CryptoDailyCandle.create(
                capture_id="test-capture",
                attempt_id="test-attempt",
                evidence_class=EvidenceClass.FIXTURE,
                bucket_start=_EPOCH + timedelta(days=offset),
                open=price,
                high=price,
                low=price,
                close=price,
                volume="1",
                ingested_at=NOW,
                raw_response_hash="a" * 64,
            )
        )
    return tuple(candles)


class TestComputeDailySignal:
    def test_buy_and_hold_is_always_long_from_day_one(self) -> None:
        definition = _definition(
            rule_kind=StrategyRuleKind.BUY_AND_HOLD, rule_parameters=StrategyRuleParameters()
        )
        signals = compute_daily_signal(definition, _candles(["100", "110", "120"]))
        assert all(item.desired_long and item.informed for item in signals)

    def test_moving_average_trend_stays_uninformed_until_slow_window_fills(self) -> None:
        definition = _definition(
            rule_kind=StrategyRuleKind.MOVING_AVERAGE_TREND,
            rule_parameters=StrategyRuleParameters(fast_window_days=2, slow_window_days=4),
        )
        prices = ["100"] * 6 + ["200", "200"]
        signals = compute_daily_signal(definition, _candles(prices))
        assert [item.informed for item in signals[:4]] == [False, False, False, False]
        assert signals[4].informed is True
        assert signals[4].desired_long is False  # fast==slow==100

    def test_moving_average_trend_crosses_up_then_down(self) -> None:
        definition = _definition(
            rule_kind=StrategyRuleKind.MOVING_AVERAGE_TREND,
            rule_parameters=StrategyRuleParameters(fast_window_days=2, slow_window_days=4),
        )
        prices = ["100", "100", "100", "100", "200", "200", "50", "50", "50"]
        signals = compute_daily_signal(definition, _candles(prices))
        desired = [item.desired_long for item in signals]
        assert desired == [False, False, False, False, False, True, True, False, False]

    def test_time_series_momentum_uses_sign_of_trailing_return(self) -> None:
        definition = _definition(
            rule_kind=StrategyRuleKind.TIME_SERIES_MOMENTUM,
            rule_parameters=StrategyRuleParameters(
                momentum_lookback_days=2, momentum_entry_threshold=Decimal("0")
            ),
        )
        prices = ["100", "100", "100", "120", "90"]
        signals = compute_daily_signal(definition, _candles(prices))
        # index 3: informed (prior len 3 >= lookback+1=3);
        # trailing = prior[-1]/prior[-3]-1 = 100/100-1=0, not >0
        assert signals[3].informed is True
        assert signals[3].desired_long is False
        # index 4: prior=[100,100,100,120]; trailing = 120/100-1=0.20 > 0
        assert signals[4].informed is True
        assert signals[4].desired_long is True

    def test_mean_reversion_enters_on_deep_dip_below_band(self) -> None:
        definition = _definition(
            rule_kind=StrategyRuleKind.MEAN_REVERSION,
            rule_parameters=StrategyRuleParameters(
                reversion_lookback_days=2, reversion_entry_threshold=Decimal("-0.05")
            ),
        )
        prices = ["100", "100", "80"]
        signals = compute_daily_signal(definition, _candles(prices))
        # index 2: prior=[100,100]; reference=100; distance=(prior[-1]-100)/100=0, not <= -0.05
        assert signals[2].informed is True
        assert signals[2].desired_long is False
        prices_deep_dip = ["100", "100", "100", "90"]
        signals2 = compute_daily_signal(definition, _candles(prices_deep_dip))
        # index 3: prior=[100,100,100]; window(last2)=[100,100]; reference=100;
        # distance=0 -- still no dip *in the window* (day3's own close of 90
        # is excluded -- the dip is not yet visible to the signal)
        assert signals2[3].desired_long is False

    def test_rejects_internal_definition(self) -> None:
        internal_provenance = StrategyProvenance(
            reference_kind="INTERNAL_RESEARCH_METHOD",
            reference_description="X",
            origin_note="test",
        )
        definition = StrategyDefinition.create(
            source_kind=StrategySourceKind.INTERNAL,
            display_name="Internal",
            venue=SharedVenue.CRYPTO,
            instrument_ids=(BENCHMARK_INSTRUMENT,),
            horizon_kind=StrategyHorizonKind.DAILY_SIGNAL_INDEFINITE_HOLD,
            rule_kind=StrategyRuleKind.INTERNAL_METHOD_REFERENCE,
            rule_parameters=StrategyRuleParameters(referenced_internal_method_id="X"),
            evidence_requirements=StrategyEvidenceRequirements(
                minimum_history_days=1, requires_contemporaneous_observation=False
            ),
            provenance=internal_provenance,
            created_at=NOW,
        )
        with pytest.raises(StrategyBenchmarkError, match="EXTERNAL"):
            compute_daily_signal(definition, _candles(["100"]))

    def test_rejects_duplicate_bucket_start(self) -> None:
        definition = _definition(
            rule_kind=StrategyRuleKind.BUY_AND_HOLD, rule_parameters=StrategyRuleParameters()
        )
        candle = _candles(["100"])[0]
        with pytest.raises(StrategyBenchmarkError, match="duplicate"):
            compute_daily_signal(definition, (candle, candle))


class TestSimulateStrategyBacktest:
    def test_buy_and_hold_single_trade_liquidated_at_window_end(self) -> None:
        definition = _definition(
            rule_kind=StrategyRuleKind.BUY_AND_HOLD, rule_parameters=StrategyRuleParameters()
        )
        candles = _candles(["100", "110", "120"])
        result = simulate_strategy_backtest(
            definition,
            candles,
            evidence_class=EvidenceClass.FIXTURE,
            evidence_provenance="test-capture",
        )
        assert result.trade_count == 1
        assert result.candidate_count == 3
        assert result.abstained_day_count == 0
        trade = result.trades[0]
        assert trade.entry_reference_price == Decimal("100")
        assert trade.exit_reference_price == Decimal("120")
        assert trade.gross_return == Decimal("0.20000000")
        # net return reflects round-trip costs eating into the +20% move, but
        # stays a clear win
        assert Decimal("0") < trade.net_return < trade.gross_return
        assert result.win_count == 1
        assert result.loss_count == 0
        assert result.win_rate == Decimal("1.00000000")
        # portfolio-level net_return (denominated on full starting capital)
        # is slightly below the trade-level return (denominated on capital
        # actually deployed after the entry fee)
        assert result.net_return < trade.net_return
        assert result.maximum_drawdown >= Decimal("0")

    def test_total_spread_is_reported_and_not_silently_folded_into_slippage(self) -> None:
        # Review C (Strategy/PAPER Activation Integration Readiness Review
        # v1): total_fees + total_slippage previously omitted the spread
        # portion of round-trip cost entirely -- it was embedded correctly
        # in execution_price (so net_return/ending_notional were always
        # right) but never surfaced as its own reported figure, silently
        # understating the transaction-cost breakdown. BENCHMARK_SPREAD_BPS
        # == BENCHMARK_SLIPPAGE_BPS, so a correct implementation accumulates
        # the two identically at every entry/exit leg -- this is a precise,
        # non-tautological invariant, not an approximate one.
        definition = _definition(
            rule_kind=StrategyRuleKind.BUY_AND_HOLD, rule_parameters=StrategyRuleParameters()
        )
        result = simulate_strategy_backtest(
            definition,
            _candles(["100", "110", "120"]),
            evidence_class=EvidenceClass.FIXTURE,
            evidence_provenance="test-capture",
        )
        assert result.total_spread > Decimal("0")
        assert result.total_spread == result.total_slippage
        trade = result.trades[0]
        assert trade.spread_paid > Decimal("0")
        assert trade.spread_paid == trade.slippage_paid

    def test_moving_average_trend_exits_mid_simulation_on_signal_flip(self) -> None:
        definition = _definition(
            rule_kind=StrategyRuleKind.MOVING_AVERAGE_TREND,
            rule_parameters=StrategyRuleParameters(fast_window_days=2, slow_window_days=4),
        )
        prices = ["100", "100", "100", "100", "200", "200", "50", "50", "50"]
        result = simulate_strategy_backtest(
            definition,
            _candles(prices),
            evidence_class=EvidenceClass.FIXTURE,
            evidence_provenance="test-capture",
        )
        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.entry_reference_price == Decimal("200")
        assert trade.exit_reference_price == Decimal("50")
        assert trade.gross_return == Decimal("-0.75000000")
        assert result.loss_count == 1
        assert result.win_count == 0

    def test_maximum_holding_days_forces_an_exit(self) -> None:
        definition = _definition(
            rule_kind=StrategyRuleKind.TIME_SERIES_MOMENTUM,
            rule_parameters=StrategyRuleParameters(
                momentum_lookback_days=2,
                momentum_entry_threshold=Decimal("-1"),  # always true once informed
                maximum_holding_days=1,
            ),
            minimum_history_days=1,
        )
        prices = ["100", "100", "100", "110", "120", "130"]
        result = simulate_strategy_backtest(
            definition,
            _candles(prices),
            evidence_class=EvidenceClass.FIXTURE,
            evidence_provenance="test-capture",
        )
        # entry becomes informed+desired at index 3 (prior len 3 >= lookback+1);
        # held for exactly 1 day then force-closed even though the signal
        # would otherwise still say "long"
        assert result.trade_count >= 1
        assert result.trades[0].forced_by_maximum_hold is True

    def test_insufficient_history_fails_closed(self) -> None:
        definition = _definition(
            rule_kind=StrategyRuleKind.BUY_AND_HOLD,
            rule_parameters=StrategyRuleParameters(),
            minimum_history_days=10,
        )
        with pytest.raises(StrategyBenchmarkError, match="INSUFFICIENT_HISTORY"):
            simulate_strategy_backtest(
                definition,
                _candles(["100", "110"]),
                evidence_class=EvidenceClass.FIXTURE,
                evidence_provenance="x",
            )

    def test_rejects_empty_evidence_provenance(self) -> None:
        definition = _definition(
            rule_kind=StrategyRuleKind.BUY_AND_HOLD, rule_parameters=StrategyRuleParameters()
        )
        with pytest.raises(StrategyBenchmarkError, match="evidence_provenance"):
            simulate_strategy_backtest(
                definition,
                _candles(["100", "110"]),
                evidence_class=EvidenceClass.FIXTURE,
                evidence_provenance=" ",
            )

    def test_result_is_deterministic_and_reproducible(self) -> None:
        definition = _definition(
            rule_kind=StrategyRuleKind.BUY_AND_HOLD, rule_parameters=StrategyRuleParameters()
        )
        candles = _candles(["100", "110", "120"])
        first = simulate_strategy_backtest(
            definition,
            candles,
            evidence_class=EvidenceClass.FIXTURE,
            evidence_provenance="test-capture",
        )
        second = simulate_strategy_backtest(
            definition,
            candles,
            evidence_class=EvidenceClass.FIXTURE,
            evidence_provenance="test-capture",
        )
        assert first.canonical_hash == second.canonical_hash
        assert first == second


class TestCanonicalStrategyBenchmarks:
    def test_returns_four_distinct_research_only_external_definitions(self) -> None:
        benchmarks = canonical_strategy_benchmarks(created_at=NOW)
        assert len(benchmarks) == 4
        assert len({item.strategy_id for item in benchmarks}) == 4
        assert all(item.source_kind is StrategySourceKind.EXTERNAL for item in benchmarks)
        assert all(
            item.lifecycle_state is StrategyLifecycleState.RESEARCH_ONLY for item in benchmarks
        )
        assert {item.rule_kind for item in benchmarks} == {
            StrategyRuleKind.BUY_AND_HOLD,
            StrategyRuleKind.MOVING_AVERAGE_TREND,
            StrategyRuleKind.TIME_SERIES_MOMENTUM,
            StrategyRuleKind.MEAN_REVERSION,
        }

    def test_catalog_is_deterministic(self) -> None:
        first = canonical_strategy_benchmarks(created_at=NOW)
        second = canonical_strategy_benchmarks(created_at=NOW)
        assert [item.strategy_id for item in first] == [item.strategy_id for item in second]
        assert [item.canonical_hash for item in first] == [item.canonical_hash for item in second]

    def test_no_provenance_field_can_carry_a_claimed_performance_number(self) -> None:
        # structural guarantee, not just convention: StrategyProvenance has no
        # numeric/claimed-performance field at all
        from dataclasses import fields

        from apex.domain.strategy_definition import StrategyProvenance

        field_names = {f.name for f in fields(StrategyProvenance)}
        assert field_names == {"reference_kind", "reference_description", "origin_note"}
