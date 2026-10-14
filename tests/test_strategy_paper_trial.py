from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from apex.domain.crypto_paper import (
    CoinbaseOrderProjection,
    CryptoPaperOrder,
    CryptoPaperPreview,
    PaperEligibilityState,
    PaperOrderState,
    PaperOrderType,
    PaperPricingSource,
    PaperSide,
    evaluate_paper_eligibility,
)
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.crypto_universe import CryptoDailyObservation, CryptoUniverseSnapshot
from apex.domain.live_market_observation import (
    LiveMarketObservation,
    LiveObservationQuality,
    LiveObservationType,
    live_observation_round_id,
)
from apex.domain.performance_attribution import PaperVenue
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import EligibilityState, SharedVenue, evaluate_eligibility
from apex.domain.strategy_benchmark import canonical_strategy_benchmarks, simulate_strategy_backtest
from apex.domain.strategy_definition import StrategyDefinition, StrategySourceKind
from apex.domain.strategy_evaluation import StrategyEvaluationResult, evaluate_external_strategy
from apex.domain.strategy_paper_trial import (
    STRATEGY_TRIAL_NOTIONAL,
    StrategyPaperOrderPlacement,
    StrategyPaperTrialError,
    StrategyPaperTrialOutcome,
    build_attribution_source_from_trial,
    build_strategy_opportunity_projection,
    daily_candles_from_close_observations,
    price_evidence_from_live_observation,
    recurring_strategy_paper_trial_seed,
    reference_crypto_provider_contract,
    reference_crypto_universe_snapshot,
)

NOW = datetime(2026, 8, 25, tzinfo=UTC)
INSTRUMENT_ID = "BTC-USD"


def _external_evaluation() -> tuple[StrategyDefinition, StrategyEvaluationResult]:
    definition = canonical_strategy_benchmarks(created_at=NOW)[0]  # buy-and-hold
    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    candles = tuple(
        CryptoDailyCandle.create(
            capture_id="test-capture",
            attempt_id="test-attempt",
            evidence_class=EvidenceClass.FIXTURE,
            bucket_start=epoch + timedelta(days=offset),
            open=price,
            high=price,
            low=price,
            close=price,
            volume="1",
            ingested_at=NOW,
            raw_response_hash="a" * 64,
        )
        for offset, price in enumerate(["100", "105", "110"])
    )
    backtest = simulate_strategy_backtest(
        definition,
        candles,
        evidence_class=EvidenceClass.FIXTURE,
        evidence_provenance="test-capture",
    )
    return definition, evaluate_external_strategy(definition, backtest)


class TestReferenceConstructors:
    def test_universe_snapshot_is_deterministic_and_scoped_to_one_instrument(self) -> None:
        first = reference_crypto_universe_snapshot(instrument_id=INSTRUMENT_ID, created_at=NOW)
        second = reference_crypto_universe_snapshot(instrument_id=INSTRUMENT_ID, created_at=NOW)
        assert first.canonical_hash == second.canonical_hash
        assert first.eligible_product_ids == (INSTRUMENT_ID,)
        assert isinstance(first, CryptoUniverseSnapshot)

    def test_provider_contract_is_deterministic(self) -> None:
        first = reference_crypto_provider_contract(
            instrument_id=INSTRUMENT_ID, base_asset="BTC", created_at=NOW
        )
        second = reference_crypto_provider_contract(
            instrument_id=INSTRUMENT_ID, base_asset="BTC", created_at=NOW
        )
        assert first == second
        assert first.product_id == INSTRUMENT_ID
        assert first.base_asset == "BTC"
        assert first.quote_asset == "USD"


def _live_observation(
    *,
    price: Decimal = Decimal("100.00"),
    captured_at: datetime = NOW,
    instrument_id: str = INSTRUMENT_ID,
) -> LiveMarketObservation:
    round_id = live_observation_round_id(plan_id="test-plan", window_start=captured_at)
    return LiveMarketObservation.create(
        round_id=round_id,
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_id=instrument_id,
        observation_type=LiveObservationType.QUOTE,
        captured_at=captured_at,
        bid_price=price - Decimal("0.01"),
        ask_price=price + Decimal("0.01"),
        book_depth_ask=Decimal("1"),
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        provider_schema_version="TEST_V1",
        source_identity="TEST_SOURCE",
        quality=LiveObservationQuality.PASSED,
    )


