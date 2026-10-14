from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.options_intelligence import (
    ExerciseStyle,
    OptionContract,
    OptionType,
    SettlementStyle,
)
from apex.domain.options_quote_intelligence import (
    DEFAULT_OPTIONS_COHORT_SYMBOLS,
    HISTORICAL_OPTIONS_PRICING_UNAVAILABLE,
    RISK_FREE_RATE_DEFAULT,
    ApexGreeksEstimate,
    ApexImpliedVolatilityEstimate,
    CaptureRunStatus,
    ChainCompletionStatus,
    ChainSampleCandidate,
    IvComparisonState,
    IVConvergenceStatus,
    IvRankStatus,
    MoneynessClassification,
    OptionChainCaptureBounds,
    OptionChainCapturePlan,
    OptionChainCaptureRun,
    OptionDescriptiveProfile,
    OptionQuoteObservation,
    OptionsQuoteIntelligenceError,
    QuoteObservationStatus,
    SkewShape,
    TermStructureChangeDirection,
    TermStructureShape,
    ValueSource,
    annualize_realized_volatility,
    build_assumption_sensitivity_row,
    build_atm_points,
    build_cohort_quote_quality_report,
    build_cross_sectional_summary,
    build_greeks_comparison,
    build_iv_and_greeks_for_chain,
    build_iv_comparison_rows,
    build_iv_rank,
    build_quote_quality_slice,
    build_term_structure,
    build_volatility_surface,
    compare_term_structures,
    create_options_chain_snapshot,
    derive_options_chain_snapshot_id,
    detect_arbitrage_anomalies,
    expected_move_from_iv,
    expected_move_from_straddle,
    segment_iv_comparison,
    select_contracts_stratified,
    validate_apex_greeks,
)
from apex.domain.research_cohort import (
    GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
    ResolvedCohortMember,
)
from apex.domain.security_identity import IdentityBasis, StableSecurityIdentity

NOW = datetime(2026, 8, 8, 15, 0, tzinfo=UTC)
UNDERLYING = "security-spy-stable"


def _contract(
    strike: str = "500",
    option_type: OptionType = OptionType.CALL,
    expiration: str = "2026-09-18",
    underlying: str = UNDERLYING,
) -> OptionContract:
    return OptionContract.create(
        provider_contract_id=f"SPY{expiration.replace('-', '')}{option_type.value[0]}{strike}",
        underlying_canonical_security_id=underlying,
        standardized_symbol=None,
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        exercise_style=ExerciseStyle.AMERICAN,
        settlement_style=SettlementStyle.PHYSICAL,
        multiplier="100",
        deliverable="100 SHARES",
        listing_exchange=None,
        state="ACTIVE",
        contract_size_state="STANDARD",
        adjustment_state="UNKNOWN",
        source_provider="ALPACA_OPTIONS_MARKET_DATA",
        reference_receipt_ids=("receipt-1",),
    )


def _observation(
    *,
    strike: str = "500",
    option_type: OptionType = OptionType.CALL,
    bid: str | None = "9.00",
    ask: str | None = "9.20",
    bid_size: str | None = "10",
    ask_size: str | None = "10",
    expiration: str = "2026-09-18",
    quote_timestamp: datetime = NOW,
    captured_at: datetime = NOW,
    underlying_price: str = "500",
    provider_iv: str | None = None,
    provider_delta: str | None = None,
    provider_gamma: str | None = None,
    provider_theta: str | None = None,
    provider_vega: str | None = None,
    receipts: tuple[str, ...] = ("receipt-1",),
) -> OptionQuoteObservation:
    contract = _contract(strike, option_type, expiration)
    return OptionQuoteObservation.create(
        contract=contract,
        expected_underlying_canonical_security_id=UNDERLYING,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        last_trade=None,
        trade_size=None,
        underlying_reference_price=underlying_price,
        volume="100",
        open_interest=None,
        provider_implied_volatility=provider_iv,
        provider_delta=provider_delta,
        provider_gamma=provider_gamma,
        provider_theta=provider_theta,
        provider_vega=provider_vega,
        provider_rho=None,
        market_status="OPEN",
        quote_timestamp=quote_timestamp,
        availability_timestamp=captured_at,
        ingestion_timestamp=captured_at,
        captured_at=captured_at,
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=receipts,
    )


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


def test_valid_two_sided_quote_is_observed_with_midpoint_and_executable_side_prices() -> None:
    observation = _observation(bid="9.00", ask="9.20")
    assert observation.status is QuoteObservationStatus.OBSERVED
    assert observation.exclusion_reasons == ()
    assert observation.midpoint == Decimal("9.10000000")
    assert observation.estimated_buy_price == Decimal("9.20")
    assert observation.estimated_sell_price == Decimal("9.00")
    assert observation.is_quotable


def test_missing_bid_is_one_sided_and_never_synthesizes_a_sell_price() -> None:
    observation = _observation(bid=None, ask="9.20")
    assert observation.status is QuoteObservationStatus.ONE_SIDED
    assert "MISSING_BID" in observation.exclusion_reasons
    assert observation.midpoint is None
    assert observation.estimated_sell_price is None
    assert observation.estimated_buy_price == Decimal("9.20")


def test_missing_ask_is_one_sided_and_never_synthesizes_a_buy_price() -> None:
    observation = _observation(bid="9.00", ask=None)
    assert observation.status is QuoteObservationStatus.ONE_SIDED
    assert "MISSING_ASK" in observation.exclusion_reasons
    assert observation.midpoint is None
    assert observation.estimated_buy_price is None
    assert observation.estimated_sell_price == Decimal("9.00")


def test_missing_both_sides_is_unavailable() -> None:
    observation = _observation(bid=None, ask=None)
    assert observation.status is QuoteObservationStatus.UNAVAILABLE
    assert observation.exclusion_reasons == ("BID_ASK_BOTH_MISSING",)
    assert observation.midpoint is None
    assert not observation.is_quotable


def test_crossed_quote_is_flagged_and_never_priced() -> None:
    observation = _observation(bid="9.50", ask="9.20")
    assert observation.status is QuoteObservationStatus.CROSSED
    assert observation.exclusion_reasons == ("CROSSED_QUOTE",)
    assert observation.midpoint is None
    assert observation.estimated_sell_price is None


def test_nonpositive_ask_is_invalid() -> None:
    observation = _observation(bid="0", ask="0")
    assert observation.status is QuoteObservationStatus.INVALID
    assert observation.exclusion_reasons == ("NONPOSITIVE_ASK",)


def test_stale_quote_is_classified_distinctly_from_missing_or_crossed() -> None:
    old_quote = NOW - timedelta(seconds=1000)
    observation = _observation(bid="9.00", ask="9.20", quote_timestamp=old_quote, captured_at=NOW)
    assert observation.status is QuoteObservationStatus.STALE
    assert "STALE_QUOTE" in observation.exclusion_reasons
    assert observation.staleness_seconds == 1000


def test_zero_bid_is_zero_liquidity_not_one_sided_and_still_two_sided_priced() -> None:
    observation = _observation(bid="0", ask="0.05", bid_size="0", ask_size="5")
    assert observation.status is QuoteObservationStatus.ZERO_LIQUIDITY
    assert "ZERO_BID" in observation.exclusion_reasons
    assert observation.midpoint == Decimal("0.02500000")
    assert observation.is_quotable


def test_zero_displayed_size_is_zero_liquidity() -> None:
    observation = _observation(bid="1.00", ask="1.10", bid_size="0", ask_size="5")
    assert observation.status is QuoteObservationStatus.ZERO_LIQUIDITY
    assert "ZERO_BID_SIZE" in observation.exclusion_reasons


def test_expired_contract_quote_is_invalid() -> None:
    observation = _observation(expiration="2026-01-01", captured_at=NOW)
    assert observation.status is QuoteObservationStatus.INVALID
    assert observation.exclusion_reasons == ("CONTRACT_EXPIRED",)


def test_mismatched_underlying_raises() -> None:
    contract = _contract(underlying="security-other")
    with pytest.raises(OptionsQuoteIntelligenceError):
        OptionQuoteObservation.create(
            contract=contract,
            expected_underlying_canonical_security_id=UNDERLYING,
            bid="1.00",
            ask="1.10",
            bid_size="1",
            ask_size="1",
            last_trade=None,
            trade_size=None,
            underlying_reference_price="500",
            volume=None,
            open_interest=None,
            provider_implied_volatility=None,
            provider_delta=None,
            provider_gamma=None,
            provider_theta=None,
            provider_vega=None,
            provider_rho=None,
            market_status="OPEN",
            quote_timestamp=NOW,
            availability_timestamp=NOW,
            ingestion_timestamp=NOW,
            captured_at=NOW,
            provider="ALPACA_OPTIONS_MARKET_DATA",
            source_receipt_ids=("receipt-1",),
        )


