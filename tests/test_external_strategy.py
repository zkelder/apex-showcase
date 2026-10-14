from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.crypto_campaign import FIXED_HOLDOUT_CAPTURE_ID
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.external_strategy import (
    EXTERNAL_STRATEGY_SPECIFICATION_V1,
    ExternalStrategyEvaluationError,
    ExternalStrategyQualification,
    ExternalStrategySpecificationError,
    build_external_strategy_evaluation_record,
    classify_qualification,
    ensure_capture_eligible,
    point_in_time_candle_window,
    strategy_definition_from_specification,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.strategy_benchmark import simulate_strategy_backtest
from apex.domain.strategy_definition import (
    MINIMUM_PROMOTION_SAMPLE_SIZE,
    StrategyLifecycleState,
    StrategyRuleKind,
    StrategySourceKind,
)
from apex.domain.strategy_evaluation import (
    StrategyEvaluationError,
    evaluate_external_strategy,
    promote_to_paper_eligible,
)

NOW = datetime(2026, 8, 25, tzinfo=UTC)
_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


def _spec(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "specification_version": EXTERNAL_STRATEGY_SPECIFICATION_V1,
        "display_name": "20/50-Day Moving Average Crossover (Manual Demo)",
        "venue": "CRYPTO",
        "instrument_id": "BTC-USD",
        "rule_kind": "MOVING_AVERAGE_TREND",
        "rule_parameters": {"fast_window_days": 20, "slow_window_days": 50},
        "reference_description": "A well-known short/medium-term moving-average crossover rule.",
        "origin_note": (
            "Manually transcribed by an operator; not sourced from one specific publication."
        ),
    }
    base.update(overrides)
    return base


def _candles(prices: list[str]) -> tuple[CryptoDailyCandle, ...]:
    # CryptoDailyCandle.create() always hardcodes product_id=COINBASE_PRODUCT
    # ("BTC-USD") -- there is no genuine way to construct a non-BTC-USD
    # candle through it (matches today's retained evidence: every real
    # crypto_daily_candle row is BTC-USD). See _other_instrument_candle
    # below for the one test that needs a differently-tagged candle.
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


def _other_instrument_candle(offset: int, price: str, *, product_id: str) -> CryptoDailyCandle:
    """Constructs a candle directly (bypassing ``.create()``'s hardcoded
    ``COINBASE_PRODUCT``) purely to exercise ``point_in_time_candle_
    window``'s own instrument filter -- not a claim that Apex can genuinely
    capture a non-BTC-USD daily candle today."""

    base = _candles([price])[0]
    from dataclasses import replace

    return replace(
        base,
        product_id=product_id,
        bucket_start=_EPOCH + timedelta(days=offset),
        candle_id=f"{base.candle_id}-{product_id}",
    )


class TestStrategyDefinitionFromSpecification:
    def test_accepts_a_valid_moving_average_specification(self) -> None:
        definition = strategy_definition_from_specification(_spec(), created_at=NOW)
        assert definition.source_kind is StrategySourceKind.EXTERNAL
        assert definition.rule_kind is StrategyRuleKind.MOVING_AVERAGE_TREND
        assert definition.instrument_ids == ("BTC-USD",)
        assert definition.lifecycle_state is StrategyLifecycleState.RESEARCH_ONLY
        assert definition.version == 1
        assert definition.evidence_requirements.minimum_history_days == 51
        assert definition.evidence_requirements.requires_contemporaneous_observation is False

    def test_accepts_buy_and_hold_with_empty_rule_parameters(self) -> None:
        definition = strategy_definition_from_specification(
            _spec(rule_kind="BUY_AND_HOLD", rule_parameters={}), created_at=NOW
        )
        assert definition.rule_kind is StrategyRuleKind.BUY_AND_HOLD
        assert definition.evidence_requirements.minimum_history_days == 1

    def test_accepts_time_series_momentum_with_decimal_threshold_as_string(self) -> None:
        definition = strategy_definition_from_specification(
            _spec(
                rule_kind="TIME_SERIES_MOMENTUM",
                rule_parameters={
                    "momentum_lookback_days": 30,
                    "momentum_entry_threshold": "0.02",
                    "maximum_holding_days": 45,
                },
            ),
            created_at=NOW,
        )
        assert definition.rule_parameters.momentum_entry_threshold == Decimal("0.02")
        assert definition.evidence_requirements.minimum_history_days == 31

    def test_accepts_mean_reversion_with_negative_decimal_threshold(self) -> None:
        definition = strategy_definition_from_specification(
            _spec(
                rule_kind="MEAN_REVERSION",
                rule_parameters={
                    "reversion_lookback_days": 10,
                    "reversion_entry_threshold": "-0.03",
                    "maximum_holding_days": 15,
                },
            ),
            created_at=NOW,
        )
        assert definition.rule_parameters.reversion_entry_threshold == Decimal("-0.03")

    def test_bounded_horizon_when_maximum_holding_days_set(self) -> None:
        definition = strategy_definition_from_specification(
            _spec(
                rule_kind="TIME_SERIES_MOMENTUM",
                rule_parameters={
                    "momentum_lookback_days": 30,
                    "momentum_entry_threshold": "0",
                    "maximum_holding_days": 45,
                },
            ),
            created_at=NOW,
        )
        assert definition.horizon_kind.value == "DAILY_SIGNAL_BOUNDED_HOLD"

    def test_indefinite_horizon_when_no_maximum_holding_days(self) -> None:
        definition = strategy_definition_from_specification(_spec(), created_at=NOW)
        assert definition.horizon_kind.value == "DAILY_SIGNAL_INDEFINITE_HOLD"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"specification_version": "SOME_OTHER_VERSION"},
            {"display_name": ""},
            {"venue": "KALSHI"},
            {"venue": "NOT_A_VENUE"},
            {"rule_kind": "NOT_A_RULE_KIND"},
            {"rule_kind": "INTERNAL_METHOD_REFERENCE"},
            {"reference_description": ""},
            {"origin_note": "   "},
        ],
    )
    def test_rejects_malformed_or_unsupported_top_level_fields(
        self, overrides: dict[str, object]
    ) -> None:
        with pytest.raises(ExternalStrategySpecificationError):
            strategy_definition_from_specification(_spec(**overrides), created_at=NOW)

    def test_rejects_missing_required_key(self) -> None:
        spec = _spec()
        del spec["display_name"]
        with pytest.raises(ExternalStrategySpecificationError):
            strategy_definition_from_specification(spec, created_at=NOW)

    def test_rejects_rule_parameters_not_an_object(self) -> None:
        with pytest.raises(ExternalStrategySpecificationError):
            strategy_definition_from_specification(_spec(rule_parameters="oops"), created_at=NOW)

    def test_rejects_unknown_rule_parameter_key(self) -> None:
        with pytest.raises(ExternalStrategySpecificationError):
            strategy_definition_from_specification(
                _spec(rule_parameters={"fast_window_days": 20, "slow_window_days": 50, "extra": 1}),
                created_at=NOW,
            )

    def test_rejects_missing_required_rule_parameter(self) -> None:
        with pytest.raises(ExternalStrategySpecificationError):
            strategy_definition_from_specification(
                _spec(rule_parameters={"fast_window_days": 20}), created_at=NOW
            )

    def test_rejects_binary_float_for_decimal_field(self) -> None:
        with pytest.raises(ExternalStrategySpecificationError):
            strategy_definition_from_specification(
                _spec(
                    rule_kind="TIME_SERIES_MOMENTUM",
                    rule_parameters={
                        "momentum_lookback_days": 30,
                        "momentum_entry_threshold": 0.02,  # a real JSON number, not a string
                    },
                ),
                created_at=NOW,
            )

    def test_rejects_bool_for_integer_field(self) -> None:
        with pytest.raises(ExternalStrategySpecificationError):
            strategy_definition_from_specification(
                _spec(rule_parameters={"fast_window_days": True, "slow_window_days": 50}),
                created_at=NOW,
            )

    def test_rejects_impossible_parameter_values_via_underlying_domain_rule(self) -> None:
        # fast_window_days must be strictly less than slow_window_days --
        # this module never re-validates that itself; it must fall through
        # honestly to StrategyDefinition.create's own rejection.
        with pytest.raises(ExternalStrategySpecificationError):
            strategy_definition_from_specification(
                _spec(rule_parameters={"fast_window_days": 50, "slow_window_days": 20}),
                created_at=NOW,
            )

    def test_rejects_a_nonnegative_reversion_threshold_via_underlying_domain_rule(self) -> None:
        # reversion_entry_threshold must be strictly negative -- this
        # module's parser deliberately does not enforce sign itself (see
        # _NEGATIVE_DECIMAL_FIELDS' own comment); it must fall through
        # honestly to StrategyDefinition.create's own rejection, exactly
        # like the fast/slow-window case above.
        with pytest.raises(ExternalStrategySpecificationError):
            strategy_definition_from_specification(
                _spec(
                    rule_kind="MEAN_REVERSION",
                    rule_parameters={
                        "reversion_lookback_days": 10,
                        "reversion_entry_threshold": "0.03",
                    },
                ),
                created_at=NOW,
            )

    def test_deterministic_identity_for_identical_specification(self) -> None:
        first = strategy_definition_from_specification(_spec(), created_at=NOW)
        second = strategy_definition_from_specification(
            _spec(), created_at=NOW + timedelta(hours=3)
        )
        assert first.strategy_id == second.strategy_id
        assert first.canonical_hash == second.canonical_hash

    def test_parameter_change_produces_distinct_identity(self) -> None:
        first = strategy_definition_from_specification(_spec(), created_at=NOW)
        second = strategy_definition_from_specification(
            _spec(rule_parameters={"fast_window_days": 10, "slow_window_days": 50}),
            created_at=NOW,
        )
        assert first.strategy_id != second.strategy_id

    def test_metadata_only_change_keeps_identity_but_changes_content_hash(self) -> None:
        # display_name/provenance do not participate in strategy_id (a pure
        # function of source_kind/venue/instruments/rule shape) but DO
        # participate in canonical_hash -- an important, deliberate
        # distinction a persistence layer must treat as a genuine content
        # conflict at the same (strategy_id, version), not silently ignore.
        first = strategy_definition_from_specification(_spec(), created_at=NOW)
        second = strategy_definition_from_specification(
            _spec(display_name="A materially different display name"), created_at=NOW
        )
        assert first.strategy_id == second.strategy_id
        assert first.canonical_hash != second.canonical_hash