class TestPriceEvidenceFromLiveObservation:
    # Review A (Strategy/PAPER Activation Integration Readiness Review v1):
    # a real PAPER decision must retain the exact durable LiveMarketObservation
    # observation_id that supported it, rather than only ever sourcing a fresh,
    # unanchored direct-provider capture (application.strategy_paper_trial.
    # capture_genuine_price_evidence). This class proves the conversion that
    # makes that possible without touching domain.crypto_paper's own,
    # unmodified RetainedPaperPriceEvidence schema.

    def test_embeds_the_durable_observation_id_in_the_evidence_id(self) -> None:
        observation = _live_observation(price=Decimal("100.00"))
        evidence = price_evidence_from_live_observation(observation, product_id=INSTRUMENT_ID)

        assert observation.observation_id in evidence.evidence_id
        assert evidence.price == Decimal("100.00")
        assert evidence.observed_at == observation.captured_at
        assert evidence.product_id == INSTRUMENT_ID

    def test_rejects_an_instrument_id_mismatch(self) -> None:
        observation = _live_observation(instrument_id="ETH-USD")
        with pytest.raises(StrategyPaperTrialError, match="INSTRUMENT_MISMATCH"):
            price_evidence_from_live_observation(observation, product_id=INSTRUMENT_ID)

    def test_rejects_an_observation_with_no_retained_top_of_book(self) -> None:
        round_id = live_observation_round_id(plan_id="test-plan", window_start=NOW)
        failed_observation = LiveMarketObservation.create(
            round_id=round_id,
            venue=SharedVenue.CRYPTO,
            provider="COINBASE",
            instrument_id=INSTRUMENT_ID,
            observation_type=LiveObservationType.QUOTE,
            captured_at=NOW,
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
            provider_schema_version="TEST_V1",
            source_identity="TEST_SOURCE",
            quality=LiveObservationQuality.FAILED,
            quality_reasons=("PROVIDER_OSError",),
        )
        with pytest.raises(StrategyPaperTrialError, match="MISSING_TOP_OF_BOOK"):
            price_evidence_from_live_observation(failed_observation, product_id=INSTRUMENT_ID)