def test_bad_timestamp_ordering_raises() -> None:
    contract = _contract()
    with pytest.raises(OptionsQuoteIntelligenceError):
        OptionQuoteObservation.create(
            contract=contract,
            expected_underlying_canonical_security_id=UNDERLYING,
            bid="1.00",
            ask="1.10",
            bid_size="1",
            ask_size="1",
            last_trade=None,
            trade_size=None,
            underlying_reference_price="500",
            volume=None,
            open_interest=None,
            provider_implied_volatility=None,
            provider_delta=None,
            provider_gamma=None,
            provider_theta=None,
            provider_vega=None,
            provider_rho=None,
            market_status="OPEN",
            quote_timestamp=NOW,
            availability_timestamp=NOW - timedelta(seconds=5),
            ingestion_timestamp=NOW,
            captured_at=NOW,
            provider="ALPACA_OPTIONS_MARKET_DATA",
            source_receipt_ids=("receipt-1",),
        )


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(OptionsQuoteIntelligenceError):
        _observation(quote_timestamp=datetime(2026, 8, 8, 15, 0), captured_at=NOW)


def test_identical_inputs_reproduce_the_identical_observation_id_exact_replay() -> None:
    first = _observation()
    second = _observation()
    assert first.observation_id == second.observation_id
    assert first.canonical_hash == second.canonical_hash


def test_changed_bid_changes_the_observation_id() -> None:
    first = _observation(bid="9.00")
    second = _observation(bid="9.01")
    assert first.observation_id != second.observation_id


# ---------------------------------------------------------------------------
# Implied volatility
# ---------------------------------------------------------------------------


def test_iv_solve_converges_for_an_ordinary_atm_call_and_retains_full_lineage() -> None:
    contract = _contract(strike="500", option_type=OptionType.CALL)
    estimate = ApexImpliedVolatilityEstimate.solve(
        contract=contract,
        underlying_price=Decimal("500"),
        option_price=Decimal("15.00"),
        option_price_source="MIDPOINT",
        as_of=date(2026, 8, 8),
    )
    assert estimate.convergence_status is IVConvergenceStatus.CONVERGED
    assert estimate.implied_volatility is not None
    assert estimate.implied_volatility > 0
    assert estimate.pricing_model == "BLACK_SCHOLES_MERTON_V1"
    assert estimate.risk_free_rate_source
    assert estimate.dividend_yield_source
    assert estimate.solve_method
    assert estimate.iterations > 0
    assert estimate.time_to_expiry_years > 0


def test_iv_solve_fails_closed_on_price_below_arbitrage_floor_and_never_substitutes_zero() -> None:
    contract = _contract(strike="500", option_type=OptionType.CALL)
    # A call struck at the spot price must be worth more than zero.
    estimate = ApexImpliedVolatilityEstimate.solve(
        contract=contract,
        underlying_price=Decimal("500"),
        option_price=Decimal("0.00000001"),
        option_price_source="MIDPOINT",
        as_of=date(2026, 8, 8),
    )
    assert estimate.convergence_status is IVConvergenceStatus.FAILED_INPUT_INVALID
    assert estimate.implied_volatility is None


def test_iv_solve_fails_closed_on_expired_contract() -> None:
    contract = _contract(expiration="2026-01-01")
    estimate = ApexImpliedVolatilityEstimate.solve(
        contract=contract,
        underlying_price=Decimal("500"),
        option_price=Decimal("5.00"),
        option_price_source="MIDPOINT",
        as_of=date(2026, 8, 8),
    )
    assert estimate.convergence_status is IVConvergenceStatus.FAILED_INPUT_INVALID
    assert estimate.implied_volatility is None
    assert estimate.time_to_expiry_years < 0


def test_iv_solve_fails_no_convergence_when_price_exceeds_the_maximum_bracket() -> None:
    contract = _contract(strike="500", option_type=OptionType.CALL)
    estimate = ApexImpliedVolatilityEstimate.solve(
        contract=contract,
        underlying_price=Decimal("500"),
        option_price=Decimal("499"),
        option_price_source="MIDPOINT",
        as_of=date(2026, 8, 8),
    )
    assert estimate.convergence_status is IVConvergenceStatus.FAILED_NO_CONVERGENCE
    assert estimate.implied_volatility is None


def test_provider_and_apex_iv_are_retained_separately_and_distinguishable() -> None:
    observation = _observation(bid="14.9", ask="15.1", provider_iv="0.19")
    assert observation.provider_implied_volatility == Decimal("0.19")
    iv_estimates, _ = build_iv_and_greeks_for_chain(
        observations=(observation,), as_of=date(2026, 8, 8)
    )
    assert len(iv_estimates) == 1
    apex_iv = iv_estimates[0].implied_volatility
    assert apex_iv is not None
    # Independently solved -- not copied from the provider value.
    assert apex_iv != observation.provider_implied_volatility


def test_chain_iv_orchestration_skips_contracts_without_a_valid_midpoint() -> None:
    quotable = _observation(bid="14.9", ask="15.1")
    one_sided = _observation(strike="510", bid=None, ask="1.00")
    iv_estimates, greeks_estimates = build_iv_and_greeks_for_chain(
        observations=(quotable, one_sided), as_of=date(2026, 8, 8)
    )
    assert len(iv_estimates) == 1
    assert len(greeks_estimates) == 1
    assert iv_estimates[0].canonical_contract_id == quotable.contract.canonical_contract_id


# ---------------------------------------------------------------------------
# Greeks
# ---------------------------------------------------------------------------


def test_greeks_sign_and_bound_sanity_for_calls_and_puts() -> None:
    call = ApexGreeksEstimate.compute(
        contract=_contract(strike="500", option_type=OptionType.CALL),
        underlying_price=Decimal("500"),
        as_of=date(2026, 8, 8),
        volatility=Decimal("0.20"),
        iv_source=ValueSource.APEX_CALCULATED,
    )
    put = ApexGreeksEstimate.compute(
        contract=_contract(strike="500", option_type=OptionType.PUT),
        underlying_price=Decimal("500"),
        as_of=date(2026, 8, 8),
        volatility=Decimal("0.20"),
        iv_source=ValueSource.APEX_CALCULATED,
    )
    assert call.delta is not None and 0 <= call.delta <= 1
    assert put.delta is not None and -1 <= put.delta <= 0
    assert call.gamma is not None and call.gamma >= 0
    assert put.gamma is not None and put.gamma >= 0
    assert call.vega is not None and call.vega >= 0
    assert put.vega is not None and put.vega >= 0
    # Same-strike ATM call/put gamma and vega are identical under Black-Scholes.
    assert call.gamma == put.gamma
    assert call.vega == put.vega


def test_call_delta_decreases_as_strike_moves_further_out_of_the_money() -> None:
    near = ApexGreeksEstimate.compute(
        contract=_contract(strike="500", option_type=OptionType.CALL),
        underlying_price=Decimal("500"),
        as_of=date(2026, 8, 8),
        volatility=Decimal("0.20"),
        iv_source=ValueSource.APEX_CALCULATED,
    )
    far = ApexGreeksEstimate.compute(
        contract=_contract(strike="550", option_type=OptionType.CALL),
        underlying_price=Decimal("500"),
        as_of=date(2026, 8, 8),
        volatility=Decimal("0.20"),
        iv_source=ValueSource.APEX_CALCULATED,
    )
    assert near.delta is not None and far.delta is not None
    assert far.delta < near.delta


def test_greeks_are_all_none_and_iv_source_unavailable_for_invalid_inputs() -> None:
    estimate = ApexGreeksEstimate.compute(
        contract=_contract(expiration="2026-01-01"),
        underlying_price=Decimal("500"),
        as_of=date(2026, 8, 8),
        volatility=None,
        iv_source=ValueSource.UNAVAILABLE,
    )
    assert estimate.delta is None
    assert estimate.gamma is None
    assert estimate.theta is None
    assert estimate.vega is None
    assert estimate.iv_source is ValueSource.UNAVAILABLE


def test_greeks_provider_source_is_distinguishable_from_apex_calculated() -> None:
    provider = ApexGreeksEstimate.compute(
        contract=_contract(),
        underlying_price=Decimal("500"),
        as_of=date(2026, 8, 8),
        volatility=Decimal("0.22"),
        iv_source=ValueSource.PROVIDER,
    )
    apex = ApexGreeksEstimate.compute(
        contract=_contract(),
        underlying_price=Decimal("500"),
        as_of=date(2026, 8, 8),
        volatility=Decimal("0.22"),
        iv_source=ValueSource.APEX_CALCULATED,
    )
    assert provider.iv_source is ValueSource.PROVIDER
    assert apex.iv_source is ValueSource.APEX_CALCULATED
    assert provider.canonical_hash != apex.canonical_hash