class TestEnsureCaptureEligible:
    def test_accepts_an_ordinary_capture_id(self) -> None:
        ensure_capture_eligible("btc-usd-genuine-2024-07-01-2026-07-01-v3")

    def test_rejects_the_sealed_holdout_capture(self) -> None:
        with pytest.raises(ExternalStrategyEvaluationError):
            ensure_capture_eligible(FIXED_HOLDOUT_CAPTURE_ID)


class TestPointInTimeCandleWindow:
    def test_excludes_a_candle_whose_day_has_not_yet_elapsed(self) -> None:
        candles = _candles(["100", "101", "102"])
        # The third candle's day ([_EPOCH+2, _EPOCH+3)) has not fully
        # elapsed by an as_of still inside that same day.
        as_of = _EPOCH + timedelta(days=2, hours=12)
        window = point_in_time_candle_window(candles, instrument_id="BTC-USD", as_of=as_of)
        assert [item.bucket_start for item in window] == [_EPOCH, _EPOCH + timedelta(days=1)]

    def test_includes_a_candle_exactly_at_its_elapsed_boundary(self) -> None:
        candles = _candles(["100", "101"])
        as_of = _EPOCH + timedelta(days=1)  # day 0's candle has *just* fully elapsed
        window = point_in_time_candle_window(candles, instrument_id="BTC-USD", as_of=as_of)
        assert [item.bucket_start for item in window] == [_EPOCH]

    def test_filters_by_instrument(self) -> None:
        btc = _candles(["100", "101"])
        eth = (
            _other_instrument_candle(0, "10", product_id="ETH-USD"),
            _other_instrument_candle(1, "11", product_id="ETH-USD"),
        )
        as_of = _EPOCH + timedelta(days=10)
        window = point_in_time_candle_window(btc + eth, instrument_id="ETH-USD", as_of=as_of)
        assert all(item.product_id == "ETH-USD" for item in window)
        assert len(window) == 2

    def test_future_candle_content_cannot_leak_into_an_earlier_evaluation(self) -> None:
        """Regression coverage for Part 10 (no look-ahead): a strategy whose
        outcome would visibly improve if it could see a later, extreme
        candle must produce the IDENTICAL result whether or not that future
        candle is present in the retained candle set at all -- proving
        ``point_in_time_candle_window`` genuinely bounds what
        ``simulate_strategy_backtest`` ever sees."""

        definition = strategy_definition_from_specification(
            _spec(rule_parameters={"fast_window_days": 3, "slow_window_days": 5}), created_at=NOW
        )
        as_of = _EPOCH + timedelta(days=10)  # only the first 10 days have "happened" yet
        truncated_prices = ["100", "101", "102", "103", "104", "105", "106", "107", "108", "109"]
        baseline_candles = _candles(truncated_prices)
        baseline_window = point_in_time_candle_window(
            baseline_candles, instrument_id="BTC-USD", as_of=as_of
        )
        baseline_backtest = simulate_strategy_backtest(
            definition,
            baseline_window,
            evidence_class=EvidenceClass.FIXTURE,
            evidence_provenance="test-capture",
        )

        # Now append a wildly different future price series -- if this leaked
        # into the signal/backtest computation for the first 10 days, the
        # result below would differ from the baseline above.
        leaking_prices = [*truncated_prices, "1", "1", "1", "5000", "5000"]
        leaking_candles = _candles(leaking_prices)
        leaking_window = point_in_time_candle_window(
            leaking_candles, instrument_id="BTC-USD", as_of=as_of
        )
        leaking_backtest = simulate_strategy_backtest(
            definition,
            leaking_window,
            evidence_class=EvidenceClass.FIXTURE,
            evidence_provenance="test-capture",
        )

        assert leaking_window == baseline_window
        assert leaking_backtest.canonical_hash == baseline_backtest.canonical_hash
        assert leaking_backtest.net_return == baseline_backtest.net_return
        assert (
            leaking_backtest.window_end
            == baseline_backtest.window_end
            == (_EPOCH + timedelta(days=9))
        )