class TestBuildStrategyOpportunityProjection:
    def test_rejects_missing_net_return(self) -> None:
        definition, evaluation = _external_evaluation()

        broken = replace(evaluation, net_return=None)
        with pytest.raises(StrategyPaperTrialError, match="net_return"):
            build_strategy_opportunity_projection(
                definition=definition,
                evaluation=broken,
                reference_price=Decimal("100"),
                generated_at=NOW,
                opportunity_ordinal="test-1",
            )

    def test_rejects_zero_sample_size(self) -> None:
        definition, evaluation = _external_evaluation()

        broken = replace(evaluation, sample_size=0)
        with pytest.raises(StrategyPaperTrialError, match="zero-sample"):
            build_strategy_opportunity_projection(
                definition=definition,
                evaluation=broken,
                reference_price=Decimal("100"),
                generated_at=NOW,
                opportunity_ordinal="test-1",
            )

    def test_rejects_nonpositive_reference_price(self) -> None:
        definition, evaluation = _external_evaluation()
        with pytest.raises(StrategyPaperTrialError, match="reference_price"):
            build_strategy_opportunity_projection(
                definition=definition,
                evaluation=evaluation,
                reference_price=Decimal("0"),
                generated_at=NOW,
                opportunity_ordinal="test-1",
            )

    def test_rejects_mismatched_evaluation(self) -> None:
        _definition, evaluation = _external_evaluation()
        other_definition = canonical_strategy_benchmarks(created_at=NOW)[1]
        with pytest.raises(StrategyPaperTrialError, match="does not belong"):
            build_strategy_opportunity_projection(
                definition=other_definition,
                evaluation=evaluation,
                reference_price=Decimal("100"),
                generated_at=NOW,
                opportunity_ordinal="test-1",
            )

    def test_produces_a_projection_that_clears_shared_eligibility_when_fresh(self) -> None:
        definition, evaluation = _external_evaluation()
        projection = build_strategy_opportunity_projection(
            definition=definition,
            evaluation=evaluation,
            reference_price=Decimal("100"),
            generated_at=NOW,
            opportunity_ordinal="test-1",
        )
        decision = evaluate_eligibility(projection, evaluated_at=NOW)
        assert decision.state is EligibilityState.ELIGIBLE
        assert projection.venue is SharedVenue.CRYPTO
        assert projection.required_hypothetical_capital == STRATEGY_TRIAL_NOTIONAL
        assert projection.maximum_loss == STRATEGY_TRIAL_NOTIONAL  # spot downside: can fall to zero

    def test_stale_projection_is_rejected_by_the_existing_paper_eligibility_gate(self) -> None:
        # This is the existing, unmodified crypto_paper.py gate -- proving
        # MY projection construction correctly feeds it real freshness data,
        # not that the gate itself needs testing (it already is elsewhere).
        definition, evaluation = _external_evaluation()
        projection = build_strategy_opportunity_projection(
            definition=definition,
            evaluation=evaluation,
            reference_price=Decimal("100"),
            generated_at=NOW,
            opportunity_ordinal="test-1",
        )
        far_future = NOW + timedelta(days=1)  # well past valid_until
        shared_decision = evaluate_eligibility(projection, evaluated_at=far_future)
        universe = reference_crypto_universe_snapshot(
            instrument_id=definition.instrument_ids[0], created_at=NOW
        )
        contract = reference_crypto_provider_contract(
            instrument_id=definition.instrument_ids[0], base_asset="BTC", created_at=NOW
        )
        eligibility = evaluate_paper_eligibility(
            decision_id="test-decision",
            opportunity_set_id="test-set",
            projection=projection,
            shared_decision=shared_decision,
            universe=universe,
            contract=contract,
            account=None,
            account_state=None,
            operating_cycle_enabled=True,
            evaluated_at=far_future,
        )
        assert eligibility.state is PaperEligibilityState.REJECTED
        assert "SOURCE_OPPORTUNITY_EXPIRED" in eligibility.reasons


class TestBuildAttributionSourceFromTrial:
    def test_rejects_a_trial_that_was_never_placed(self) -> None:
        definition, evaluation = _external_evaluation()
        placement = StrategyPaperOrderPlacement(
            definition_id=definition.strategy_id,
            projection_id="some-projection",
            eligibility=None,  # type: ignore[arg-type]
            intent=None,
            preview=None,
            order=None,
        )
        outcome = StrategyPaperTrialOutcome(
            placement=placement, fill=None, closure=None, reconciliation=None
        )
        with pytest.raises(StrategyPaperTrialError, match="never placed"):
            build_attribution_source_from_trial(
                definition=definition, evaluation=evaluation, outcome=outcome, materialized_at=NOW
            )


def test_internal_definition_limitation_note_records_source_kind() -> None:
    # Sanity: the limitations note on an INTERNAL-sourced trial's attribution
    # should be distinguishable from an EXTERNAL one -- exercised indirectly
    # via the source_kind branch in build_attribution_source_from_trial.
    definition, _evaluation = _external_evaluation()
    assert definition.source_kind is StrategySourceKind.EXTERNAL


def test_paper_venue_used_for_attribution_is_crypto_internal_paper() -> None:
    # Structural guard against the PaperVenue.CRYPTO typo this module's
    # first draft actually had (PaperVenue has no plain CRYPTO member).
    assert PaperVenue.CRYPTO_INTERNAL_PAPER.value == "CRYPTO_INTERNAL_PAPER"