# ---------------------------------------------------------------------------
# Volatility surface: ATM / skew / term structure / sparse chain
# ---------------------------------------------------------------------------


def _priced_chain() -> tuple[OptionQuoteObservation, ...]:
    near = (
        _observation(strike="480", option_type=OptionType.PUT, bid="1.10", ask="1.30"),
        _observation(strike="490", option_type=OptionType.PUT, bid="3.00", ask="3.20"),
        _observation(strike="500", option_type=OptionType.PUT, bid="8.00", ask="8.20"),
        _observation(strike="500", option_type=OptionType.CALL, bid="9.00", ask="9.20"),
        _observation(strike="510", option_type=OptionType.CALL, bid="4.00", ask="4.20"),
        _observation(strike="520", option_type=OptionType.CALL, bid="1.50", ask="1.70"),
    )
    far = (
        _observation(
            strike="500",
            option_type=OptionType.CALL,
            bid="12.0",
            ask="12.4",
            expiration="2026-12-18",
        ),
        _observation(
            strike="500",
            option_type=OptionType.PUT,
            bid="11.0",
            ask="11.4",
            expiration="2026-12-18",
        ),
    )
    return near + far


def test_atm_point_selects_the_nearest_strike_quotable_contract_per_expiration() -> None:
    chain = _priced_chain()
    iv_estimates, _ = build_iv_and_greeks_for_chain(observations=chain, as_of=date(2026, 8, 8))
    ivs_by_contract = {
        item.canonical_contract_id: item.implied_volatility
        for item in iv_estimates
        if item.implied_volatility is not None
    }
    points = build_atm_points(chain, ivs_by_contract)
    assert {point.expiration for point in points} == {"2026-09-18", "2026-12-18"}
    near_point = next(point for point in points if point.expiration == "2026-09-18")
    assert near_point.atm_iv is not None
    assert near_point.source is ValueSource.APEX_CALCULATED


def test_term_structure_requires_at_least_two_expirations() -> None:
    chain = _priced_chain()
    iv_estimates, _ = build_iv_and_greeks_for_chain(observations=chain, as_of=date(2026, 8, 8))
    ivs_by_contract = {
        item.canonical_contract_id: item.implied_volatility
        for item in iv_estimates
        if item.implied_volatility is not None
    }
    points = build_atm_points(chain, ivs_by_contract)
    term_structure = build_term_structure(points)
    assert term_structure.shape is not TermStructureShape.INSUFFICIENT_EXPIRATIONS
    assert term_structure.near_far_spread is not None

    single_expiration = tuple(item for item in points if item.expiration == "2026-09-18")
    sparse = build_term_structure(single_expiration)
    assert sparse.shape is TermStructureShape.INSUFFICIENT_EXPIRATIONS
    assert sparse.near_far_spread is None


def test_term_structure_change_is_insufficient_prior_without_a_previous_surface() -> None:
    surface = build_volatility_surface(
        underlying_canonical_security_id=UNDERLYING,
        captured_at=NOW,
        observations=_priced_chain(),
        ivs_by_contract={},
        deltas_by_contract={},
    )
    change = compare_term_structures(
        underlying_canonical_security_id=UNDERLYING, previous=None, current=surface
    )
    assert change.direction is TermStructureChangeDirection.INSUFFICIENT_PRIOR_SNAPSHOTS
    assert change.spread_delta is None


def test_term_structure_change_detects_steepening_between_two_real_surfaces() -> None:
    chain = _priced_chain()
    iv_estimates, _ = build_iv_and_greeks_for_chain(observations=chain, as_of=date(2026, 8, 8))
    ivs_by_contract = {
        item.canonical_contract_id: item.implied_volatility
        for item in iv_estimates
        if item.implied_volatility is not None
    }
    earlier = build_volatility_surface(
        underlying_canonical_security_id=UNDERLYING,
        captured_at=NOW - timedelta(days=1),
        observations=chain,
        ivs_by_contract=ivs_by_contract,
        deltas_by_contract={},
    )
    # A widened near/far spread the next day -- construct a second surface
    # by scaling the far-expiration IV further from the near one.
    scaled_ivs = dict(ivs_by_contract)
    far_call = next(
        item.contract.canonical_contract_id
        for item in chain
        if item.contract.expiration == "2026-12-18" and item.contract.option_type is OptionType.CALL
    )
    scaled_ivs[far_call] = (scaled_ivs[far_call] or Decimal("0")) - Decimal("0.05")
    later = build_volatility_surface(
        underlying_canonical_security_id=UNDERLYING,
        captured_at=NOW,
        observations=chain,
        ivs_by_contract=scaled_ivs,
        deltas_by_contract={},
    )
    change = compare_term_structures(
        underlying_canonical_security_id=UNDERLYING, previous=earlier, current=later
    )
    assert change.direction is not TermStructureChangeDirection.INSUFFICIENT_PRIOR_SNAPSHOTS
    assert change.spread_delta is not None
    assert change.previous_captured_at == earlier.captured_at
    assert change.current_captured_at == later.captured_at


def test_sparse_single_contract_chain_produces_insufficient_skew_data_not_a_crash() -> None:
    chain = (_observation(strike="500", option_type=OptionType.CALL, bid="9.00", ask="9.20"),)
    surface = build_volatility_surface(
        underlying_canonical_security_id=UNDERLYING,
        captured_at=NOW,
        observations=chain,
        ivs_by_contract={},
        deltas_by_contract={},
    )
    assert len(surface.skew_by_expiration) == 1
    assert surface.skew_by_expiration[0].shape is SkewShape.INSUFFICIENT_DATA
    assert surface.term_structure.shape is TermStructureShape.INSUFFICIENT_EXPIRATIONS


def test_skew_profile_computes_put_versus_call_iv_at_the_target_delta() -> None:
    chain = _priced_chain()
    iv_estimates, greeks_estimates = build_iv_and_greeks_for_chain(
        observations=chain, as_of=date(2026, 8, 8)
    )
    ivs_by_contract = {
        item.canonical_contract_id: item.implied_volatility
        for item in iv_estimates
        if item.implied_volatility is not None
    }
    deltas_by_contract = {
        item.canonical_contract_id: item.delta
        for item in greeks_estimates
        if item.delta is not None
    }
    surface = build_volatility_surface(
        underlying_canonical_security_id=UNDERLYING,
        captured_at=NOW,
        observations=chain,
        ivs_by_contract=ivs_by_contract,
        deltas_by_contract=deltas_by_contract,
    )
    near_skew = next(item for item in surface.skew_by_expiration if item.expiration == "2026-09-18")
    assert near_skew.put_iv_target_delta is not None
    assert near_skew.call_iv_target_delta is not None
    assert near_skew.skew_target_delta is not None
    assert near_skew.shape is not SkewShape.INSUFFICIENT_DATA


# ---------------------------------------------------------------------------
# IV rank / IV history
# ---------------------------------------------------------------------------


def test_iv_rank_is_insufficient_history_below_the_declared_minimum() -> None:
    result = build_iv_rank(
        current_atm_iv=Decimal("0.20"),
        prior_atm_iv_history=(Decimal("0.18"),) * 5,
        minimum_required=20,
    )
    assert result.status is IvRankStatus.INSUFFICIENT_HISTORY
    assert result.percentile is None
    assert result.retained_snapshot_count == 5


def test_iv_rank_computes_a_percentile_once_sufficient_history_exists() -> None:
    history = tuple(Decimal(str(0.10 + 0.01 * i)) for i in range(20))
    at_the_top = build_iv_rank(
        current_atm_iv=Decimal("0.35"), prior_atm_iv_history=history, minimum_required=20
    )
    assert at_the_top.status is IvRankStatus.COMPUTED
    assert at_the_top.percentile == Decimal("1.00000000")

    mid_pack = build_iv_rank(
        current_atm_iv=Decimal("0.20"), prior_atm_iv_history=history, minimum_required=20
    )
    assert mid_pack.status is IvRankStatus.COMPUTED
    # 11 of 20 retained values (0.10..0.20 inclusive) are <= the current IV.
    assert mid_pack.percentile == Decimal("0.55000000")


# ---------------------------------------------------------------------------
# Expected move
# ---------------------------------------------------------------------------


def test_expected_move_from_straddle_sums_ask_side_atm_call_and_put() -> None:
    chain = _priced_chain()
    estimate = expected_move_from_straddle(
        underlying_canonical_security_id=UNDERLYING,
        underlying_price=Decimal("500"),
        expiration="2026-09-18",
        dte=41,
        observations=chain,
    )
    assert estimate.status == "COMPUTED"
    assert estimate.method == "ATM_STRADDLE_V1"
    assert estimate.dollar_move == Decimal("17.40000000")
    assert len(estimate.inputs_used) == 2