class TestClassifyQualification:
    def _evaluation(self, *, sample_size: int, net_return: Decimal | None):
        definition = strategy_definition_from_specification(_spec(), created_at=NOW)
        prices = [str(100 + i) for i in range(60)]
        candles = _candles(prices)
        backtest = simulate_strategy_backtest(
            definition, candles, evidence_class=EvidenceClass.FIXTURE, evidence_provenance="c"
        )
        evaluation = evaluate_external_strategy(definition, backtest)
        # sample_size/net_return are overridden directly for boundary
        # testing -- the backtest's own genuine values are not material to
        # what this test is checking.
        from dataclasses import replace

        return replace(evaluation, sample_size=sample_size, net_return=net_return)

    def test_insufficient_sample_stays_research_only(self) -> None:
        evaluation = self._evaluation(
            sample_size=MINIMUM_PROMOTION_SAMPLE_SIZE - 1, net_return=Decimal("0.10")
        )
        assert (
            classify_qualification(evaluation)
            is ExternalStrategyQualification.RESEARCH_ONLY_INSUFFICIENT_SAMPLE
        )

    def test_nonpositive_net_return_stays_research_only(self) -> None:
        evaluation = self._evaluation(
            sample_size=MINIMUM_PROMOTION_SAMPLE_SIZE, net_return=Decimal("0")
        )
        assert (
            classify_qualification(evaluation)
            is ExternalStrategyQualification.RESEARCH_ONLY_NONPOSITIVE_NET_RETURN
        )

    def test_negative_net_return_stays_research_only(self) -> None:
        evaluation = self._evaluation(
            sample_size=MINIMUM_PROMOTION_SAMPLE_SIZE + 10, net_return=Decimal("-0.05")
        )
        assert (
            classify_qualification(evaluation)
            is ExternalStrategyQualification.RESEARCH_ONLY_NONPOSITIVE_NET_RETURN
        )

    def test_adequate_evidence_would_qualify(self) -> None:
        evaluation = self._evaluation(
            sample_size=MINIMUM_PROMOTION_SAMPLE_SIZE + 10, net_return=Decimal("0.05")
        )
        assert (
            classify_qualification(evaluation)
            is ExternalStrategyQualification.WOULD_QUALIFY_FOR_PAPER_ELIGIBLE
        )

    def test_agrees_with_the_real_promotion_gate_on_a_qualifying_case(self) -> None:
        """The classification must never diverge from the actual,
        evidence-gated ``promote_to_paper_eligible`` it mirrors -- proven
        directly by calling the real gate, not merely by construction."""

        definition = strategy_definition_from_specification(_spec(), created_at=NOW)
        evaluation = self._evaluation(
            sample_size=MINIMUM_PROMOTION_SAMPLE_SIZE + 10, net_return=Decimal("0.05")
        )
        assert (
            classify_qualification(evaluation)
            is ExternalStrategyQualification.WOULD_QUALIFY_FOR_PAPER_ELIGIBLE
        )
        promoted = promote_to_paper_eligible(definition, evaluation=evaluation, promoted_at=NOW)
        assert promoted.lifecycle_state.value == "PAPER_ELIGIBLE"

    def test_agrees_with_the_real_promotion_gate_on_a_rejected_case(self) -> None:
        definition = strategy_definition_from_specification(_spec(), created_at=NOW)
        evaluation = self._evaluation(
            sample_size=MINIMUM_PROMOTION_SAMPLE_SIZE, net_return=Decimal("0")
        )
        assert (
            classify_qualification(evaluation)
            is not ExternalStrategyQualification.WOULD_QUALIFY_FOR_PAPER_ELIGIBLE
        )
        with pytest.raises(StrategyEvaluationError):
            promote_to_paper_eligible(definition, evaluation=evaluation, promoted_at=NOW)


