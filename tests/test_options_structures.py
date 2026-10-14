"""Defined-Risk Options Structure Economics and Research Integration v1:
domain-level proofs.

No database, no network, no real market data -- every fixture below is a
synthetic but internally-consistent two-sided quote chain, built with the
identical ``domain.options_intelligence``/``domain.options_quote_
intelligence`` constructors the genuine capture path uses.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.options_intelligence import (
    ExerciseStyle,
    OptionContract,
    OptionType,
    SettlementStyle,
)
from apex.domain.options_quote_intelligence import (
    STALENESS_THRESHOLD_SECONDS,
    OptionQuoteObservation,
    OptionsChainSnapshot,
    QuoteObservationStatus,
    create_options_chain_snapshot,
)
from apex.domain.options_structures import (
    ALL_STRUCTURE_TYPES,
    EXPECTED_VALUE_UNAVAILABLE,
    FEE_EVIDENCE_INCOMPLETE,
    LegRole,
    OptionsFeePolicy,
    OptionsSlippagePolicy,
    OptionsStructureError,
    OptionsStructureResearchPolicy,
    OptionStructureCandidate,
    RejectedStructureCandidate,
    ScenarioLabel,
    StructureFamily,
    StructurePricingPolicy,
    StructureQualityLabel,
    StructureResearchFeatures,
    StructureResearchSnapshot,
    StructureType,
    build_structure_economics,
    build_structure_greeks,
    build_structure_quality,
    build_structure_scenarios,
    compare_structure_expressions,
    evaluate_vertical_eligibility,
    generate_structure_candidates,
    option_leg_terminal_value,
    structure_payoff_at,
    structure_type_family,
    structure_type_option_type,
)

NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)  # a Monday -- fresh by construction
AS_OF = NOW.date()
UNDERLYING_ID = "security-spy-stable"
UNDERLYING_SYMBOL = "SPY"
EXPIRATION = "2026-08-21"


def make_contract(
    *, option_type: OptionType, strike: Decimal | str, expiration: str = EXPIRATION
) -> OptionContract:
    strike_decimal = Decimal(strike)
    side = "C" if option_type is OptionType.CALL else "P"
    return OptionContract.create(
        provider_contract_id=(
            f"SPY{expiration.replace('-', '')[2:]}{side}{int(strike_decimal * 1000):08d}"
        ),
        underlying_canonical_security_id=UNDERLYING_ID,
        standardized_symbol=None,
        option_type=option_type,
        strike=strike_decimal,
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


def make_observation(
    *,
    option_type: OptionType,
    strike: Decimal | str,
    bid: str | None,
    ask: str | None,
    expiration: str = EXPIRATION,
    bid_size: str | None = "10",
    ask_size: str | None = "10",
    quote_timestamp: datetime = NOW,
    captured_at: datetime = NOW,
    underlying_reference_price: str = "500",
    delta: str | None = None,
    gamma: str | None = None,
    theta: str | None = None,
    vega: str | None = None,
    iv: str | None = None,
) -> OptionQuoteObservation:
    return OptionQuoteObservation.create(
        contract=make_contract(option_type=option_type, strike=strike, expiration=expiration),
        expected_underlying_canonical_security_id=UNDERLYING_ID,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        last_trade=None,
        trade_size=None,
        underlying_reference_price=underlying_reference_price,
        volume=None,
        open_interest=None,
        provider_implied_volatility=iv,
        provider_delta=delta,
        provider_gamma=gamma,
        provider_theta=theta,
        provider_vega=vega,
        provider_rho=None,
        market_status="UNKNOWN",
        quote_timestamp=quote_timestamp,
        availability_timestamp=captured_at,
        ingestion_timestamp=captured_at,
        captured_at=captured_at,
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )


def standard_chain(*, captured_at: datetime = NOW) -> tuple[OptionQuoteObservation, ...]:
    """A small, symmetric, genuinely two-sided chain: three call and three
    put strikes straddling a 500 underlying, all fresh and fully quoted."""

    return (
        make_observation(
            option_type=OptionType.CALL,
            strike="495",
            bid="9.00",
            ask="9.20",
            captured_at=captured_at,
        ),
        make_observation(
            option_type=OptionType.CALL,
            strike="500",
            bid="6.00",
            ask="6.20",
            captured_at=captured_at,
        ),
        make_observation(
            option_type=OptionType.CALL,
            strike="505",
            bid="4.00",
            ask="4.20",
            captured_at=captured_at,
        ),
        make_observation(
            option_type=OptionType.PUT,
            strike="495",
            bid="4.00",
            ask="4.20",
            captured_at=captured_at,
        ),
        make_observation(
            option_type=OptionType.PUT,
            strike="500",
            bid="6.00",
            ask="6.20",
            captured_at=captured_at,
        ),
        make_observation(
            option_type=OptionType.PUT,
            strike="505",
            bid="9.00",
            ask="9.20",
            captured_at=captured_at,
        ),
    )


def make_snapshot(
    observations: tuple[OptionQuoteObservation, ...], *, captured_at: datetime = NOW
) -> OptionsChainSnapshot:
    return create_options_chain_snapshot(
        plan_id="plan-1",
        run_id="run-1",
        underlying_canonical_security_id=UNDERLYING_ID,
        underlying_symbol=UNDERLYING_SYMBOL,
        underlying_reference_price=Decimal("500"),
        underlying_price_source="TEST",
        captured_at=captured_at,
        observations=observations,
        as_of=captured_at.date(),
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )


def default_policies(
    **overrides: object,
) -> tuple[
    StructurePricingPolicy, OptionsFeePolicy, OptionsSlippagePolicy, OptionsStructureResearchPolicy
]:
    pricing = StructurePricingPolicy.create()
    fee = OptionsFeePolicy.create()
    slippage = OptionsSlippagePolicy.create()
    structure = OptionsStructureResearchPolicy.create(
        pricing_policy=pricing,
        fee_policy=fee,
        slippage_policy=slippage,
        created_at=NOW,
        **overrides,
    )
    return pricing, fee, slippage, structure


def build(
    observations: tuple[OptionQuoteObservation, ...] = (),
    *,
    snapshot: OptionsChainSnapshot | None = None,
    captured_at: datetime = NOW,
    **policy_overrides: object,
):
    obs = observations or standard_chain(captured_at=captured_at)
    snap = snapshot or make_snapshot(obs, captured_at=captured_at)
    _pricing, fee, slippage, structure_policy = default_policies(**policy_overrides)
    result = generate_structure_candidates(
        snapshot=snap,
        observations=obs,
        structure_policy=structure_policy,
        fee_policy=fee,
        slippage_policy=slippage,
        as_of=captured_at.date(),
        evaluated_at=captured_at,
    )
    return result, snap, structure_policy, fee, slippage


def find_one(
    candidates: tuple[OptionStructureCandidate, ...],
    structure_type: StructureType,
    *,
    long_strike: str | None = None,
    short_strike: str | None = None,
) -> OptionStructureCandidate:
    for item in candidates:
        if item.structure_type is not structure_type:
            continue
        if long_strike is not None and item.long_leg.strike != Decimal(long_strike):
            continue
        if short_strike is not None and item.short_leg.strike != Decimal(short_strike):
            continue
        return item
    raise AssertionError(f"no matching candidate for {structure_type}")


# ==========================================================================
# Structure vocabulary / family mapping
# ==========================================================================


def test_exactly_four_structure_types_declared() -> None:
    assert set(ALL_STRUCTURE_TYPES) == {
        StructureType.CALL_DEBIT_VERTICAL,
        StructureType.PUT_DEBIT_VERTICAL,
        StructureType.CALL_CREDIT_VERTICAL,
        StructureType.PUT_CREDIT_VERTICAL,
    }


@pytest.mark.parametrize(
    ("structure_type", "expected_family", "expected_option_type"),
    (
        (StructureType.CALL_DEBIT_VERTICAL, StructureFamily.DEBIT, OptionType.CALL),
        (StructureType.PUT_DEBIT_VERTICAL, StructureFamily.DEBIT, OptionType.PUT),
        (StructureType.CALL_CREDIT_VERTICAL, StructureFamily.CREDIT, OptionType.CALL),
        (StructureType.PUT_CREDIT_VERTICAL, StructureFamily.CREDIT, OptionType.PUT),
    ),
)
def test_structure_type_family_and_option_type_mapping(
    structure_type: StructureType,
    expected_family: StructureFamily,
    expected_option_type: OptionType,
) -> None:
    assert structure_type_family(structure_type) is expected_family
    assert structure_type_option_type(structure_type) is expected_option_type


# ==========================================================================
# Structure validity / eligibility (milestone section 3)
# ==========================================================================


class TestEligibility:
    def _policy(self) -> OptionsStructureResearchPolicy:
        return default_policies()[3]

    def test_call_debit_vertical_valid_pairing_is_accepted(self) -> None:
        long_leg = make_observation(
            option_type=OptionType.CALL, strike="495", bid="9.00", ask="9.20"
        )
        short_leg = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert result.accepted
        assert result.reasons == ()

    def test_put_debit_vertical_valid_pairing_is_accepted(self) -> None:
        long_leg = make_observation(
            option_type=OptionType.PUT, strike="500", bid="6.00", ask="6.20"
        )
        short_leg = make_observation(
            option_type=OptionType.PUT, strike="495", bid="4.00", ask="4.20"
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.PUT_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert result.accepted

    def test_call_credit_vertical_valid_pairing_is_accepted(self) -> None:
        long_leg = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        short_leg = make_observation(
            option_type=OptionType.CALL, strike="495", bid="9.00", ask="9.20"
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_CREDIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert result.accepted

    def test_put_credit_vertical_valid_pairing_is_accepted(self) -> None:
        long_leg = make_observation(
            option_type=OptionType.PUT, strike="495", bid="4.00", ask="4.20"
        )
        short_leg = make_observation(
            option_type=OptionType.PUT, strike="500", bid="6.00", ask="6.20"
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.PUT_CREDIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert result.accepted

    def test_invalid_strike_order_is_rejected_never_silently_repaired(self) -> None:
        # Legs reversed relative to CALL_DEBIT_VERTICAL's required ordering.
        long_leg = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        short_leg = make_observation(
            option_type=OptionType.CALL, strike="495", bid="9.00", ask="9.20"
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert not result.accepted
        assert "INVALID_STRIKE_ORDER" in result.reasons

    def test_identical_strikes_rejected(self) -> None:
        long_leg = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        short_leg = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert not result.accepted
        assert "IDENTICAL_STRIKES" in result.reasons

    def test_expiration_mismatch_is_rejected(self) -> None:
        long_leg = make_observation(
            option_type=OptionType.CALL,
            strike="495",
            bid="9.00",
            ask="9.20",
            expiration="2026-08-21",
        )
        short_leg = make_observation(
            option_type=OptionType.CALL,
            strike="500",
            bid="6.00",
            ask="6.20",
            expiration="2026-09-18",
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert not result.accepted
        assert "EXPIRATION_MISMATCH" in result.reasons

    def test_option_type_mismatch_is_rejected(self) -> None:
        long_leg = make_observation(
            option_type=OptionType.CALL, strike="495", bid="9.00", ask="9.20"
        )
        short_leg = make_observation(
            option_type=OptionType.PUT, strike="500", bid="6.00", ask="6.20"
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert not result.accepted
        assert "OPTION_TYPE_MISMATCH" in result.reasons

    def test_one_sided_quote_is_rejected(self) -> None:
        long_leg = make_observation(
            option_type=OptionType.CALL, strike="495", bid="9.00", ask="9.20"
        )
        short_leg = make_observation(
            option_type=OptionType.CALL, strike="500", bid=None, ask="6.20"
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert not result.accepted
        assert "SHORT_LEG_QUOTE_NOT_TWO_SIDED" in result.reasons

    def test_stale_quote_is_rejected(self) -> None:
        stale_time = NOW - timedelta(seconds=STALENESS_THRESHOLD_SECONDS + 1)
        long_leg = make_observation(
            option_type=OptionType.CALL,
            strike="495",
            bid="9.00",
            ask="9.20",
            quote_timestamp=stale_time,
        )
        short_leg = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert not result.accepted
        assert "LONG_LEG_QUOTE_STALE" in result.reasons

    def test_missing_size_is_rejected(self) -> None:
        long_leg = make_observation(
            option_type=OptionType.CALL, strike="495", bid="9.00", ask="9.20", bid_size=None
        )
        short_leg = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert not result.accepted
        assert "LONG_LEG_SIZE_MISSING" in result.reasons

    def test_incompatible_chain_snapshot_evidence_is_rejected(self) -> None:
        long_leg = make_observation(
            option_type=OptionType.CALL, strike="495", bid="9.00", ask="9.20"
        )
        short_leg = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW + timedelta(hours=1),
        )
        assert not result.accepted
        assert "INCOMPATIBLE_CHAIN_SNAPSHOT_EVIDENCE" in result.reasons

    def test_dte_out_of_policy_range_is_rejected(self) -> None:
        far_expiration = (AS_OF + timedelta(days=400)).isoformat()
        long_leg = make_observation(
            option_type=OptionType.CALL,
            strike="495",
            bid="9.00",
            ask="9.20",
            expiration=far_expiration,
        )
        short_leg = make_observation(
            option_type=OptionType.CALL,
            strike="500",
            bid="6.00",
            ask="6.20",
            expiration=far_expiration,
        )
        result = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert not result.accepted
        assert "DTE_OUT_OF_POLICY_RANGE" in result.reasons

    def test_stale_quote_rejected_by_default_but_allowed_when_policy_opts_in(self) -> None:
        stale_time = NOW - timedelta(seconds=STALENESS_THRESHOLD_SECONDS + 1)
        long_leg = make_observation(
            option_type=OptionType.CALL,
            strike="495",
            bid="9.00",
            ask="9.20",
            quote_timestamp=stale_time,
        )
        short_leg = make_observation(
            option_type=OptionType.CALL,
            strike="500",
            bid="6.00",
            ask="6.20",
            quote_timestamp=stale_time,
        )
        strict = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=self._policy(),
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert not strict.accepted
        assert "LONG_LEG_QUOTE_STALE" in strict.reasons
        assert "SHORT_LEG_QUOTE_STALE" in strict.reasons

        _pricing, fee, slippage, _default_policy = default_policies()
        research_policy = OptionsStructureResearchPolicy.create(
            pricing_policy=_pricing,
            fee_policy=fee,
            slippage_policy=slippage,
            created_at=NOW,
            allow_stale_quotes_for_research=True,
        )
        permissive = evaluate_vertical_eligibility(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_observation=long_leg,
            short_observation=short_leg,
            policy=research_policy,
            as_of=AS_OF,
            chain_snapshot_captured_at=NOW,
        )
        assert permissive.accepted
        assert permissive.reasons == ()


# ==========================================================================
# Economics (milestone sections 6-9)
# ==========================================================================


class TestEconomics:
    def test_call_debit_vertical_economics_are_algebraically_correct(self) -> None:
        result, *_ = build()
        candidate = find_one(
            result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        economics = candidate.economics
        # gross debit = (ask_long - bid_short) * 100 = (9.20 - 6.00) * 100
        assert economics.gross_amount == Decimal("320.00000000")
        assert economics.fees.opening_cost == Decimal("1.30000000")  # 0.65 * 2 legs * 1 contract
        assert economics.slippage.total_slippage == Decimal("10.00000000")
        assert economics.capital_required == Decimal("331.30000000")
        assert economics.maximum_loss == economics.capital_required
        assert economics.maximum_profit == Decimal("168.70000000")  # 500 width - 331.30
        assert economics.breakeven == Decimal("498.31300000")
        assert economics.reward_risk_ratio == Decimal("0.50920616")  # 168.70 / 331.30, rounded

    def test_put_debit_vertical_economics_are_algebraically_correct(self) -> None:
        result, *_ = build()
        candidate = find_one(
            result.evaluated,
            StructureType.PUT_DEBIT_VERTICAL,
            long_strike="500",
            short_strike="495",
        )
        economics = candidate.economics
        assert economics.gross_amount == Decimal("220.00000000")
        assert economics.capital_required == Decimal("231.30000000")
        assert economics.maximum_profit == Decimal("268.70000000")
        assert economics.breakeven == Decimal("497.68700000")

    def test_call_credit_vertical_economics_are_algebraically_correct(self) -> None:
        result, *_ = build()
        candidate = find_one(
            result.evaluated,
            StructureType.CALL_CREDIT_VERTICAL,
            long_strike="500",
            short_strike="495",
        )
        economics = candidate.economics
        # gross credit = (bid_short - ask_long) * 100 = (9.00 - 6.20) * 100
        assert economics.gross_amount == Decimal("280.00000000")
        assert economics.net_amount == Decimal("268.70000000")
        assert economics.maximum_profit == economics.net_amount
        assert economics.maximum_loss == Decimal("231.30000000")
        assert economics.capital_required == economics.maximum_loss
        assert economics.breakeven == Decimal("497.68700000")

    def test_put_credit_vertical_economics_are_algebraically_correct(self) -> None:
        result, *_ = build()
        candidate = find_one(
            result.evaluated,
            StructureType.PUT_CREDIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        economics = candidate.economics
        assert economics.gross_amount == Decimal("180.00000000")
        assert economics.net_amount == Decimal("168.70000000")
        assert economics.maximum_loss == Decimal("331.30000000")
        assert economics.breakeven == Decimal("498.31300000")

    def test_fee_policy_defaults_to_fee_evidence_incomplete_never_zero(self) -> None:
        policy = OptionsFeePolicy.create()
        assert policy.fee_evidence_status == FEE_EVIDENCE_INCOMPLETE
        assert policy.per_contract_fee > 0

    def test_fee_evidence_status_must_be_a_declared_value(self) -> None:
        with pytest.raises(OptionsStructureError):
            OptionsFeePolicy.create(fee_evidence_status="TOTALLY_MADE_UP")

    def test_slippage_side_price_only_requires_zero_fraction(self) -> None:
        with pytest.raises(OptionsStructureError):
            OptionsSlippagePolicy.create(
                methodology="SIDE_PRICE_ONLY", adverse_fraction_of_spread="0.1"
            )
        zero_slippage = OptionsSlippagePolicy.create(
            methodology="SIDE_PRICE_ONLY", adverse_fraction_of_spread="0"
        )
        assert zero_slippage.adverse_fraction_of_spread == 0

    def test_zero_slippage_policy_yields_zero_modeled_slippage(self) -> None:
        pricing = StructurePricingPolicy.create()
        fee = OptionsFeePolicy.create()
        zero_slippage = OptionsSlippagePolicy.create(
            methodology="SIDE_PRICE_ONLY", adverse_fraction_of_spread="0"
        )
        structure_policy = OptionsStructureResearchPolicy.create(
            pricing_policy=pricing, fee_policy=fee, slippage_policy=zero_slippage, created_at=NOW
        )
        snapshot = make_snapshot(standard_chain())
        zero_result = generate_structure_candidates(
            snapshot=snapshot,
            observations=standard_chain(),
            structure_policy=structure_policy,
            fee_policy=fee,
            slippage_policy=zero_slippage,
            as_of=AS_OF,
            evaluated_at=NOW,
        )
        candidate = find_one(
            zero_result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        assert candidate.economics.slippage.total_slippage == 0

    def test_invalid_gross_debit_is_rejected_not_clamped(self) -> None:
        long_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="495", bid="1.00", ask="1.20"
        )
        # A short leg priced *above* the long leg's ask makes the nominal
        # debit spread's raw pricing invalid (short bid > long ask).
        short_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="500", bid="5.00", ask="5.20"
        )
        _pricing, fee, slippage, _policy = default_policies()
        long_leg = _leg(long_leg_obs, LegRole.LONG)
        short_leg = _leg(short_leg_obs, LegRole.SHORT)
        outcome = build_structure_economics(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_leg=long_leg,
            short_leg=short_leg,
            quantity=1,
            fee_policy=fee,
            slippage_policy=slippage,
        )
        assert outcome.economics is None
        assert outcome.rejection_reasons == ("INVALID_GROSS_DEBIT_NONPOSITIVE",)

    def test_invalid_gross_credit_is_rejected_not_clamped(self) -> None:
        long_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="500", bid="5.00", ask="5.20"
        )
        short_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="495", bid="1.00", ask="1.20"
        )
        _pricing, fee, slippage, _policy = default_policies()
        long_leg = _leg(long_leg_obs, LegRole.LONG)
        short_leg = _leg(short_leg_obs, LegRole.SHORT)
        outcome = build_structure_economics(
            structure_type=StructureType.CALL_CREDIT_VERTICAL,
            long_leg=long_leg,
            short_leg=short_leg,
            quantity=1,
            fee_policy=fee,
            slippage_policy=slippage,
        )
        assert outcome.economics is None
        assert outcome.rejection_reasons == ("INVALID_GROSS_CREDIT_NONPOSITIVE",)

    def test_maximum_profit_nonpositive_is_rejected(self) -> None:
        # A debit so large relative to width (after fees/slippage) that no
        # profit remains: gross ~498.90 + fees + slippage > width (500).
        long_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="495", bid="4.90", ask="4.99"
        )
        short_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="500", bid="0.001", ask="0.05"
        )
        _pricing, fee, slippage, _policy = default_policies()
        long_leg = _leg(long_leg_obs, LegRole.LONG)
        short_leg = _leg(short_leg_obs, LegRole.SHORT)
        outcome = build_structure_economics(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_leg=long_leg,
            short_leg=short_leg,
            quantity=1,
            fee_policy=fee,
            slippage_policy=slippage,
        )
        assert outcome.economics is None
        assert outcome.rejection_reasons == ("MAXIMUM_PROFIT_NONPOSITIVE",)

    def test_net_credit_nonpositive_after_costs_is_rejected(self) -> None:
        # A razor-thin credit that fees/slippage fully consume.
        long_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="500", bid="1.00", ask="1.05"
        )
        short_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="495", bid="1.06", ask="1.10"
        )
        _pricing, fee, slippage, _policy = default_policies()
        long_leg = _leg(long_leg_obs, LegRole.LONG)
        short_leg = _leg(short_leg_obs, LegRole.SHORT)
        outcome = build_structure_economics(
            structure_type=StructureType.CALL_CREDIT_VERTICAL,
            long_leg=long_leg,
            short_leg=short_leg,
            quantity=1,
            fee_policy=fee,
            slippage_policy=slippage,
        )
        assert outcome.economics is None
        assert outcome.rejection_reasons == ("INVALID_NET_CREDIT_NONPOSITIVE",)

    def test_entry_price_unavailable_is_rejected(self) -> None:
        long_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="495", bid="9.00", ask=None
        )
        short_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        _pricing, fee, slippage, _policy = default_policies()
        long_leg = _leg(long_leg_obs, LegRole.LONG)
        short_leg = _leg(short_leg_obs, LegRole.SHORT)
        outcome = build_structure_economics(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_leg=long_leg,
            short_leg=short_leg,
            quantity=1,
            fee_policy=fee,
            slippage_policy=slippage,
        )
        assert outcome.economics is None
        assert outcome.rejection_reasons == ("ENTRY_PRICE_UNAVAILABLE",)

    def test_quantity_out_of_range_raises(self) -> None:
        long_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="495", bid="9.00", ask="9.20"
        )
        short_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        _pricing, fee, slippage, _policy = default_policies()
        long_leg = _leg(long_leg_obs, LegRole.LONG)
        short_leg = _leg(short_leg_obs, LegRole.SHORT)
        with pytest.raises(OptionsStructureError):
            build_structure_economics(
                structure_type=StructureType.CALL_DEBIT_VERTICAL,
                long_leg=long_leg,
                short_leg=short_leg,
                quantity=0,
                fee_policy=fee,
                slippage_policy=slippage,
            )


def _leg(observation: OptionQuoteObservation, role: LegRole):
    from apex.domain.options_structures import _build_leg, _SnapshotGreekLookups

    empty = _SnapshotGreekLookups({}, {}, {}, {}, {})
    return _build_leg(role=role, observation=observation, lookups=empty)


# ==========================================================================
# Payoff (milestone sections 10-11)
# ==========================================================================


class TestPayoff:
    def test_call_terminal_value(self) -> None:
        assert option_leg_terminal_value(
            option_type=OptionType.CALL,
            strike=Decimal("100"),
            underlying_price_at_expiration=Decimal("110"),
        ) == Decimal("10")
        assert option_leg_terminal_value(
            option_type=OptionType.CALL,
            strike=Decimal("100"),
            underlying_price_at_expiration=Decimal("90"),
        ) == Decimal("0")

    def test_put_terminal_value(self) -> None:
        assert option_leg_terminal_value(
            option_type=OptionType.PUT,
            strike=Decimal("100"),
            underlying_price_at_expiration=Decimal("90"),
        ) == Decimal("10")
        assert option_leg_terminal_value(
            option_type=OptionType.PUT,
            strike=Decimal("100"),
            underlying_price_at_expiration=Decimal("110"),
        ) == Decimal("0")

    @pytest.mark.parametrize(
        ("underlying_price", "expected_net"),
        (
            (Decimal("480"), Decimal("-331.30000000")),  # far below lower strike -> max loss
            (Decimal("495"), Decimal("-331.30000000")),  # at lower (long) strike -> max loss
            (Decimal("497.5"), Decimal("-81.30000000")),  # between strikes
            (Decimal("500"), Decimal("168.70000000")),  # at upper (short) strike -> max profit
            (
                Decimal("520"),
                Decimal("168.70000000"),
            ),  # far above upper strike -> max profit (capped)
        ),
    )
    def test_call_debit_vertical_payoff_across_the_full_range(
        self, underlying_price: Decimal, expected_net: Decimal
    ) -> None:
        result, *_ = build()
        candidate = find_one(
            result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        payoff = structure_payoff_at(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_strike=candidate.long_leg.strike,
            short_strike=candidate.short_leg.strike,
            multiplier=candidate.economics.multiplier,
            quantity=candidate.economics.quantity,
            initial_cash_flow=-candidate.economics.capital_required,
            underlying_price_at_expiration=underlying_price,
        )
        assert payoff.net_profit_loss == expected_net

    @pytest.mark.parametrize(
        ("underlying_price", "expected_net"),
        (
            (Decimal("480"), Decimal("-331.30000000")),
            (Decimal("495"), Decimal("-331.30000000")),
            (Decimal("497.5"), Decimal("-81.30000000")),
            (Decimal("500"), Decimal("168.70000000")),
            (Decimal("520"), Decimal("168.70000000")),
        ),
    )
    def test_put_credit_vertical_payoff_across_the_full_range(
        self, underlying_price: Decimal, expected_net: Decimal
    ) -> None:
        result, *_ = build()
        candidate = find_one(
            result.evaluated,
            StructureType.PUT_CREDIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        payoff = structure_payoff_at(
            structure_type=StructureType.PUT_CREDIT_VERTICAL,
            long_strike=candidate.long_leg.strike,
            short_strike=candidate.short_leg.strike,
            multiplier=candidate.economics.multiplier,
            quantity=candidate.economics.quantity,
            initial_cash_flow=candidate.economics.net_amount,
            underlying_price_at_expiration=underlying_price,
        )
        assert payoff.net_profit_loss == expected_net

    def test_payoff_rejects_negative_underlying_price(self) -> None:
        with pytest.raises(OptionsStructureError):
            structure_payoff_at(
                structure_type=StructureType.CALL_DEBIT_VERTICAL,
                long_strike=Decimal("495"),
                short_strike=Decimal("500"),
                multiplier=Decimal("100"),
                quantity=1,
                initial_cash_flow=Decimal("-100"),
                underlying_price_at_expiration=Decimal("-1"),
            )

    def test_payoff_at_strikes_are_retained_on_the_candidate(self) -> None:
        result, *_ = build()
        candidate = find_one(
            result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        assert len(candidate.payoff_at_strikes) == 2
        prices = {p.underlying_price_at_expiration for p in candidate.payoff_at_strikes}
        assert prices == {Decimal("495"), Decimal("500")}

    def test_no_early_exercise_limitation_is_explicit(self) -> None:
        result, *_ = build()
        candidate = result.evaluated[0]
        assert "EUROPEAN_STYLE_TERMINAL_PAYOFF" in candidate.terminal_payoff_model_note
        assert "no early exercise" in candidate.terminal_payoff_model_note.lower()


# ==========================================================================
# Greeks (milestone section 15)
# ==========================================================================


class TestGreeks:
    def test_net_greeks_are_long_minus_short_and_negate_when_legs_swap(self) -> None:
        result, *_ = build(standard_chain())
        long_candidate = find_one(
            result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        credit_candidate = find_one(
            result.evaluated,
            StructureType.CALL_CREDIT_VERTICAL,
            long_strike="500",
            short_strike="495",
        )
        greeks = build_structure_greeks(
            long_leg=long_candidate.long_leg, short_leg=long_candidate.short_leg
        )
        assert greeks.available
        assert (
            greeks.net_delta
            == long_candidate.long_leg.apex_delta - long_candidate.short_leg.apex_delta
        )
        # Swapping which leg is long/short (the credit variant of the same
        # two contracts) negates every net Greek.
        assert credit_candidate.greeks.net_delta == -long_candidate.greeks.net_delta
        assert credit_candidate.greeks.net_gamma == -long_candidate.greeks.net_gamma
        assert credit_candidate.greeks.net_theta == -long_candidate.greeks.net_theta
        assert credit_candidate.greeks.net_vega == -long_candidate.greeks.net_vega

    def test_greeks_unavailable_when_a_leg_has_no_apex_estimate(self) -> None:
        # DTE == 0 (same day as as_of) makes ApexGreeksEstimate.compute bail
        # out with every field None (see its own ``dte > 0`` guard).
        same_day = NOW.date().isoformat()
        observations = (
            make_observation(
                option_type=OptionType.CALL,
                strike="495",
                bid="9.00",
                ask="9.20",
                expiration=same_day,
            ),
            make_observation(
                option_type=OptionType.CALL,
                strike="500",
                bid="6.00",
                ask="6.20",
                expiration=same_day,
            ),
        )
        snapshot = make_snapshot(observations)
        _pricing, fee, slippage, policy = default_policies(dte_minimum=0)
        result = generate_structure_candidates(
            snapshot=snapshot,
            observations=observations,
            structure_policy=policy,
            fee_policy=fee,
            slippage_policy=slippage,
            as_of=AS_OF,
            evaluated_at=NOW,
        )
        candidate = find_one(
            result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        assert not candidate.greeks.available
        assert candidate.greeks.net_delta is None


# ==========================================================================
# Scenario analysis (milestone section 16)
# ==========================================================================


class TestScenarios:
    def test_seven_bounded_deterministic_scenarios(self) -> None:
        result, *_ = build()
        candidate = find_one(
            result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        labels = tuple(s.label for s in candidate.scenarios)
        assert labels == (
            ScenarioLabel.DOWN_EXPECTED_MOVE,
            ScenarioLabel.UNCHANGED,
            ScenarioLabel.UP_EXPECTED_MOVE,
            ScenarioLabel.AT_LOWER_STRIKE,
            ScenarioLabel.AT_UPPER_STRIKE,
            ScenarioLabel.FAR_BELOW_BOTH_STRIKES,
            ScenarioLabel.FAR_ABOVE_BOTH_STRIKES,
        )
        assert all(s.status == "COMPUTED" for s in candidate.scenarios)

    def test_unavailable_expected_move_reports_explicitly_never_fabricated(self) -> None:
        scenarios = build_structure_scenarios(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_strike=Decimal("495"),
            short_strike=Decimal("500"),
            multiplier=Decimal("100"),
            quantity=1,
            initial_cash_flow=Decimal("-100"),
            underlying_reference_price=Decimal("500"),
            expected_move_percent=None,
        )
        by_label = {s.label: s for s in scenarios}
        assert by_label[ScenarioLabel.DOWN_EXPECTED_MOVE].status == "EXPECTED_MOVE_UNAVAILABLE"
        assert by_label[ScenarioLabel.DOWN_EXPECTED_MOVE].payoff is None
        assert by_label[ScenarioLabel.UNCHANGED].status == "COMPUTED"


# ==========================================================================
# Quality (milestone section 17)
# ==========================================================================


class TestQuality:
    def test_high_quality_structure_from_tight_liquid_chain(self) -> None:
        result, *_ = build()
        candidate = find_one(
            result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        assert candidate.quality.label == StructureQualityLabel.HIGH_QUALITY
        assert candidate.quality.reasons == ()

    def test_stale_quote_forces_unusable(self) -> None:
        stale_time = NOW - timedelta(seconds=STALENESS_THRESHOLD_SECONDS + 1)
        long_leg_obs = make_observation(
            option_type=OptionType.CALL,
            strike="495",
            bid="9.00",
            ask="9.20",
            quote_timestamp=stale_time,
        )
        short_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        _pricing, fee, slippage, _policy = default_policies()
        long_leg = _leg(long_leg_obs, LegRole.LONG)
        short_leg = _leg(short_leg_obs, LegRole.SHORT)
        outcome = build_structure_economics(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_leg=long_leg,
            short_leg=short_leg,
            quantity=1,
            fee_policy=fee,
            slippage_policy=slippage,
        )
        assert outcome.economics is not None
        quality = build_structure_quality(
            long_leg=long_leg, short_leg=short_leg, width=Decimal("5"), economics=outcome.economics
        )
        assert quality.label == StructureQualityLabel.UNUSABLE
        assert "QUOTE_STALE" in quality.reasons

    def test_wide_relative_spread_degrades_quality(self) -> None:
        long_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="495", bid="5.00", ask="9.00"
        )
        short_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        _pricing, fee, slippage, _policy = default_policies()
        long_leg = _leg(long_leg_obs, LegRole.LONG)
        short_leg = _leg(short_leg_obs, LegRole.SHORT)
        outcome = build_structure_economics(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_leg=long_leg,
            short_leg=short_leg,
            quantity=1,
            fee_policy=fee,
            slippage_policy=slippage,
        )
        assert outcome.economics is not None
        quality = build_structure_quality(
            long_leg=long_leg, short_leg=short_leg, width=Decimal("5"), economics=outcome.economics
        )
        assert quality.label in (StructureQualityLabel.MARGINAL, StructureQualityLabel.UNUSABLE)
        assert "WIDE_RELATIVE_SPREAD" in quality.reasons

    def test_zero_displayed_size_degrades_quality(self) -> None:
        long_leg_obs = make_observation(
            option_type=OptionType.CALL,
            strike="495",
            bid="9.00",
            ask="9.20",
            bid_size="0",
            ask_size="0",
        )
        short_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="500", bid="6.00", ask="6.20"
        )
        _pricing, fee, slippage, _policy = default_policies()
        long_leg = _leg(long_leg_obs, LegRole.LONG)
        short_leg = _leg(short_leg_obs, LegRole.SHORT)
        outcome = build_structure_economics(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_leg=long_leg,
            short_leg=short_leg,
            quantity=1,
            fee_policy=fee,
            slippage_policy=slippage,
        )
        assert outcome.economics is not None
        quality = build_structure_quality(
            long_leg=long_leg, short_leg=short_leg, width=Decimal("5"), economics=outcome.economics
        )
        assert quality.label == StructureQualityLabel.UNUSABLE
        assert "ZERO_OR_UNKNOWN_DISPLAYED_SIZE" in quality.reasons

    def test_high_fee_burden_degrades_quality(self) -> None:
        # Tight relative spreads on both legs (so WIDE_RELATIVE_SPREAD does
        # not also fire) but a small gross amount (6.00), so the fixed
        # $1.30 fee alone is a large fraction of it: 1.30 / 6.00 > 0.20.
        long_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="499", bid="1.00", ask="1.01"
        )
        short_leg_obs = make_observation(
            option_type=OptionType.CALL, strike="500", bid="0.95", ask="0.96"
        )
        _pricing, fee, slippage, _policy = default_policies()
        long_leg = _leg(long_leg_obs, LegRole.LONG)
        short_leg = _leg(short_leg_obs, LegRole.SHORT)
        outcome = build_structure_economics(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_leg=long_leg,
            short_leg=short_leg,
            quantity=1,
            fee_policy=fee,
            slippage_policy=slippage,
        )
        assert outcome.economics is not None
        quality = build_structure_quality(
            long_leg=long_leg, short_leg=short_leg, width=Decimal("1"), economics=outcome.economics
        )
        assert quality.fee_burden is not None and quality.fee_burden > Decimal("0.20")
        assert "HIGH_FEE_BURDEN" in quality.reasons


# ==========================================================================
# Candidate generation (milestone sections 19-20)
# ==========================================================================


class TestCandidateGeneration:
    def test_generation_is_deterministic_across_repeated_runs(self) -> None:
        result_a, *_ = build()
        result_b, *_ = build()
        ids_a = tuple(sorted(item.structure_id for item in result_a.evaluated))
        ids_b = tuple(sorted(item.structure_id for item in result_b.evaluated))
        assert ids_a == ids_b
        assert result_a.report == result_b.report

    def test_all_four_structure_types_are_generated_from_a_symmetric_chain(self) -> None:
        result, *_ = build()
        assert {item.structure_type for item in result.evaluated} == set(ALL_STRUCTURE_TYPES)

    def test_every_considered_pair_is_evaluated_or_rejected_with_reasons(self) -> None:
        result, *_ = build()
        assert result.report.considered_pairs == result.report.evaluated_structures + len(
            result.rejected
        )
        for rejection in result.rejected:
            assert isinstance(rejection, RejectedStructureCandidate)
            assert rejection.reasons
            assert rejection.stage in ("ELIGIBILITY", "ECONOMICS")

    def test_candidate_pool_is_bounded_not_a_full_n_squared_scan(self) -> None:
        # 20 call strikes -> C(20, 2) = 190 raw pairs, but max_adjacent_
        # strike_span=1 must bound generation to adjacent strikes only.
        observations = tuple(
            make_observation(
                option_type=OptionType.CALL, strike=str(490 + i), bid="1.00", ask="1.20"
            )
            for i in range(20)
        )
        snapshot = make_snapshot(observations)
        _pricing, fee, slippage, policy = default_policies(max_adjacent_strike_span=1)
        result = generate_structure_candidates(
            snapshot=snapshot,
            observations=observations,
            structure_policy=policy,
            fee_policy=fee,
            slippage_policy=slippage,
            as_of=AS_OF,
            evaluated_at=NOW,
        )
        # Adjacent-only: 19 strike pairs * 2 structure types (debit+credit).
        assert result.report.potential_pairs == 19 * 2
        assert result.report.potential_pairs < 190 * 2

    def test_maximum_candidate_pairs_per_underlying_bounds_considered_pairs(self) -> None:
        observations = tuple(
            make_observation(
                option_type=OptionType.CALL, strike=str(480 + i), bid="1.00", ask="1.20"
            )
            for i in range(20)
        )
        snapshot = make_snapshot(observations)
        _pricing, fee, slippage, policy = default_policies(
            maximum_candidate_pairs_per_underlying=5, max_adjacent_strike_span=3
        )
        result = generate_structure_candidates(
            snapshot=snapshot,
            observations=observations,
            structure_policy=policy,
            fee_policy=fee,
            slippage_policy=slippage,
            as_of=AS_OF,
            evaluated_at=NOW,
        )
        assert result.report.considered_pairs == 5
        assert result.report.potential_pairs > result.report.considered_pairs

    def test_maximum_structures_per_underlying_bounds_retained_structures(self) -> None:
        _pricing, fee, slippage, policy = default_policies(maximum_structures_per_underlying=1)
        observations = standard_chain()
        snapshot = make_snapshot(observations)
        result = generate_structure_candidates(
            snapshot=snapshot,
            observations=observations,
            structure_policy=policy,
            fee_policy=fee,
            slippage_policy=slippage,
            as_of=AS_OF,
            evaluated_at=NOW,
        )
        assert len(result.evaluated) == 1
        assert any(
            "STRUCTURES_PER_UNDERLYING_CAP_REACHED" in item.reasons for item in result.rejected
        )

    def test_supported_structure_types_can_be_narrowed(self) -> None:
        _pricing, fee, slippage, policy = default_policies(
            supported_structure_types=(StructureType.CALL_DEBIT_VERTICAL,)
        )
        observations = standard_chain()
        snapshot = make_snapshot(observations)
        result = generate_structure_candidates(
            snapshot=snapshot,
            observations=observations,
            structure_policy=policy,
            fee_policy=fee,
            slippage_policy=slippage,
            as_of=AS_OF,
            evaluated_at=NOW,
        )
        assert {item.structure_type for item in result.evaluated} == {
            StructureType.CALL_DEBIT_VERTICAL
        }

    def test_moneyness_band_excludes_far_out_strikes(self) -> None:
        observations = (
            *standard_chain(),
            make_observation(option_type=OptionType.CALL, strike="800", bid="0.01", ask="0.02"),
        )
        _pricing, fee, slippage, policy = default_policies(moneyness_band="0.05")
        snapshot = make_snapshot(observations)
        result = generate_structure_candidates(
            snapshot=snapshot,
            observations=observations,
            structure_policy=policy,
            fee_policy=fee,
            slippage_policy=slippage,
            as_of=AS_OF,
            evaluated_at=NOW,
        )
        assert all(
            item.long_leg.strike != Decimal("800") and item.short_leg.strike != Decimal("800")
            for item in result.evaluated
        )

    def test_stale_evidence_acceptance_policy_retains_structures_all_carrying_the_limitation(
        self,
    ) -> None:
        """Milestone section 24's retained-evidence economics acceptance:
        an explicitly-labeled research policy can construct genuine
        structures from stale evidence -- proving construction/economics
        work end to end -- while every one of them is unmistakably marked
        unusable, never presented as current or executable. The default
        (production) policy, exercised by every other test in this class,
        continues to reject the identical stale evidence outright."""

        stale_time = NOW - timedelta(seconds=STALENESS_THRESHOLD_SECONDS + 1)
        observations = tuple(
            make_observation(
                option_type=obs.contract.option_type,
                strike=obs.contract.strike,
                bid=str(obs.bid),
                ask=str(obs.ask),
                quote_timestamp=stale_time,
            )
            for obs in standard_chain()
        )
        snapshot = make_snapshot(observations, captured_at=NOW)
        pricing = StructurePricingPolicy.create()
        fee = OptionsFeePolicy.create()
        slippage = OptionsSlippagePolicy.create()
        research_policy = OptionsStructureResearchPolicy.create(
            pricing_policy=pricing,
            fee_policy=fee,
            slippage_policy=slippage,
            created_at=NOW,
            version="STALE_EVIDENCE_ACCEPTANCE_RESEARCH_POLICY_V1",
            allow_stale_quotes_for_research=True,
            minimum_quote_quality=(
                QuoteObservationStatus.OBSERVED,
                QuoteObservationStatus.ZERO_LIQUIDITY,
                QuoteObservationStatus.STALE,
            ),
        )
        result = generate_structure_candidates(
            snapshot=snapshot,
            observations=observations,
            structure_policy=research_policy,
            fee_policy=fee,
            slippage_policy=slippage,
            as_of=AS_OF,
            evaluated_at=NOW,
        )
        assert result.evaluated, "stale-evidence acceptance policy must genuinely retain structures"
        for structure in result.evaluated:
            assert structure.long_leg.quote_status == QuoteObservationStatus.STALE
            assert structure.short_leg.quote_status == QuoteObservationStatus.STALE
            assert structure.quality.label == StructureQualityLabel.UNUSABLE
            assert "QUOTE_STALE" in structure.quality.reasons
            assert structure.authority == "NONE"
            # Economics are still genuinely computed, not fabricated.
            assert structure.economics.maximum_loss > 0
            assert structure.economics.maximum_profit > 0


# ==========================================================================
# Snapshot (milestone section 21)
# ==========================================================================


class TestResearchSnapshot:
    def test_replay_from_identical_evidence_and_policy_is_deterministic(self) -> None:
        result_a, snapshot, policy, *_ = build()
        result_b, *_ = build()
        snap_a = StructureResearchSnapshot.create(
            structure_policy=policy, snapshot=snapshot, result=result_a, evaluated_at=NOW
        )
        snap_b = StructureResearchSnapshot.create(
            structure_policy=policy,
            snapshot=snapshot,
            result=result_b,
            evaluated_at=NOW + timedelta(hours=3),
        )
        assert snap_a.research_snapshot_id == snap_b.research_snapshot_id

    def test_changed_quote_evidence_changes_identity(self) -> None:
        result_a, snapshot_a, policy, *_ = build()
        snap_a = StructureResearchSnapshot.create(
            structure_policy=policy, snapshot=snapshot_a, result=result_a, evaluated_at=NOW
        )
        moved_chain = tuple(
            make_observation(
                option_type=obs.contract.option_type,
                strike=obs.contract.strike,
                bid=str(Decimal(obs.bid) + Decimal("0.50")) if obs.bid is not None else None,
                ask=str(Decimal(obs.ask) + Decimal("0.50")) if obs.ask is not None else None,
                expiration=obs.contract.expiration,
            )
            for obs in standard_chain()
        )
        result_b, snapshot_b, _pricing_b, _fee_b, _slippage_b = build(moved_chain)
        snap_b = StructureResearchSnapshot.create(
            structure_policy=policy, snapshot=snapshot_b, result=result_b, evaluated_at=NOW
        )
        assert snap_a.research_snapshot_id != snap_b.research_snapshot_id

    def test_changed_policy_changes_identity(self) -> None:
        result_a, snapshot, policy_a, *_ = build()
        snap_a = StructureResearchSnapshot.create(
            structure_policy=policy_a, snapshot=snapshot, result=result_a, evaluated_at=NOW
        )
        result_b, _snapshot_b, policy_b, _fee_b, _slippage_b = build(
            maximum_structures_per_underlying=2
        )
        snap_b = StructureResearchSnapshot.create(
            structure_policy=policy_b, snapshot=snapshot, result=result_b, evaluated_at=NOW
        )
        assert policy_a.policy_id != policy_b.policy_id
        assert snap_a.research_snapshot_id != snap_b.research_snapshot_id

    def test_snapshot_carries_limitations_and_none_authority(self) -> None:
        result, snapshot, policy, *_ = build()
        research_snapshot = StructureResearchSnapshot.create(
            structure_policy=policy, snapshot=snapshot, result=result, evaluated_at=NOW
        )
        assert research_snapshot.authority == "NONE"
        assert research_snapshot.limitations
        assert any("EARLY_EXERCISE" in item for item in research_snapshot.limitations)


# ==========================================================================
# Structure identity (milestone section 2)
# ==========================================================================


class TestStructureIdentity:
    def test_changing_a_leg_changes_the_structure_id(self) -> None:
        result, *_ = build()
        debit = find_one(
            result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        wider_debit = find_one(
            result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="505",
        )
        assert debit.structure_id != wider_debit.structure_id

    def test_changing_cost_assumption_changes_the_structure_id(self) -> None:
        result_a, *_ = build()
        candidate_a = find_one(
            result_a.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        pricing = StructurePricingPolicy.create()
        fee_b = OptionsFeePolicy.create(per_contract_fee="2.00")
        slippage = OptionsSlippagePolicy.create()
        policy_b = OptionsStructureResearchPolicy.create(
            pricing_policy=pricing, fee_policy=fee_b, slippage_policy=slippage, created_at=NOW
        )
        observations = standard_chain()
        snapshot = make_snapshot(observations)
        result_b = generate_structure_candidates(
            snapshot=snapshot,
            observations=observations,
            structure_policy=policy_b,
            fee_policy=fee_b,
            slippage_policy=slippage,
            as_of=AS_OF,
            evaluated_at=NOW,
        )
        candidate_b = find_one(
            result_b.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        assert candidate_a.structure_id != candidate_b.structure_id
        # Same legs, same evidence -- only the cost assumption changed.
        assert (
            candidate_a.long_leg.canonical_contract_id == candidate_b.long_leg.canonical_contract_id
        )

    def test_identical_evidence_and_policy_replay_to_the_identical_structure_id(self) -> None:
        result_a, *_ = build()
        result_b, *_ = build()
        candidate_a = find_one(
            result_a.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        candidate_b = find_one(
            result_b.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        assert candidate_a.structure_id == candidate_b.structure_id

    def test_authority_is_none_and_expected_value_is_unavailable(self) -> None:
        result, *_ = build()
        candidate = result.evaluated[0]
        assert candidate.authority == "NONE"
        assert candidate.expected_value_state == EXPECTED_VALUE_UNAVAILABLE


# ==========================================================================
# Research seam (milestone section 22) -- feature evidence only
# ==========================================================================


class TestResearchFeatures:
    def test_features_are_derived_deterministically_from_the_candidate(self) -> None:
        result, *_ = build()
        candidate = find_one(
            result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        features_a = StructureResearchFeatures.from_candidate(candidate)
        features_b = StructureResearchFeatures.from_candidate(candidate)
        assert features_a.feature_id == features_b.feature_id
        assert features_a.maximum_loss == candidate.economics.maximum_loss
        assert features_a.maximum_profit == candidate.economics.maximum_profit
        assert features_a.expected_value_state == EXPECTED_VALUE_UNAVAILABLE
        assert features_a.quality_label == candidate.quality.label


# ==========================================================================
# Compare expression alternatives (milestone section 23)
# ==========================================================================


class TestComparison:
    def test_comparison_reports_separate_dimensions_never_a_single_best(self) -> None:
        result, *_ = build()
        narrower = find_one(
            result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="500",
        )
        wider = find_one(
            result.evaluated,
            StructureType.CALL_DEBIT_VERTICAL,
            long_strike="495",
            short_strike="505",
        )
        comparison = compare_structure_expressions((narrower, wider))
        labels = {dimension.label for dimension in comparison.dimensions}
        assert labels == {
            "LOWER_CAPITAL_REQUIRED",
            "HIGHER_MAXIMUM_REWARD",
            "LOWER_COST_BURDEN",
            "BETTER_LIQUIDITY",
        }
        for dimension in comparison.dimensions:
            assert dimension.winner_structure_id in (narrower.structure_id, wider.structure_id)
        assert "none is labeled the single best trade" in comparison.note.lower()

    def test_comparison_requires_at_least_two_candidates(self) -> None:
        result, *_ = build()
        with pytest.raises(OptionsStructureError):
            compare_structure_expressions((result.evaluated[0],))

    def test_comparison_requires_a_single_underlying(self) -> None:
        result, *_ = build()
        candidate = result.evaluated[0]
        other = replace(candidate, underlying_canonical_security_id="different-underlying")
        with pytest.raises(OptionsStructureError):
            compare_structure_expressions((candidate, other))