def test_expected_move_from_straddle_is_insufficient_without_both_sides() -> None:
    calls_only = (_observation(strike="500", option_type=OptionType.CALL, bid="9.00", ask="9.20"),)
    estimate = expected_move_from_straddle(
        underlying_canonical_security_id=UNDERLYING,
        underlying_price=Decimal("500"),
        expiration="2026-09-18",
        dte=41,
        observations=calls_only,
    )
    assert estimate.status == "INSUFFICIENT_DATA"
    assert estimate.dollar_move is None


def test_expected_move_from_iv_scales_with_sqrt_of_time() -> None:
    estimate = expected_move_from_iv(
        underlying_canonical_security_id=UNDERLYING,
        underlying_price=Decimal("500"),
        expiration="2026-09-18",
        dte=365,
        atm_iv=Decimal("0.20"),
    )
    assert estimate.status == "COMPUTED"
    assert estimate.dollar_move == Decimal("100.00000000")
    assert "confidence" not in estimate.methodology_note.lower().replace(
        "not an empirically validated confidence", ""
    )


def test_expected_move_from_iv_is_insufficient_without_an_atm_iv() -> None:
    estimate = expected_move_from_iv(
        underlying_canonical_security_id=UNDERLYING,
        underlying_price=Decimal("500"),
        expiration="2026-09-18",
        dte=41,
        atm_iv=None,
    )
    assert estimate.status == "INSUFFICIENT_DATA"
    assert estimate.dollar_move is None
    assert estimate.percent_move is None


# ---------------------------------------------------------------------------
# Quality: spread / staleness / completeness / arbitrage anomaly
# ---------------------------------------------------------------------------


def test_realized_volatility_annualization_uses_the_252_session_convention() -> None:
    annualized = annualize_realized_volatility(Decimal("0.01"))
    assert annualized == Decimal(str(round(0.01 * (252**0.5), 8)))


def test_arbitrage_detector_flags_a_strike_monotonicity_violation() -> None:
    inconsistent = (
        _observation(strike="500", option_type=OptionType.CALL, bid="4.00", ask="4.20"),
        # A higher-strike call must never be priced above a lower-strike call.
        _observation(strike="510", option_type=OptionType.CALL, bid="9.00", ask="9.20"),
    )
    flags = detect_arbitrage_anomalies(UNDERLYING, inconsistent)
    assert any(flag.anomaly_type == "STRIKE_MONOTONICITY_VIOLATION_CALL" for flag in flags)


def test_arbitrage_detector_is_quiet_on_a_consistent_monotonic_chain() -> None:
    consistent = (
        _observation(strike="500", option_type=OptionType.CALL, bid="9.00", ask="9.20"),
        _observation(strike="510", option_type=OptionType.CALL, bid="4.00", ask="4.20"),
        _observation(strike="520", option_type=OptionType.CALL, bid="1.50", ask="1.70"),
    )
    flags = detect_arbitrage_anomalies(UNDERLYING, consistent)
    assert not any(flag.anomaly_type.startswith("STRIKE_MONOTONICITY") for flag in flags)


def test_arbitrage_detector_never_mutates_or_excludes_the_flagged_observations() -> None:
    inconsistent = (
        _observation(strike="500", option_type=OptionType.CALL, bid="4.00", ask="4.20"),
        _observation(strike="510", option_type=OptionType.CALL, bid="9.00", ask="9.20"),
    )
    flags = detect_arbitrage_anomalies(UNDERLYING, inconsistent)
    assert flags  # quality control only -- this is purely descriptive evidence
    assert len(inconsistent) == 2


# ---------------------------------------------------------------------------
# Snapshot: deterministic identity, changed evidence, source lineage
# ---------------------------------------------------------------------------


def _snapshot(observations: tuple[OptionQuoteObservation, ...] | None = None):
    return create_options_chain_snapshot(
        plan_id="plan-1",
        run_id="run-1",
        underlying_canonical_security_id=UNDERLYING,
        underlying_symbol="SPY",
        underlying_reference_price=Decimal("500"),
        underlying_price_source="ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
        captured_at=NOW,
        observations=_priced_chain() if observations is None else observations,
        as_of=date(2026, 8, 8),
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )


def test_snapshot_replay_from_identical_evidence_reproduces_the_identical_id() -> None:
    first = _snapshot()
    second = _snapshot()
    assert first.snapshot_id == second.snapshot_id


def test_snapshot_changed_quote_changes_the_snapshot_id() -> None:
    baseline = _priced_chain()
    mutated = (
        _observation(strike="480", option_type=OptionType.PUT, bid="1.15", ask="1.30"),
        *baseline[1:],
    )
    first = _snapshot(baseline)
    second = _snapshot(mutated)
    assert first.snapshot_id != second.snapshot_id


def test_snapshot_retains_source_lineage_and_authority_none() -> None:
    snapshot = _snapshot()
    assert snapshot.source_receipt_ids == ("receipt-1",)
    assert snapshot.provider == "ALPACA_OPTIONS_MARKET_DATA"
    assert snapshot.authority == "NONE"
    assert snapshot.historical_options_pricing_note == HISTORICAL_OPTIONS_PRICING_UNAVAILABLE
    assert len(snapshot.observation_ids) == len(_priced_chain())


def test_snapshot_data_quality_status_reflects_exclusions() -> None:
    complete = _snapshot(
        (_observation(strike="500", option_type=OptionType.CALL, bid="9.00", ask="9.20"),)
    )
    assert complete.data_quality_status is ChainCompletionStatus.COMPLETE

    one_sided = _observation(strike="510", option_type=OptionType.CALL, bid=None, ask="1.00")
    partial = _snapshot(
        (_observation(strike="500", option_type=OptionType.CALL, bid="9.00", ask="9.20"), one_sided)
    )
    assert partial.data_quality_status is ChainCompletionStatus.PARTIAL
    assert partial.exclusion_reason_counts == (("MISSING_BID", 1),)

    empty = _snapshot(())
    assert empty.data_quality_status is ChainCompletionStatus.EMPTY


def test_snapshot_underlying_mismatch_raises() -> None:
    foreign = _observation()
    with pytest.raises(OptionsQuoteIntelligenceError):
        create_options_chain_snapshot(
            plan_id="plan-1",
            run_id="run-1",
            underlying_canonical_security_id="security-other",
            underlying_symbol="OTHER",
            underlying_reference_price=Decimal("500"),
            underlying_price_source="ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
            captured_at=NOW,
            observations=(foreign,),
            as_of=date(2026, 8, 8),
            provider="ALPACA_OPTIONS_MARKET_DATA",
            source_receipt_ids=("receipt-1",),
        )


# ---------------------------------------------------------------------------
# Descriptive metadata (moneyness / maturity)
# ---------------------------------------------------------------------------


def test_moneyness_classification_itm_atm_otm_for_calls_and_puts() -> None:
    atm_call = OptionDescriptiveProfile.create(
        contract=_contract(strike="500", option_type=OptionType.CALL),
        underlying_price=Decimal("500"),
        as_of=date(2026, 8, 8),
    )
    itm_call = OptionDescriptiveProfile.create(
        contract=_contract(strike="480", option_type=OptionType.CALL),
        underlying_price=Decimal("500"),
        as_of=date(2026, 8, 8),
    )
    otm_call = OptionDescriptiveProfile.create(
        contract=_contract(strike="520", option_type=OptionType.CALL),
        underlying_price=Decimal("500"),
        as_of=date(2026, 8, 8),
    )
    itm_put = OptionDescriptiveProfile.create(
        contract=_contract(strike="520", option_type=OptionType.PUT),
        underlying_price=Decimal("500"),
        as_of=date(2026, 8, 8),
    )
    assert atm_call.moneyness_classification is MoneynessClassification.ATM
    assert itm_call.moneyness_classification is MoneynessClassification.ITM
    assert otm_call.moneyness_classification is MoneynessClassification.OTM
    assert itm_put.moneyness_classification is MoneynessClassification.ITM


def test_expiration_bucket_boundaries() -> None:
    as_of = date(2026, 8, 8)
    week = OptionDescriptiveProfile.create(
        contract=_contract(expiration="2026-08-12"), underlying_price=Decimal("500"), as_of=as_of
    )
    month = OptionDescriptiveProfile.create(
        contract=_contract(expiration="2026-08-30"), underlying_price=Decimal("500"), as_of=as_of
    )
    assert week.dte == 4
    assert week.expiration_bucket.value == "DAYS_0_7"
    assert month.expiration_bucket.value == "DAYS_8_30"


# ---------------------------------------------------------------------------
# Bounded chain-selection policy
# ---------------------------------------------------------------------------