def _minimal_placed_order(definition: StrategyDefinition) -> StrategyPaperOrderPlacement:
    """A minimal, directly-constructed (never through the real authorize ->
    construct -> preview -> submit chain) but structurally valid placed
    order/preview pair -- build_attribution_source_from_trial only ever
    reads a handful of fields off each (confirmed by inspection), so this
    mirrors this codebase's own "hand-rolled minimal factory" test
    convention (e.g. test_strategy_evaluation.py's _health_assessment())
    rather than exercising the full crypto_paper.py order lifecycle just to
    reach this one translation function."""

    provider_request = CoinbaseOrderProjection(
        client_order_id="test-order",
        product_id=INSTRUMENT_ID,
        side=PaperSide.BUY,
        order_configuration="market_market_ioc",
        quote_size="10",
        base_size=None,
        limit_price=None,
        canonical_hash="a" * 64,
    )
    order = CryptoPaperOrder(
        order_id="test-order",
        account_id="test-account",
        intent_id="test-intent",
        preview_id="test-preview",
        authorization_id="test-authorization",
        product_id=INSTRUMENT_ID,
        base_asset="BTC",
        quote_asset="USD",
        side=PaperSide.BUY,
        order_type=PaperOrderType.MARKET,
        target_base_quantity=Decimal("0.0001"),
        limit_price=None,
        filled_base_quantity=Decimal("0"),
        filled_quote_quantity=Decimal("0"),
        cumulative_fees=Decimal("0"),
        state=PaperOrderState.OPEN,
        submitted_at=NOW,
        valid_until=NOW + timedelta(hours=4),
        last_sequence=2,
        canonical_hash="b" * 64,
    )
    preview = CryptoPaperPreview(
        preview_id="test-preview",
        intent_id="test-intent",
        account_id="test-account",
        product_id=INSTRUMENT_ID,
        account_state_hash="c" * 64,
        estimated_execution_price=Decimal("100"),
        estimated_base_quantity=Decimal("0.0001"),
        estimated_quote_quantity=Decimal("10"),
        modeled_spread=Decimal("0.01"),
        modeled_slippage=Decimal("0.01"),
        modeled_fee=Decimal("0.06"),
        estimated_total_cost=Decimal("10.06"),
        estimated_remaining_cash=Decimal("989.94"),
        estimated_remaining_asset=Decimal("0.0001"),
        rejection_reasons=(),
        provider_request=provider_request,
        evidence_ids=("test-evidence",),
        pricing_source=PaperPricingSource.GENUINE_TOP_OF_BOOK,
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=300),
        canonical_hash="d" * 64,
    )
    return StrategyPaperOrderPlacement(
        definition_id=definition.strategy_id,
        projection_id="test-projection",
        eligibility=None,  # type: ignore[arg-type]  -- never read downstream (see module docstring)
        intent=None,
        preview=preview,
        order=order,
    )


class TestBuildAttributionSourceFromTrialContextLineage:
    """Recurring Strategy PAPER Orchestration v1, Phase D: the Total Market
    Context integration hook this module's own persistence test previously
    documented as deliberately unwired is now closed."""

    def test_threads_the_real_context_id_when_supplied(self) -> None:
        definition, evaluation = _external_evaluation()
        outcome = StrategyPaperTrialOutcome(
            placement=_minimal_placed_order(definition),
            fill=None,
            closure=None,
            reconciliation=None,
        )
        source = build_attribution_source_from_trial(
            definition=definition,
            evaluation=evaluation,
            outcome=outcome,
            materialized_at=NOW,
            context_at_consideration_id="total-market-context-test-1",
            consideration_context="RISK_SUPPORTIVE",
        )
        assert source.context_at_consideration_id == "total-market-context-test-1"
        assert source.consideration_context == "RISK_SUPPORTIVE"
        # Phase D scopes consideration-time context only -- execution-time
        # context remains a separate, not-yet-wired concern (mirrors the
        # older Cross-Venue-Allocation path's own identical choice).
        assert source.context_at_execution_id is None
        assert source.execution_context is None

    def test_defaults_to_none_never_fabricated(self) -> None:
        # Phase D: no eligible context this cycle must never invent one --
        # None flows straight through to performance_attribution.
        # assess_strategy_health's own honest DATA_QUALITY_BLOCKED path.
        definition, evaluation = _external_evaluation()
        outcome = StrategyPaperTrialOutcome(
            placement=_minimal_placed_order(definition),
            fill=None,
            closure=None,
            reconciliation=None,
        )
        source = build_attribution_source_from_trial(
            definition=definition, evaluation=evaluation, outcome=outcome, materialized_at=NOW
        )
        assert source.context_at_consideration_id is None
        assert source.consideration_context is None