class TestBuildExternalStrategyEvaluationRecord:
    def _candidate_and_comparisons(self):
        definition = strategy_definition_from_specification(_spec(), created_at=NOW)
        prices = [str(100 + i) for i in range(60)]
        candles = _candles(prices)
        backtest = simulate_strategy_backtest(
            definition, candles, evidence_class=EvidenceClass.FIXTURE, evidence_provenance="c"
        )
        candidate = evaluate_external_strategy(definition, backtest)
        return definition, candidate

    def test_deterministic_replay_produces_the_identical_record_id(self) -> None:
        definition, candidate = self._candidate_and_comparisons()
        first = build_external_strategy_evaluation_record(
            strategy_id=definition.strategy_id,
            strategy_version=definition.version,
            as_of=NOW,
            capture_id="test-capture",
            evaluated_at=NOW,
            candidate=candidate,
            benchmark_comparisons=(),
        )
        second = build_external_strategy_evaluation_record(
            strategy_id=definition.strategy_id,
            strategy_version=definition.version,
            as_of=NOW,
            capture_id="test-capture",
            evaluated_at=NOW + timedelta(hours=1),  # evaluated_at is not part of identity
            candidate=candidate,
            benchmark_comparisons=(),
        )
        assert first.evaluation_id == second.evaluation_id
        assert first.canonical_hash == second.canonical_hash

    def test_different_as_of_produces_a_distinct_record_id(self) -> None:
        definition, candidate = self._candidate_and_comparisons()
        first = build_external_strategy_evaluation_record(
            strategy_id=definition.strategy_id,
            strategy_version=definition.version,
            as_of=NOW,
            capture_id="test-capture",
            evaluated_at=NOW,
            candidate=candidate,
            benchmark_comparisons=(),
        )
        second = build_external_strategy_evaluation_record(
            strategy_id=definition.strategy_id,
            strategy_version=definition.version,
            as_of=NOW + timedelta(days=1),
            capture_id="test-capture",
            evaluated_at=NOW,
            candidate=candidate,
            benchmark_comparisons=(),
        )
        assert first.evaluation_id != second.evaluation_id

    def test_qualification_is_populated_from_the_candidate(self) -> None:
        _definition, candidate = self._candidate_and_comparisons()
        record = build_external_strategy_evaluation_record(
            strategy_id=candidate.strategy_id,
            strategy_version=candidate.strategy_version,
            as_of=NOW,
            capture_id="test-capture",
            evaluated_at=NOW,
            candidate=candidate,
            benchmark_comparisons=(),
        )
        assert record.qualification == classify_qualification(candidate)
        assert record.authority == "NONE"