def _stable_identity(symbol: str) -> StableSecurityIdentity:
    return StableSecurityIdentity(
        stable_security_id=f"stable-{symbol.lower()}",
        identity_basis=IdentityBasis.CIK_AND_SYMBOL,
        cik="0000000000",
        normalized_symbol=symbol,
        current_symbol=symbol,
        source_identity_hash="0" * 64,
        source_canonical_security_id=f"legacy-{symbol.lower()}",
        review_required=False,
        canonical_hash="0" * 64,
    )


def _resolved_member(symbol: str) -> ResolvedCohortMember:
    declared = next(
        item for item in GENUINE_MULTI_ASSET_COHORT_POLICY_V1.members if item.symbol == symbol
    )
    return ResolvedCohortMember(
        stable_security_id=f"stable-{symbol.lower()}",
        symbol=symbol,
        sector=declared.sector,
        roles=declared.roles,
        asset_class="ETF" if declared.is_etf else "COMMON_STOCK",
    )


def test_default_cohort_is_bounded_between_10_and_25_and_drawn_from_the_genuine_cohort() -> None:
    assert 10 <= len(DEFAULT_OPTIONS_COHORT_SYMBOLS) <= 25
    declared_symbols = {item.symbol for item in GENUINE_MULTI_ASSET_COHORT_POLICY_V1.members}
    assert set(DEFAULT_OPTIONS_COHORT_SYMBOLS).issubset(declared_symbols)
    assert {"SPY", "QQQ", "IWM"}.issubset(set(DEFAULT_OPTIONS_COHORT_SYMBOLS))


def test_bounds_validate_declared_ranges() -> None:
    bounds = OptionChainCaptureBounds.create()
    assert bounds.max_contracts_per_underlying > 0
    assert bounds.dte_minimum < bounds.dte_maximum
    with pytest.raises(OptionsQuoteIntelligenceError):
        OptionChainCaptureBounds.create(max_expirations=0)
    with pytest.raises(OptionsQuoteIntelligenceError):
        OptionChainCaptureBounds.create(dte_minimum=10, dte_maximum=5)


def test_plan_creation_requires_at_least_ten_and_at_most_twenty_five_members() -> None:
    bounds = OptionChainCaptureBounds.create()
    too_few = tuple(_resolved_member(symbol) for symbol in DEFAULT_OPTIONS_COHORT_SYMBOLS[:5])
    with pytest.raises(OptionsQuoteIntelligenceError):
        OptionChainCapturePlan.create(
            cohort_policy_id=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id,
            cohort_policy_hash=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.canonical_hash,
            members=too_few,
            unresolved_symbols=(),
            bounds=bounds,
            created_at=NOW,
        )


def test_plan_creation_rejects_a_symbol_outside_the_declared_selection() -> None:
    bounds = OptionChainCaptureBounds.create()
    members = tuple(_resolved_member(symbol) for symbol in DEFAULT_OPTIONS_COHORT_SYMBOLS[:9])
    members = (
        *members,
        _resolved_member("AAPL")
        if "AAPL" not in DEFAULT_OPTIONS_COHORT_SYMBOLS
        else _resolved_member("TSLA"),
    )
    outside_symbols = {"TSLA", "GOOGL", "META"} - set(DEFAULT_OPTIONS_COHORT_SYMBOLS)
    outside_symbol = next(iter(outside_symbols))
    members = (*members[:-1], _resolved_member(outside_symbol))
    with pytest.raises(OptionsQuoteIntelligenceError):
        OptionChainCapturePlan.create(
            cohort_policy_id=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id,
            cohort_policy_hash=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.canonical_hash,
            members=members,
            unresolved_symbols=(),
            bounds=bounds,
            created_at=NOW,
        )


def test_plan_identity_excludes_created_at_so_re_declaration_is_idempotent() -> None:
    bounds = OptionChainCaptureBounds.create()
    members = tuple(_resolved_member(symbol) for symbol in DEFAULT_OPTIONS_COHORT_SYMBOLS)
    first = OptionChainCapturePlan.create(
        cohort_policy_id=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id,
        cohort_policy_hash=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.canonical_hash,
        members=members,
        unresolved_symbols=(),
        bounds=bounds,
        created_at=NOW,
    )
    second = OptionChainCapturePlan.create(
        cohort_policy_id=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id,
        cohort_policy_hash=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.canonical_hash,
        members=members,
        unresolved_symbols=(),
        bounds=bounds,
        created_at=NOW + timedelta(days=1),
    )
    assert first.plan_id == second.plan_id


# ---------------------------------------------------------------------------
# Capability: never inferred from authentication
# ---------------------------------------------------------------------------


def test_capability_snapshot_rejects_a_nonzero_request_count_with_absent_credentials() -> None:
    from apex.domain.options_intelligence import CapabilityState, OptionProviderCapabilities
    from apex.domain.options_quote_intelligence import OptionsQuoteProviderCapabilitySnapshot

    capability = OptionProviderCapabilities.create(
        provider="ALPACA_OPTIONS_MARKET_DATA",
        contract_discovery=CapabilityState.UNKNOWN,
        quotes=CapabilityState.UNKNOWN,
        trades=CapabilityState.UNKNOWN,
        snapshots=CapabilityState.UNKNOWN,
        implied_volatility=CapabilityState.UNKNOWN,
        greeks=CapabilityState.UNKNOWN,
        open_interest=CapabilityState.UNKNOWN,
        volume=CapabilityState.UNKNOWN,
    )
    with pytest.raises(OptionsQuoteIntelligenceError):
        OptionsQuoteProviderCapabilitySnapshot.create(
            checked_at=NOW,
            credentials_available=False,
            requests_made=1,
            capability=capability,
            unavailable_reason="ALPACA_CREDENTIALS_ABSENT",
        )


# ---------------------------------------------------------------------------
# Capture run: identity, ordering invariants
# ---------------------------------------------------------------------------


def test_capture_run_id_is_derivable_before_the_run_completes() -> None:
    run_id = OptionChainCaptureRun.derive_run_id(plan_id="plan-1", requested_at=NOW)
    run = OptionChainCaptureRun.create(
        plan_id="plan-1",
        requested_at=NOW,
        completed_at=NOW + timedelta(seconds=5),
        status=CaptureRunStatus.EMPTY,
        underlyings_requested=1,
        underlyings_captured=0,
        provider_request_count=1,
        payload_bytes=100,
        contracts_considered=0,
        contracts_quoted=0,
        snapshot_ids=(),
        reason="NO_UNDERLYING_PRODUCED_QUOTABLE_EVIDENCE",
    )
    assert run.run_id == run_id


def test_capture_run_rejects_completed_before_requested() -> None:
    with pytest.raises(OptionsQuoteIntelligenceError):
        OptionChainCaptureRun.create(
            plan_id="plan-1",
            requested_at=NOW,
            completed_at=NOW - timedelta(seconds=1),
            status=CaptureRunStatus.EMPTY,
            underlyings_requested=1,
            underlyings_captured=0,
            provider_request_count=0,
            payload_bytes=0,
            contracts_considered=0,
            contracts_quoted=0,
            snapshot_ids=(),
            reason=None,
        )


def test_capture_run_rejects_a_request_when_credentials_are_absent() -> None:
    with pytest.raises(OptionsQuoteIntelligenceError):
        OptionChainCaptureRun.create(
            plan_id="plan-1",
            requested_at=NOW,
            completed_at=NOW,
            status=CaptureRunStatus.BLOCKED_CREDENTIALS_ABSENT,
            underlyings_requested=1,
            underlyings_captured=0,
            provider_request_count=1,
            payload_bytes=0,
            contracts_considered=0,
            contracts_quoted=0,
            snapshot_ids=(),
            reason="ALPACA_CREDENTIALS_ABSENT",
        )


# ---------------------------------------------------------------------------
# Genuine Options Quote-Chain Bring-Up and Volatility Validation v1:
# snapshot-id pre-derivation, quote-quality slicing, provider-versus-Apex
# comparison, Greeks validation, cross-sectional summaries, assumption
# sensitivity.
# ---------------------------------------------------------------------------


def test_derived_snapshot_id_matches_the_id_create_options_chain_snapshot_computes() -> None:
    observations = _priced_chain()
    snapshot = _snapshot(observations)
    predicted = derive_options_chain_snapshot_id(
        underlying_canonical_security_id=UNDERLYING, captured_at=NOW, observations=observations
    )
    assert predicted == snapshot.snapshot_id