class TestRecurringStrategyPaperTrialSeed:
    """Recurring Strategy PAPER Orchestration v1, Phase B/F: the
    cycle-independent identity anchor that lets a later cycle recognize
    "already open" without any new registration."""

    def test_deterministic_across_repeated_calls(self) -> None:
        first = recurring_strategy_paper_trial_seed(
            strategy_id="strategy_definition_abc", strategy_version=2, instrument_id="BTC-USD"
        )
        second = recurring_strategy_paper_trial_seed(
            strategy_id="strategy_definition_abc", strategy_version=2, instrument_id="BTC-USD"
        )
        assert first == second

    def test_differs_by_strategy_version_and_by_instrument(self) -> None:
        base = recurring_strategy_paper_trial_seed(
            strategy_id="strategy_definition_abc", strategy_version=2, instrument_id="BTC-USD"
        )
        different_version = recurring_strategy_paper_trial_seed(
            strategy_id="strategy_definition_abc", strategy_version=3, instrument_id="BTC-USD"
        )
        different_instrument = recurring_strategy_paper_trial_seed(
            strategy_id="strategy_definition_abc", strategy_version=2, instrument_id="ETH-USD"
        )
        different_strategy = recurring_strategy_paper_trial_seed(
            strategy_id="strategy_definition_xyz", strategy_version=2, instrument_id="BTC-USD"
        )
        assert len({base, different_version, different_instrument, different_strategy}) == 4

    def test_never_derived_from_a_cycle_id(self) -> None:
        # The function signature itself has no cycle_id/timestamp parameter
        # at all -- this is a structural guard, not merely a behavioral one.
        import inspect

        parameters = inspect.signature(recurring_strategy_paper_trial_seed).parameters
        assert "cycle_id" not in parameters
        assert "as_of" not in parameters
        assert "executed_at" not in parameters


class TestDailyCandlesFromCloseObservations:
    """Recurring Strategy PAPER Orchestration v1, Phase C: genuine,
    retained daily closes (no open/high/low in this pipeline) -> the
    OHLC-shaped input domain.strategy_benchmark.simulate_strategy_backtest
    already expects, honestly (see the function's own docstring)."""

    def test_derives_close_only_ohlc_candles(self) -> None:
        epoch = datetime(2026, 1, 1, tzinfo=UTC)
        observations = tuple(
            CryptoDailyObservation.create(
                bucket_start=epoch + timedelta(days=offset), close=price, volume="1"
            )
            for offset, price in enumerate(["100", "105", "110"])
        )
        candles = daily_candles_from_close_observations(
            observations,
            source_observation_id="test-observation",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            ingested_at=NOW,
        )
        assert len(candles) == 3
        assert [item.close for item in candles] == [Decimal("100"), Decimal("105"), Decimal("110")]
        assert all(item.open == item.high == item.low == item.close for item in candles)
        assert all(item.evidence_class is EvidenceClass.HISTORICAL_PUBLIC for item in candles)
        assert candles[0].bucket_start == epoch

    def test_never_upgrades_or_downgrades_evidence_class(self) -> None:
        observations = (CryptoDailyObservation.create(bucket_start=NOW, close="100", volume="1"),)
        candles = daily_candles_from_close_observations(
            observations,
            source_observation_id="test-observation",
            evidence_class=EvidenceClass.FIXTURE,
            ingested_at=NOW,
        )
        assert candles[0].evidence_class is EvidenceClass.FIXTURE

    def test_deterministic_across_repeated_calls(self) -> None:
        observations = (CryptoDailyObservation.create(bucket_start=NOW, close="100", volume="1"),)
        first = daily_candles_from_close_observations(
            observations,
            source_observation_id="test-observation",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            ingested_at=NOW,
        )
        second = daily_candles_from_close_observations(
            observations,
            source_observation_id="test-observation",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            ingested_at=NOW,
        )
        assert first[0].candle_id == second[0].candle_id
        assert first[0].normalized_hash == second[0].normalized_hash

    def test_produces_input_the_real_simulator_accepts_and_signals_correctly(self) -> None:
        # Integration-shaped: feed the derived candles straight into the
        # real, unmodified simulator and confirm it runs and produces a
        # genuine, non-fixture-labeled result end to end.
        definition = canonical_strategy_benchmarks(created_at=NOW)[0]  # buy-and-hold
        epoch = datetime(2026, 1, 1, tzinfo=UTC)
        observations = tuple(
            CryptoDailyObservation.create(
                bucket_start=epoch + timedelta(days=offset), close=price, volume="1"
            )
            for offset, price in enumerate(["100", "105", "110", "108", "112"])
        )
        candles = daily_candles_from_close_observations(
            observations,
            source_observation_id="test-observation",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            ingested_at=NOW,
        )
        backtest = simulate_strategy_backtest(
            definition,
            candles,
            evidence_class=candles[0].evidence_class,
            evidence_provenance=candles[0].capture_id,
        )
        assert backtest.evidence_class is EvidenceClass.HISTORICAL_PUBLIC
        assert backtest.sample_size == 5
        assert backtest.net_return is not None