def test_derived_snapshot_id_is_order_independent_and_changes_with_the_evidence() -> None:
    observations = _priced_chain()
    reversed_order = tuple(reversed(observations))
    assert derive_options_chain_snapshot_id(
        underlying_canonical_security_id=UNDERLYING, captured_at=NOW, observations=observations
    ) == derive_options_chain_snapshot_id(
        underlying_canonical_security_id=UNDERLYING, captured_at=NOW, observations=reversed_order
    )
    changed = (*observations[1:], _observation(strike="530", bid="0.50", ask="0.70"))
    assert derive_options_chain_snapshot_id(
        underlying_canonical_security_id=UNDERLYING, captured_at=NOW, observations=observations
    ) != derive_options_chain_snapshot_id(
        underlying_canonical_security_id=UNDERLYING, captured_at=NOW, observations=changed
    )


# ---------------------------------------------------------------------------
# Quote-quality slicing
# ---------------------------------------------------------------------------


def test_quote_quality_slice_separates_staleness_from_sidedness() -> None:
    """A stale-but-two-sided quote must count as both stale *and*
    two-sided -- ``QuoteObservationStatus`` alone would hide this."""

    stale_two_sided = _observation(
        strike="500", bid="9.00", ask="9.20", quote_timestamp=NOW - timedelta(minutes=30)
    )
    fresh_two_sided = _observation(strike="510", bid="4.00", ask="4.20", quote_timestamp=NOW)
    one_sided = _observation(strike="520", bid=None, ask="1.00", quote_timestamp=NOW)
    zero_bid = _observation(strike="530", bid="0", ask="0.20", quote_timestamp=NOW)
    crossed = _observation(strike="540", bid="5.00", ask="4.00", quote_timestamp=NOW)
    observations = (stale_two_sided, fresh_two_sided, one_sided, zero_bid, crossed)

    report = build_quote_quality_slice(observations, dimension="UNDERLYING", key="SPY")
    assert report.contracts_considered == 5
    # "two-sided" means both fields are *present*, independent of whether
    # they are validly ordered -- crossed still counts (bid=5.00/ask=4.00
    # are both populated, just inverted).
    assert report.two_sided_count == 4  # stale, fresh, zero_bid, crossed
    assert report.one_sided_count == 1
    # "zero-bid" (matching the existing ``build_liquidity_asymmetry``
    # convention) is bid missing *or* zero: the one-sided quote's missing
    # bid counts here too, alongside the explicit zero-bid quote.
    assert report.zero_bid_count == 2
    assert report.crossed_count == 1
    assert report.stale_count == 1
    assert report.usable_count == 3  # stale + fresh + zero_bid all have a valid midpoint
    assert report.current_quotable_count == 2  # fresh + zero_bid (is_quotable); stale excluded


def test_quote_quality_slice_median_spread_handles_even_and_odd_counts() -> None:
    observations = (
        _observation(strike="500", bid="9.00", ask="9.20"),  # spread 0.20
        _observation(strike="510", bid="4.00", ask="4.50"),  # spread 0.50
        _observation(strike="520", bid="1.00", ask="1.90"),  # spread 0.90
    )
    report = build_quote_quality_slice(observations, dimension="OVERALL", key="ALL")
    assert report.median_absolute_spread == Decimal("0.50")

    two_only = observations[:2]
    report_two = build_quote_quality_slice(two_only, dimension="OVERALL", key="ALL")
    assert report_two.median_absolute_spread == Decimal("0.35")  # midpoint of 0.20 and 0.50


def test_quote_quality_slice_on_empty_observations_reports_zero_rates_never_divides_by_zero() -> (
    None
):
    report = build_quote_quality_slice((), dimension="UNDERLYING", key="EMPTY")
    assert report.contracts_considered == 0
    assert report.two_sided_rate == Decimal(0)
    assert report.median_absolute_spread is None


def test_cohort_quote_quality_report_segments_by_underlying_expiration_and_call_put() -> None:
    spy_observations = _priced_chain()
    spy_snapshot = _snapshot(spy_observations)
    qqq_observations = (
        _observation(strike="500", option_type=OptionType.CALL, bid="9.00", ask="9.20"),
    )
    qqq_snapshot = create_options_chain_snapshot(
        plan_id="plan-1",
        run_id="run-1",
        underlying_canonical_security_id="security-qqq-stable",
        underlying_symbol="QQQ",
        underlying_reference_price=Decimal("500"),
        underlying_price_source="ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
        captured_at=NOW,
        observations=tuple(
            replace(
                item,
                contract=replace(
                    item.contract, underlying_canonical_security_id="security-qqq-stable"
                ),
            )
            for item in qqq_observations
        ),
        as_of=date(2026, 8, 8),
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )
    report = build_cohort_quote_quality_report(
        generated_at=NOW,
        members=(
            ("SPY", spy_snapshot, spy_observations),
            ("QQQ", qqq_snapshot, (qqq_observations[0],)),
        ),
    )
    assert report.underlyings_included == ("SPY", "QQQ")
    assert report.overall.contracts_considered == len(spy_observations) + 1
    assert {item.key for item in report.by_underlying} == {"SPY", "QQQ"}
    spy_slice = next(item for item in report.by_underlying if item.key == "SPY")
    assert spy_slice.contracts_considered == len(spy_observations)
    assert {item.key for item in report.by_call_put} == {"CALL", "PUT"}
    calls = sum(1 for item in spy_observations if item.contract.option_type is OptionType.CALL) + 1
    call_slice = next(item for item in report.by_call_put if item.key == "CALL")
    assert call_slice.contracts_considered == calls
    bucket_total = sum(item.contracts_considered for item in report.by_expiration_bucket)
    assert bucket_total == report.overall.contracts_considered


# ---------------------------------------------------------------------------
# Provider-versus-Apex IV comparison
# ---------------------------------------------------------------------------


def test_iv_comparison_classifies_close_moderate_and_large_disagreement() -> None:
    close = _observation(strike="500", bid="9.00", ask="9.20", provider_iv="0.20")
    moderate = _observation(strike="510", bid="4.00", ask="4.20", provider_iv="0.20")
    large = _observation(strike="520", bid="1.50", ask="1.70", provider_iv="0.90")
    snapshot = _snapshot((close, moderate, large))
    rows = build_iv_comparison_rows(
        underlying_symbol="SPY", snapshot=snapshot, observations=(close, moderate, large)
    )
    assert len(rows) == 3
    states = {row.canonical_contract_id: row.comparison_state for row in rows}
    close_state = states[close.contract.canonical_contract_id]
    large_state = states[large.contract.canonical_contract_id]
    assert large_state is IvComparisonState.LARGE_DISAGREEMENT
    # Whatever Apex actually solves for the near-ATM contract, it must be
    # retained -- large disagreement is evidence, never filtered away.
    assert close_state in (
        IvComparisonState.CLOSE_AGREEMENT,
        IvComparisonState.MODERATE_DISAGREEMENT,
        IvComparisonState.LARGE_DISAGREEMENT,
    )


def test_iv_comparison_reports_provider_missing_and_apex_missing_states_separately() -> None:
    no_provider = _observation(strike="500", bid="9.00", ask="9.20", provider_iv=None)
    invalid_price = _observation(strike="600", bid="0.00", ask="0.00", provider_iv="0.20")
    snapshot = _snapshot((no_provider, invalid_price))
    rows = build_iv_comparison_rows(
        underlying_symbol="SPY", snapshot=snapshot, observations=(no_provider, invalid_price)
    )
    by_contract = {row.canonical_contract_id: row for row in rows}
    assert (
        by_contract[no_provider.contract.canonical_contract_id].comparison_state
        is IvComparisonState.PROVIDER_IV_UNAVAILABLE
    )
    # A zero-priced (invalid) quote never reaches the IV solver, so Apex has
    # no estimate even though the provider supplied one.
    assert (
        by_contract[invalid_price.contract.canonical_contract_id].comparison_state
        is IvComparisonState.APEX_IV_UNAVAILABLE
    )


def test_iv_comparison_never_forces_agreement_and_skips_rows_with_neither_side() -> None:
    neither = _observation(strike="700", bid=None, ask=None, provider_iv=None)
    snapshot = _snapshot((neither,))
    rows = build_iv_comparison_rows(
        underlying_symbol="SPY", snapshot=snapshot, observations=(neither,)
    )
    assert rows == ()


def test_segment_iv_comparison_produces_an_overall_row_plus_every_dimension() -> None:
    call = _observation(
        strike="500", option_type=OptionType.CALL, bid="9.00", ask="9.20", provider_iv="0.19"
    )
    put = _observation(
        strike="500", option_type=OptionType.PUT, bid="8.00", ask="8.20", provider_iv="0.21"
    )
    snapshot = _snapshot((call, put))
    rows = build_iv_comparison_rows(
        underlying_symbol="SPY", snapshot=snapshot, observations=(call, put)
    )
    segments = segment_iv_comparison(rows)
    dimensions = {item.dimension for item in segments}
    assert dimensions == {
        "OVERALL",
        "OPTION_TYPE",
        "EXPIRATION_BUCKET",
        "MONEYNESS",
        "LIQUIDITY_STATUS",
    }
    overall = next(item for item in segments if item.dimension == "OVERALL")
    assert overall.row_count == 2


# ---------------------------------------------------------------------------
# Greeks comparison and validation
# ---------------------------------------------------------------------------


def test_greeks_comparison_computes_mean_absolute_differences() -> None:
    observation = _observation(
        strike="500",
        option_type=OptionType.CALL,
        bid="9.00",
        ask="9.20",
        provider_delta="0.60",
        provider_gamma="0.01",
        provider_theta="-0.03",
        provider_vega="0.20",
    )
    snapshot = _snapshot((observation,))
    apex_delta = next(
        item.delta
        for item in snapshot.greeks_estimates
        if item.canonical_contract_id == observation.contract.canonical_contract_id
    )
    assert apex_delta is not None
    summary = build_greeks_comparison(
        underlying_symbol="SPY", snapshot=snapshot, observations=(observation,)
    )
    assert summary.both_available_count == 1
    assert summary.mean_absolute_delta_difference == abs(Decimal("0.60") - apex_delta)


def test_greeks_comparison_is_zero_available_when_provider_has_no_greeks() -> None:
    observation = _observation(strike="500", bid="9.00", ask="9.20")
    snapshot = _snapshot((observation,))
    summary = build_greeks_comparison(
        underlying_symbol="SPY", snapshot=snapshot, observations=(observation,)
    )
    assert summary.both_available_count == 0
    assert summary.mean_absolute_delta_difference is None


def test_validate_apex_greeks_delta_range_and_gamma_vega_are_clean_for_an_ordinary_chain() -> None:
    """Delta-range/gamma/vega non-negativity are guaranteed by the
    closed-form Black-Scholes-Merton formulas themselves for any valid
    (sigma > 0, t > 0, S > 0, K > 0) input, so these stay at zero even for
    an ordinary, not-specially-smoothed fixture chain. Delta-by-strike
    monotonicity is a *different*, weaker property -- it depends on the
    solved-IV curve across strikes actually being smooth, which an
    arbitrary hand-built chain is not guaranteed to be (genuine capture
    evidence confirmed real chains routinely show this too); it is
    exercised by its own dedicated tests below, not asserted zero here."""

    observations = _priced_chain()
    snapshot = _snapshot(observations)
    report = validate_apex_greeks(
        underlying_symbol="SPY", snapshot=snapshot, observations=observations
    )
    assert report.delta_range_violations == 0
    assert report.gamma_negative_violations == 0
    assert report.vega_negative_violations == 0
    assert report.contracts_checked > 0


def test_validate_apex_greeks_detects_an_out_of_range_delta() -> None:
    """Unit-tests the validator in isolation: a deliberately out-of-range
    delta is injected via ``dataclasses.replace`` (never producible by the
    real closed-form solver for valid inputs) to prove the check fires."""

    observations = _priced_chain()
    snapshot = _snapshot(observations)
    call_contract_id = next(
        item.contract.canonical_contract_id
        for item in observations
        if item.contract.option_type is OptionType.CALL
    )
    bad_estimates = tuple(
        replace(item, delta=Decimal("1.5"))
        if item.canonical_contract_id == call_contract_id
        else item
        for item in snapshot.greeks_estimates
    )
    mutated = replace(snapshot, greeks_estimates=bad_estimates)
    report = validate_apex_greeks(
        underlying_symbol="SPY", snapshot=mutated, observations=observations
    )
    assert report.delta_range_violations == 1
    assert "1_OF_" in report.findings[0]


def test_validate_apex_greeks_detects_delta_monotonicity_violation() -> None:
    calls = sorted(
        (item for item in _priced_chain() if item.contract.option_type is OptionType.CALL),
        key=lambda item: item.contract.strike,
    )
    assert len(calls) >= 2
    lowest_strike_id = calls[0].contract.canonical_contract_id
    highest_strike_id = calls[-1].contract.canonical_contract_id
    snapshot = _snapshot(_priced_chain())
    by_contract = {item.canonical_contract_id: item for item in snapshot.greeks_estimates}
    # Force the lowest-strike call's delta below the highest-strike call's
    # -- a call's delta must never *increase* with strike.
    bad_estimates = tuple(
        replace(item, delta=Decimal("0.05"))
        if item.canonical_contract_id == lowest_strike_id
        else replace(item, delta=Decimal("0.90"))
        if item.canonical_contract_id == highest_strike_id
        else item
        for item in snapshot.greeks_estimates
    )
    assert by_contract  # sanity: the chain actually produced estimates
    mutated = replace(snapshot, greeks_estimates=bad_estimates)
    report = validate_apex_greeks(
        underlying_symbol="SPY", snapshot=mutated, observations=_priced_chain()
    )
    assert report.delta_monotonicity_violations >= 1


def test_validate_apex_greeks_detects_negative_gamma() -> None:
    observations = _priced_chain()
    snapshot = _snapshot(observations)
    any_contract_id = snapshot.greeks_estimates[0].canonical_contract_id
    bad_estimates = tuple(
        replace(item, gamma=Decimal("-0.01"))
        if item.canonical_contract_id == any_contract_id
        else item
        for item in snapshot.greeks_estimates
    )
    mutated = replace(snapshot, greeks_estimates=bad_estimates)
    report = validate_apex_greeks(
        underlying_symbol="SPY", snapshot=mutated, observations=observations
    )
    assert report.gamma_negative_violations == 1


# ---------------------------------------------------------------------------
# Cross-sectional intelligence
# ---------------------------------------------------------------------------


def test_cross_sectional_summary_picks_the_correct_highest_and_lowest_atm_iv() -> None:
    low_iv_chain = tuple(
        _observation(strike=s, bid=b, ask=a)
        for s, b, a in (("480", "1.10", "1.30"), ("500", "5.00", "5.20"), ("520", "1.00", "1.10"))
    )
    high_iv_chain = tuple(
        _observation(strike=s, bid=b, ask=a)
        for s, b, a in (("480", "5.10", "5.60"), ("500", "20.00", "22.20"), ("520", "5.00", "5.90"))
    )
    low = create_options_chain_snapshot(
        plan_id="plan-1",
        run_id="run-1",
        underlying_canonical_security_id="security-low",
        underlying_symbol="LOWVOL",
        underlying_reference_price=Decimal("500"),
        underlying_price_source="ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
        captured_at=NOW,
        observations=tuple(
            replace(
                item,
                contract=replace(item.contract, underlying_canonical_security_id="security-low"),
            )
            for item in low_iv_chain
        ),
        as_of=date(2026, 8, 8),
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )
    high = create_options_chain_snapshot(
        plan_id="plan-1",
        run_id="run-1",
        underlying_canonical_security_id="security-high",
        underlying_symbol="HIGHVOL",
        underlying_reference_price=Decimal("500"),
        underlying_price_source="ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
        captured_at=NOW,
        observations=tuple(
            replace(
                item,
                contract=replace(item.contract, underlying_canonical_security_id="security-high"),
            )
            for item in high_iv_chain
        ),
        as_of=date(2026, 8, 8),
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )
    summary = build_cross_sectional_summary(generated_at=NOW, snapshots=(low, high))
    highest = next(item for item in summary.highlights if item.metric == "HIGHEST_ATM_IV")
    lowest = next(item for item in summary.highlights if item.metric == "LOWEST_ATM_IV")
    assert highest.underlying_symbol == "HIGHVOL"
    assert lowest.underlying_symbol == "LOWVOL"


def test_cross_sectional_summary_warns_rather_than_crashes_with_no_usable_snapshots() -> None:
    empty_snapshot = _snapshot(())
    summary = build_cross_sectional_summary(generated_at=NOW, snapshots=(empty_snapshot,))
    assert summary.underlyings_considered == 1
    assert any("UNAVAILABLE" in warning for warning in summary.warnings)
    assert summary.highlights == ()


def test_cross_sectional_summary_over_no_snapshots_warns_every_metric() -> None:
    summary = build_cross_sectional_summary(generated_at=NOW, snapshots=())
    assert summary.underlyings_considered == 0
    assert len(summary.warnings) >= 5
    assert summary.highlights == ()


# ---------------------------------------------------------------------------
# Risk-free-rate / dividend-yield assumption sensitivity
# ---------------------------------------------------------------------------


def test_assumption_sensitivity_retains_the_baseline_assumption_lineage() -> None:
    observation = _observation(strike="500", bid="9.00", ask="9.20")
    row = build_assumption_sensitivity_row(
        underlying_symbol="SPY", observation=observation, as_of=date(2026, 8, 8)
    )
    assert row is not None
    assert row.baseline_risk_free_rate == RISK_FREE_RATE_DEFAULT
    assert row.baseline_dividend_yield == Decimal("0")
    assert row.baseline_implied_volatility is not None