class TestAttributionClosureSemantics:
    """PAPER Trial Closure Readiness v1 (F3/F4)."""

    def test_expired_sell_is_never_attributed_closed(self) -> None:
        definition, evaluation = _external_evaluation()
        placed = _minimal_placed_order(definition)
        assert placed.order is not None
        expired_sell = replace(placed.order, side=PaperSide.SELL, state=PaperOrderState.EXPIRED)
        outcome = StrategyPaperTrialOutcome(
            placement=replace(placed, order=expired_sell),
            fill=None,
            closure=cast(Any, SimpleNamespace()),  # a CryptoPaperCancellation exists
            reconciliation=None,
        )
        source = build_attribution_source_from_trial(
            definition=definition, evaluation=evaluation, outcome=outcome, materialized_at=NOW
        )
        assert source.position_state == "EXPIRED"
        assert source.completed_at is None
        assert source.open_exposure is None
        assert source.source_hash == expired_sell.canonical_hash

    def test_expired_unfilled_buy_is_still_terminal(self) -> None:
        definition, evaluation = _external_evaluation()
        placed = _minimal_placed_order(definition)
        assert placed.order is not None
        expired_buy = replace(placed.order, state=PaperOrderState.EXPIRED)
        outcome = StrategyPaperTrialOutcome(
            placement=replace(placed, order=expired_buy),
            fill=None,
            closure=cast(Any, SimpleNamespace()),
            reconciliation=None,
        )
        source = build_attribution_source_from_trial(
            definition=definition, evaluation=evaluation, outcome=outcome, materialized_at=NOW
        )
        assert source.position_state == "CLOSED"

    def test_position_closed_supersedes_with_a_distinct_source_hash(self) -> None:
        definition, evaluation = _external_evaluation()
        placed = _minimal_placed_order(definition)
        assert placed.order is not None
        outcome = StrategyPaperTrialOutcome(
            placement=placed, fill=None, closure=None, reconciliation=None
        )
        open_source = build_attribution_source_from_trial(
            definition=definition, evaluation=evaluation, outcome=outcome, materialized_at=NOW
        )
        closed_at = NOW + timedelta(days=31)
        closed_source = build_attribution_source_from_trial(
            definition=definition,
            evaluation=evaluation,
            outcome=outcome,
            materialized_at=NOW,
            position_closed=True,
            closed_at=closed_at,
        )
        assert open_source.source_hash == placed.order.canonical_hash  # no churn
        assert closed_source.source_id == open_source.source_id
        assert closed_source.source_hash != open_source.source_hash
        assert closed_source.position_state == "CLOSED"
        assert closed_source.completed_at == closed_at
        assert closed_source.open_exposure is None