def test_assumption_sensitivity_is_none_without_a_valid_midpoint() -> None:
    observation = _observation(strike="500", bid=None, ask="9.20")
    row = build_assumption_sensitivity_row(
        underlying_symbol="SPY", observation=observation, as_of=date(2026, 8, 8)
    )
    assert row is None


def test_assumption_sensitivity_a_higher_alternate_rate_moves_call_and_put_iv_oppositely() -> None:
    call = _observation(strike="500", option_type=OptionType.CALL, bid="9.00", ask="9.20")
    put = _observation(strike="500", option_type=OptionType.PUT, bid="8.00", ask="8.20")
    call_row = build_assumption_sensitivity_row(
        underlying_symbol="SPY", observation=call, as_of=date(2026, 8, 8)
    )
    put_row = build_assumption_sensitivity_row(
        underlying_symbol="SPY", observation=put, as_of=date(2026, 8, 8)
    )
    assert call_row is not None and put_row is not None
    assert call_row.iv_rate_sensitivity is not None and put_row.iv_rate_sensitivity is not None
    # A higher discount rate raises the forward, requiring a *higher*
    # solved IV to reproduce a call's unchanged market price and a *lower*
    # solved IV to reproduce a put's -- opposite-signed sensitivities.
    assert (call_row.iv_rate_sensitivity > 0) != (put_row.iv_rate_sensitivity > 0)


# --------------------------------------------------------------------------
# select_contracts_stratified -- direct unit coverage (milestone: "Fresh
# Options Evidence Operations", section 5/required-tests "sampling"). Prior
# coverage was only indirect, through the Alpaca adapter's pagination
# tests using single-stratum synthetic data; these exercise genuine
# multi-expiration/multi-moneyness/multi-type breadth directly.
# --------------------------------------------------------------------------


def _dense_chain(
    *,
    underlying_price: Decimal = Decimal("500"),
    as_of: date = date(2026, 8, 10),
    expirations_dte: tuple[int, ...] = (3, 7, 14, 21, 30, 45, 60, 90),
    strikes_per_side: int = 20,
    strike_step: Decimal = Decimal("5"),
) -> tuple[ChainSampleCandidate, ...]:
    """A genuinely dense, SPY-like synthetic chain: many expirations, many
    strikes each side of the money, both option types -- 640 contracts for
    the default arguments, comfortably denser than any 200-contract cap."""

    candidates: list[ChainSampleCandidate] = []
    for dte in expirations_dte:
        expiration = (as_of + timedelta(days=dte)).isoformat()
        for offset in range(-strikes_per_side, strikes_per_side):
            strike = underlying_price + Decimal(offset) * strike_step
            if strike <= 0:
                continue
            for option_type in (OptionType.CALL, OptionType.PUT):
                symbol = (
                    f"SPY{expiration.replace('-', '')[2:]}"
                    f"{'C' if option_type is OptionType.CALL else 'P'}"
                    f"{int(strike * 1000):08d}"
                )
                candidates.append(ChainSampleCandidate(symbol, expiration, option_type, strike))
    return tuple(candidates)


class TestSelectContractsStratified:
    def test_fewer_candidates_than_cap_retains_all_unchanged(self) -> None:
        candidates = _dense_chain(expirations_dte=(7,), strikes_per_side=2)
        selection = select_contracts_stratified(
            candidates, underlying_price=Decimal("500"), captured_at=NOW, max_contracts=200
        )
        assert selection.contracts_available == len(candidates)
        assert selection.contracts_selected == len(candidates)
        assert selection.contracts_excluded_by_cap == 0
        assert set(selection.selected_contract_symbols) == {
            item.contract_symbol for item in candidates
        }

    def test_dense_chain_preserves_expiration_breadth_not_starved_to_nearest_few(self) -> None:
        candidates = _dense_chain()  # 8 expirations, 640 total candidates
        distinct_expirations = {item.expiration for item in candidates}
        assert len(distinct_expirations) == 8
        selection = select_contracts_stratified(
            candidates, underlying_price=Decimal("500"), captured_at=NOW, max_contracts=200
        )
        assert selection.contracts_available == 640
        assert selection.contracts_selected == 200
        assert selection.contracts_excluded_by_cap == 440
        # The regression this proves: a naive "first 200 returned" truncation
        # of a provider response ordered by expiration would silently
        # consume the entire budget on the one or two nearest expirations.
        # Stratified selection must instead retain representation from
        # every expiration that exists in the eligible pool.
        assert selection.expiration_count_available == 8
        assert selection.expiration_count_selected == 8
        selected_expirations = {
            item.split("SPY")[1][:6] for item in selection.selected_contract_symbols
        }
        assert len(selected_expirations) == 8

    def test_dense_chain_preserves_moneyness_breadth(self) -> None:
        candidates = _dense_chain()
        selection = select_contracts_stratified(
            candidates, underlying_price=Decimal("500"), captured_at=NOW, max_contracts=200
        )
        moneyness_keys = {
            item.key for item in selection.coverage_by_moneyness if item.contracts_selected > 0
        }
        assert moneyness_keys == {"ITM", "ATM", "OTM"}

    def test_dense_chain_preserves_call_put_balance(self) -> None:
        candidates = _dense_chain()
        selection = select_contracts_stratified(
            candidates, underlying_price=Decimal("500"), captured_at=NOW, max_contracts=200
        )
        by_type = {item.key: item.contracts_selected for item in selection.coverage_by_option_type}
        assert by_type.get("CALL", 0) > 0
        assert by_type.get("PUT", 0) > 0
        # Symmetric synthetic chain (equal calls/puts at every strike) ->
        # the deterministic round-robin should split close to evenly, not
        # exhaust one side before touching the other.
        assert abs(by_type["CALL"] - by_type["PUT"]) <= 2

    def test_dte_bucket_breadth_is_retained_across_a_wide_expiration_range(self) -> None:
        candidates = _dense_chain()
        selection = select_contracts_stratified(
            candidates, underlying_price=Decimal("500"), captured_at=NOW, max_contracts=200
        )
        selected_buckets = {
            item.key
            for item in selection.coverage_by_expiration_bucket
            if item.contracts_selected > 0
        }
        # dte=(3,7,14,21,30,45,60,90) spans DAYS_0_7, DAYS_8_30, DAYS_31_60,
        # DAYS_61_120 -- four distinct buckets, all must survive the cap.
        assert selected_buckets == {"DAYS_0_7", "DAYS_8_30", "DAYS_31_60", "DAYS_61_120"}

    def test_cap_constrained_underlying_reports_honest_exclusion_counts(self) -> None:
        candidates = _dense_chain()
        selection = select_contracts_stratified(
            candidates, underlying_price=Decimal("500"), captured_at=NOW, max_contracts=50
        )
        assert selection.contracts_selected == 50
        assert selection.contracts_excluded_by_cap == 590
        assert selection.expiration_count_selected >= 1
        # Even a tight cap must not collapse to a single stratum: multiple
        # expirations should still be represented via the round-robin.
        assert selection.expiration_count_selected > 1

    def test_deduplicates_repeated_contract_symbols(self) -> None:
        base = _dense_chain(expirations_dte=(7,), strikes_per_side=3)
        duplicated = base + base  # same provider page returned twice
        selection = select_contracts_stratified(
            duplicated, underlying_price=Decimal("500"), captured_at=NOW, max_contracts=200
        )
        assert selection.contracts_available == len(base)
        assert len(selection.selected_contract_symbols) == len(
            set(selection.selected_contract_symbols)
        )

    def test_selection_is_deterministic_across_repeated_calls(self) -> None:
        candidates = _dense_chain()
        first = select_contracts_stratified(
            candidates, underlying_price=Decimal("500"), captured_at=NOW, max_contracts=200
        )
        second = select_contracts_stratified(
            candidates, underlying_price=Decimal("500"), captured_at=NOW, max_contracts=200
        )
        assert first.selected_contract_symbols == second.selected_contract_symbols

    def test_coverage_by_expiration_date_reports_exact_dates_not_only_coarse_buckets(self) -> None:
        candidates = _dense_chain(expirations_dte=(7, 14))
        selection = select_contracts_stratified(
            candidates, underlying_price=Decimal("500"), captured_at=NOW, max_contracts=200
        )
        exact_dates = {item.key for item in selection.coverage_by_expiration_date}
        expected = {(date(2026, 8, 10) + timedelta(days=d)).isoformat() for d in (7, 14)}
        assert exact_dates == expected
        assert selection.expiration_count_available == 2

    def test_max_contracts_below_one_is_rejected(self) -> None:
        with pytest.raises(OptionsQuoteIntelligenceError):
            select_contracts_stratified(
                (), underlying_price=Decimal("500"), captured_at=NOW, max_contracts=0
            )
