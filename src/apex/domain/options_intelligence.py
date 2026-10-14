"""Provider-neutral, defined-risk options research evidence.

Only long options and fully covered same-expiration debit spreads are modeled.
Nothing in this module can create a broker order, position, reservation, journal,
PAPER authority, or LIVE authority.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.values import DecimalInput, content_digest, deterministic_id, parse_decimal

APEX_OPTION_CONTRACT_V1 = "APEX_OPTION_CONTRACT_V1"
APEX_OPTION_CHAIN_SNAPSHOT_V1 = "APEX_OPTION_CHAIN_SNAPSHOT_V1"
OPTIONS_INTELLIGENCE_FEATURES_V1 = "OPTIONS_INTELLIGENCE_FEATURES_V1"
STOCK_OPTION_EXPRESSION_COMPARISON_V1 = "STOCK_OPTION_EXPRESSION_COMPARISON_V1"
DEFINED_RISK_OPTIONS_RANKING_V1 = "DEFINED_RISK_OPTIONS_RANKING_V1"
DEFINED_RISK_OPTIONS_SHADOW_PROPOSAL_V1 = "DEFINED_RISK_OPTIONS_SHADOW_PROPOSAL_V1"
OPTIONS_WARNINGS = (
    "OPTIONS INTELLIGENCE IS RESEARCH ONLY",
    "OPTIONS CAN EXPIRE WORTHLESS",
    "ONLY DEFINED-RISK STRUCTURES ARE INCLUDED IN V1",
    "NO OPTION ORDER AUTHORITY IS ENABLED",
    "LIVE TRADING IS NOT ENABLED",
)


class OptionsIntelligenceError(ValueError):
    pass


class OptionType(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


class ExerciseStyle(StrEnum):
    AMERICAN = "AMERICAN"
    EUROPEAN = "EUROPEAN"
    UNKNOWN = "UNKNOWN"


class SettlementStyle(StrEnum):
    PHYSICAL = "PHYSICAL"
    CASH = "CASH"
    UNKNOWN = "UNKNOWN"


class CapabilityState(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    PAID_REFERENCE_REQUIRED = "PAID_REFERENCE_REQUIRED"
    UNKNOWN = "UNKNOWN"


class OptionsStrategyType(StrEnum):
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    CALL_DEBIT_SPREAD = "CALL_DEBIT_SPREAD"
    PUT_DEBIT_SPREAD = "PUT_DEBIT_SPREAD"


@dataclass(frozen=True, slots=True)
class OptionProviderCapabilities:
    provider: str
    contract_discovery: CapabilityState
    quotes: CapabilityState
    trades: CapabilityState
    snapshots: CapabilityState
    implied_volatility: CapabilityState
    greeks: CapabilityState
    open_interest: CapabilityState
    volume: CapabilityState
    multi_leg_paper: CapabilityState
    exercise_assignment_observation: CapabilityState
    limitations: tuple[str, ...]
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        contract_discovery: CapabilityState,
        quotes: CapabilityState,
        trades: CapabilityState,
        snapshots: CapabilityState,
        implied_volatility: CapabilityState,
        greeks: CapabilityState,
        open_interest: CapabilityState,
        volume: CapabilityState,
        multi_leg_paper: CapabilityState = CapabilityState.UNAVAILABLE,
        exercise_assignment_observation: CapabilityState = CapabilityState.UNKNOWN,
        limitations: tuple[str, ...] = (),
    ) -> OptionProviderCapabilities:
        payload = (
            provider,
            contract_discovery,
            quotes,
            trades,
            snapshots,
            implied_volatility,
            greeks,
            open_interest,
            volume,
            multi_leg_paper,
            exercise_assignment_observation,
            tuple(sorted(limitations)),
        )
        return cls(*payload[:-1], payload[-1], content_digest(payload))


@dataclass(frozen=True, slots=True)
class OptionContract:
    canonical_contract_id: str
    provider_contract_id: str
    underlying_canonical_security_id: str
    standardized_symbol: str | None
    option_type: OptionType
    strike: Decimal
    expiration: str
    exercise_style: ExerciseStyle
    settlement_style: SettlementStyle
    multiplier: Decimal
    deliverable: str | None
    listing_exchange: str | None
    state: str
    contract_size_state: str
    adjustment_state: str
    source_provider: str
    reference_receipt_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        provider_contract_id: str,
        underlying_canonical_security_id: str,
        standardized_symbol: str | None,
        option_type: OptionType | str,
        strike: DecimalInput,
        expiration: str,
        exercise_style: ExerciseStyle | str,
        settlement_style: SettlementStyle | str,
        multiplier: DecimalInput,
        deliverable: str | None,
        listing_exchange: str | None,
        state: str,
        contract_size_state: str,
        adjustment_state: str,
        source_provider: str,
        reference_receipt_ids: tuple[str, ...],
        limitations: tuple[str, ...] = (),
    ) -> OptionContract:
        parsed_strike = parse_decimal(strike, allow_zero=False)
        parsed_multiplier = parse_decimal(multiplier, allow_zero=False)
        try:
            date.fromisoformat(expiration)
        except ValueError as error:
            raise OptionsIntelligenceError("OPTION_EXPIRATION_INVALID") from error
        if (
            not provider_contract_id
            or not underlying_canonical_security_id
            or not source_provider
            or not reference_receipt_ids
        ):
            raise OptionsIntelligenceError("OPTION_CONTRACT_IDENTITY_REQUIRED")
        payload = (
            APEX_OPTION_CONTRACT_V1,
            underlying_canonical_security_id,
            standardized_symbol,
            OptionType(option_type),
            parsed_strike,
            expiration,
            ExerciseStyle(exercise_style),
            SettlementStyle(settlement_style),
            parsed_multiplier,
            deliverable,
            listing_exchange,
            state,
            contract_size_state,
            adjustment_state,
            source_provider,
            tuple(sorted(reference_receipt_ids)),
            tuple(sorted(limitations)),
        )
        return cls(
            deterministic_id("apex_option_contract", payload),
            provider_contract_id,
            underlying_canonical_security_id,
            standardized_symbol,
            OptionType(option_type),
            parsed_strike,
            expiration,
            ExerciseStyle(exercise_style),
            SettlementStyle(settlement_style),
            parsed_multiplier,
            deliverable,
            listing_exchange,
            state,
            contract_size_state,
            adjustment_state,
            source_provider,
            tuple(sorted(reference_receipt_ids)),
            tuple(sorted(limitations)),
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class OptionMarketEvidence:
    contract: OptionContract
    bid: Decimal | None
    ask: Decimal | None
    midpoint: Decimal | None
    spread: Decimal | None
    last_trade: Decimal | None
    volume: Decimal | None
    open_interest: Decimal | None
    implied_volatility: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    quote_timestamp: datetime
    staleness_seconds: int
    provider: str
    source_receipt_ids: tuple[str, ...]
    exclusions: tuple[str, ...]
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        contract: OptionContract,
        bid: DecimalInput | None,
        ask: DecimalInput | None,
        last_trade: DecimalInput | None,
        volume: DecimalInput | None,
        open_interest: DecimalInput | None,
        implied_volatility: DecimalInput | None,
        delta: DecimalInput | None,
        gamma: DecimalInput | None,
        theta: DecimalInput | None,
        vega: DecimalInput | None,
        quote_timestamp: datetime,
        staleness_seconds: int,
        provider: str,
        source_receipt_ids: tuple[str, ...],
    ) -> OptionMarketEvidence:
        if quote_timestamp.tzinfo is None or staleness_seconds < 0 or not source_receipt_ids:
            raise OptionsIntelligenceError("OPTION_MARKET_EVIDENCE_INVALID")
        parsed_bid = _optional_decimal(bid)
        parsed_ask = _optional_decimal(ask)
        exclusions: list[str] = []
        if parsed_bid is None or parsed_ask is None:
            exclusions.append("TWO_SIDED_QUOTE_REQUIRED")
        elif parsed_bid == 0:
            exclusions.append("ZERO_BID")
        elif parsed_ask < parsed_bid:
            exclusions.append("CROSSED_QUOTE")
        midpoint = (
            _exact((parsed_bid + parsed_ask) / 2)
            if parsed_bid is not None and parsed_ask is not None and parsed_ask >= parsed_bid
            else None
        )
        spread = (
            _exact(parsed_ask - parsed_bid)
            if parsed_bid is not None and parsed_ask is not None and parsed_ask >= parsed_bid
            else None
        )
        if staleness_seconds > 900:
            exclusions.append("STALE_QUOTE")
        parsed = (
            _optional_decimal(last_trade),
            _optional_decimal(volume),
            _optional_decimal(open_interest),
            _optional_decimal(implied_volatility),
            _optional_decimal(delta, allow_negative=True),
            _optional_decimal(gamma, allow_negative=True),
            _optional_decimal(theta, allow_negative=True),
            _optional_decimal(vega, allow_negative=True),
        )
        payload = (
            contract.canonical_hash,
            parsed_bid,
            parsed_ask,
            midpoint,
            spread,
            parsed,
            quote_timestamp,
            staleness_seconds,
            provider,
            tuple(sorted(source_receipt_ids)),
            tuple(sorted(exclusions)),
        )
        return cls(
            contract,
            parsed_bid,
            parsed_ask,
            midpoint,
            spread,
            parsed[0],
            parsed[1],
            parsed[2],
            parsed[3],
            parsed[4],
            parsed[5],
            parsed[6],
            parsed[7],
            quote_timestamp,
            staleness_seconds,
            provider,
            tuple(sorted(source_receipt_ids)),
            tuple(sorted(exclusions)),
            content_digest(payload),
        )


def _optional_decimal(
    value: DecimalInput | None, *, allow_negative: bool = False
) -> Decimal | None:
    return parse_decimal(value, allow_negative=allow_negative) if value is not None else None


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    chain_id: str
    underlying_canonical_security_id: str
    captured_at: datetime
    underlying_price: Decimal
    expiration_start: str
    expiration_end: str
    maximum_strike_distance: Decimal
    contracts: tuple[OptionMarketEvidence, ...]
    provider: str
    source_receipt_ids: tuple[str, ...]
    exclusions: tuple[str, ...]
    canonical_hash: str


def create_option_chain_snapshot(
    *,
    underlying_canonical_security_id: str,
    captured_at: datetime,
    underlying_price: DecimalInput,
    expiration_start: date,
    expiration_end: date,
    maximum_strike_distance: DecimalInput,
    maximum_contract_count: int,
    market_evidence: tuple[OptionMarketEvidence, ...],
    provider: str,
    source_receipt_ids: tuple[str, ...],
) -> OptionChainSnapshot:
    price = parse_decimal(underlying_price, allow_zero=False)
    distance = parse_decimal(maximum_strike_distance, allow_zero=False)
    if (
        captured_at.tzinfo is None
        or expiration_start > expiration_end
        or not 1 <= maximum_contract_count <= 500
        or len(market_evidence) > maximum_contract_count
        or not source_receipt_ids
    ):
        raise OptionsIntelligenceError("OPTION_CHAIN_BOUND_INVALID")
    included: list[OptionMarketEvidence] = []
    exclusions: list[str] = []
    seen: set[str] = set()
    for evidence in market_evidence:
        contract = evidence.contract
        if contract.canonical_contract_id in seen:
            raise OptionsIntelligenceError("DUPLICATE_OPTION_CONTRACT")
        seen.add(contract.canonical_contract_id)
        if contract.underlying_canonical_security_id != underlying_canonical_security_id:
            raise OptionsIntelligenceError("OPTION_CHAIN_UNDERLYING_MISMATCH")
        expiry = date.fromisoformat(contract.expiration)
        strike_distance = abs(contract.strike / price - 1)
        if not expiration_start <= expiry <= expiration_end:
            exclusions.append(f"{contract.canonical_contract_id}:EXPIRATION_OUT_OF_RANGE")
            continue
        if strike_distance > distance:
            exclusions.append(f"{contract.canonical_contract_id}:STRIKE_OUT_OF_RANGE")
            continue
        included.append(evidence)
    payload = (
        APEX_OPTION_CHAIN_SNAPSHOT_V1,
        underlying_canonical_security_id,
        captured_at,
        price,
        expiration_start.isoformat(),
        expiration_end.isoformat(),
        distance,
        tuple(item.canonical_hash for item in included),
        provider,
        tuple(sorted(source_receipt_ids)),
        tuple(sorted(exclusions)),
    )
    return OptionChainSnapshot(
        deterministic_id("apex_option_chain", payload),
        underlying_canonical_security_id,
        captured_at,
        price,
        expiration_start.isoformat(),
        expiration_end.isoformat(),
        distance,
        tuple(included),
        provider,
        tuple(sorted(source_receipt_ids)),
        tuple(sorted(exclusions)),
        content_digest(payload),
    )


@dataclass(frozen=True, slots=True)
class OptionLeg:
    canonical_contract_id: str
    side: str
    quantity: int
    strike: Decimal
    option_type: OptionType
    expiration: str


@dataclass(frozen=True, slots=True)
class DefinedRiskOptionCandidate:
    candidate_id: str
    rank: int
    underlying_canonical_security_id: str
    strategy_type: OptionsStrategyType
    legs: tuple[OptionLeg, ...]
    premium_or_debit: Decimal
    maximum_loss: Decimal
    maximum_gain: Decimal | None
    maximum_gain_classification: str
    break_even: Decimal
    moneyness: Decimal
    days_to_expiration: int
    spread_percentage: Decimal
    intrinsic_value: Decimal
    extrinsic_value: Decimal
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    liquidity_score: Decimal
    evidence_completeness: Decimal
    uncertainty: Decimal
    blockers: tuple[str, ...]
    canonical_hash: str


def evaluate_defined_risk_options(
    *,
    chain: OptionChainSnapshot,
    as_of_date: date,
    maximum_spread_percentage: DecimalInput = "0.25",
    minimum_open_interest: DecimalInput = "10",
) -> tuple[DefinedRiskOptionCandidate, ...]:
    maximum_spread = parse_decimal(maximum_spread_percentage, allow_zero=False)
    minimum_oi = parse_decimal(minimum_open_interest)
    candidates: list[DefinedRiskOptionCandidate] = []
    eligible = tuple(
        item
        for item in chain.contracts
        if not item.exclusions
        and item.ask is not None
        and item.bid is not None
        and item.ask > 0
        and (item.open_interest is None or item.open_interest >= minimum_oi)
    )
    for evidence in eligible:
        candidate = _long_candidate(
            chain=chain,
            evidence=evidence,
            as_of_date=as_of_date,
            maximum_spread=maximum_spread,
        )
        candidates.append(candidate)
    for index, long_evidence in enumerate(eligible):
        for short_evidence in eligible[index + 1 :]:
            if (
                long_evidence.contract.option_type is not short_evidence.contract.option_type
                or long_evidence.contract.expiration != short_evidence.contract.expiration
            ):
                continue
            ordered = _debit_spread_legs(long_evidence, short_evidence)
            if ordered is None:
                continue
            if (
                ordered[0].ask is None
                or ordered[1].bid is None
                or ordered[0].ask <= ordered[1].bid
                or ordered[0].ask - ordered[1].bid
                >= abs(ordered[1].contract.strike - ordered[0].contract.strike)
            ):
                continue
            candidate = _spread_candidate(
                chain=chain,
                long_evidence=ordered[0],
                short_evidence=ordered[1],
                as_of_date=as_of_date,
                maximum_spread=maximum_spread,
            )
            candidates.append(candidate)
    ranked = sorted(
        candidates,
        key=lambda item: (
            bool(item.blockers),
            item.uncertainty,
            item.spread_percentage,
            item.maximum_loss,
            item.candidate_id,
        ),
    )
    return tuple(
        DefinedRiskOptionCandidate(
            candidate.candidate_id,
            rank,
            candidate.underlying_canonical_security_id,
            candidate.strategy_type,
            candidate.legs,
            candidate.premium_or_debit,
            candidate.maximum_loss,
            candidate.maximum_gain,
            candidate.maximum_gain_classification,
            candidate.break_even,
            candidate.moneyness,
            candidate.days_to_expiration,
            candidate.spread_percentage,
            candidate.intrinsic_value,
            candidate.extrinsic_value,
            candidate.delta,
            candidate.gamma,
            candidate.theta,
            candidate.vega,
            candidate.liquidity_score,
            candidate.evidence_completeness,
            candidate.uncertainty,
            candidate.blockers,
            content_digest(
                (
                    DEFINED_RISK_OPTIONS_RANKING_V1,
                    rank,
                    candidate.canonical_hash,
                )
            ),
        )
        for rank, candidate in enumerate(ranked, start=1)
    )


def _long_candidate(
    *,
    chain: OptionChainSnapshot,
    evidence: OptionMarketEvidence,
    as_of_date: date,
    maximum_spread: Decimal,
) -> DefinedRiskOptionCandidate:
    contract = evidence.contract
    assert evidence.ask is not None and evidence.bid is not None
    premium = evidence.ask * contract.multiplier
    intrinsic_per_share = (
        max(Decimal(), chain.underlying_price - contract.strike)
        if contract.option_type is OptionType.CALL
        else max(Decimal(), contract.strike - chain.underlying_price)
    )
    break_even = (
        contract.strike + evidence.ask
        if contract.option_type is OptionType.CALL
        else contract.strike - evidence.ask
    )
    maximum_gain = (
        None
        if contract.option_type is OptionType.CALL
        else max(Decimal(), contract.strike * contract.multiplier - premium)
    )
    strategy = (
        OptionsStrategyType.LONG_CALL
        if contract.option_type is OptionType.CALL
        else OptionsStrategyType.LONG_PUT
    )
    return _candidate(
        chain=chain,
        strategy=strategy,
        legs=(_leg(evidence, "LONG"),),
        premium=premium,
        maximum_gain=maximum_gain,
        maximum_gain_classification="UNBOUNDED" if maximum_gain is None else "BOUNDED",
        break_even=break_even,
        intrinsic=intrinsic_per_share * contract.multiplier,
        evidence=(evidence,),
        as_of_date=as_of_date,
        maximum_spread=maximum_spread,
    )


def _debit_spread_legs(
    first: OptionMarketEvidence, second: OptionMarketEvidence
) -> tuple[OptionMarketEvidence, OptionMarketEvidence] | None:
    first_contract = first.contract
    second_contract = second.contract
    if first_contract.option_type is OptionType.CALL:
        return (
            (first, second) if first_contract.strike < second_contract.strike else (second, first)
        )
    return (first, second) if first_contract.strike > second_contract.strike else (second, first)


def _spread_candidate(
    *,
    chain: OptionChainSnapshot,
    long_evidence: OptionMarketEvidence,
    short_evidence: OptionMarketEvidence,
    as_of_date: date,
    maximum_spread: Decimal,
) -> DefinedRiskOptionCandidate:
    long_contract = long_evidence.contract
    short_contract = short_evidence.contract
    assert long_evidence.ask is not None and short_evidence.bid is not None
    if long_contract.multiplier != short_contract.multiplier:
        raise OptionsIntelligenceError("DEBIT_SPREAD_MULTIPLIER_MISMATCH")
    debit_per_share = long_evidence.ask - short_evidence.bid
    if debit_per_share <= 0:
        raise OptionsIntelligenceError("DEBIT_SPREAD_POSITIVE_NET_DEBIT_REQUIRED")
    multiplier = long_contract.multiplier
    width = abs(short_contract.strike - long_contract.strike)
    maximum_loss = debit_per_share * multiplier
    maximum_gain = width * multiplier - maximum_loss
    if maximum_gain <= 0:
        raise OptionsIntelligenceError("DEBIT_SPREAD_MAXIMUM_GAIN_MUST_BE_POSITIVE")
    if long_contract.option_type is OptionType.CALL:
        strategy = OptionsStrategyType.CALL_DEBIT_SPREAD
        break_even = long_contract.strike + debit_per_share
        intrinsic = max(Decimal(), chain.underlying_price - long_contract.strike) * multiplier
    else:
        strategy = OptionsStrategyType.PUT_DEBIT_SPREAD
        break_even = long_contract.strike - debit_per_share
        intrinsic = max(Decimal(), long_contract.strike - chain.underlying_price) * multiplier
    return _candidate(
        chain=chain,
        strategy=strategy,
        legs=(_leg(long_evidence, "LONG"), _leg(short_evidence, "SHORT_COVERED")),
        premium=maximum_loss,
        maximum_gain=maximum_gain,
        maximum_gain_classification="BOUNDED",
        break_even=break_even,
        intrinsic=intrinsic,
        evidence=(long_evidence, short_evidence),
        as_of_date=as_of_date,
        maximum_spread=maximum_spread,
    )


def _leg(evidence: OptionMarketEvidence, side: str) -> OptionLeg:
    contract = evidence.contract
    return OptionLeg(
        contract.canonical_contract_id,
        side,
        1,
        contract.strike,
        contract.option_type,
        contract.expiration,
    )


def _candidate(
    *,
    chain: OptionChainSnapshot,
    strategy: OptionsStrategyType,
    legs: tuple[OptionLeg, ...],
    premium: Decimal,
    maximum_gain: Decimal | None,
    maximum_gain_classification: str,
    break_even: Decimal,
    intrinsic: Decimal,
    evidence: tuple[OptionMarketEvidence, ...],
    as_of_date: date,
    maximum_spread: Decimal,
) -> DefinedRiskOptionCandidate:
    primary = evidence[0]
    contract = primary.contract
    quoted_spreads = [
        item.spread / item.midpoint
        for item in evidence
        if item.spread is not None and item.midpoint is not None and item.midpoint > 0
    ]
    spread_percentage = _exact(max(quoted_spreads, default=Decimal("1")))
    blockers: list[str] = []
    if spread_percentage > maximum_spread:
        blockers.append("SPREAD_TOO_WIDE")
    blockers.extend(value for item in evidence for value in item.exclusions)
    dte = (date.fromisoformat(contract.expiration) - as_of_date).days
    if dte <= 0:
        blockers.append("EXPIRATION_NOT_FUTURE")
    greek_values = (primary.delta, primary.gamma, primary.theta, primary.vega)
    known_fields = sum(
        value is not None
        for value in (
            primary.bid,
            primary.ask,
            primary.volume,
            primary.open_interest,
            primary.implied_volatility,
            *greek_values,
        )
    )
    completeness = _exact(Decimal(known_fields) / Decimal(9))
    uncertainty = _exact(
        min(
            Decimal("1"),
            Decimal("1") - completeness + min(Decimal("0.5"), spread_percentage),
        )
    )
    midpoint = primary.midpoint or primary.ask or Decimal()
    extrinsic = max(Decimal(), midpoint * contract.multiplier - intrinsic)
    moneyness = _exact(
        chain.underlying_price / contract.strike - 1
        if contract.option_type is OptionType.CALL
        else contract.strike / chain.underlying_price - 1
    )
    liquidity = _exact(max(Decimal(), 1 - min(Decimal("1"), spread_percentage)))
    payload = (
        OPTIONS_INTELLIGENCE_FEATURES_V1,
        chain.canonical_hash,
        strategy,
        legs,
        _exact(premium),
        _exact(maximum_gain) if maximum_gain is not None else None,
        maximum_gain_classification,
        _exact(break_even),
        _exact(intrinsic),
        _exact(extrinsic),
        dte,
        spread_percentage,
        greek_values,
        liquidity,
        completeness,
        uncertainty,
        tuple(sorted(set(blockers))),
    )
    return DefinedRiskOptionCandidate(
        deterministic_id("defined_risk_option_candidate", payload),
        0,
        chain.underlying_canonical_security_id,
        strategy,
        legs,
        _exact(premium),
        _exact(premium),
        _exact(maximum_gain) if maximum_gain is not None else None,
        maximum_gain_classification,
        _exact(break_even),
        moneyness,
        dte,
        spread_percentage,
        _exact(intrinsic),
        _exact(extrinsic),
        primary.delta,
        primary.gamma,
        primary.theta,
        primary.vega,
        liquidity,
        completeness,
        uncertainty,
        tuple(sorted(set(blockers))),
        content_digest(payload),
    )


@dataclass(frozen=True, slots=True)
class StockOptionExpressionComparison:
    comparison_id: str
    underlying_canonical_security_id: str
    stock_capital_required: Decimal
    option_candidate_id: str | None
    option_capital_required: Decimal | None
    maximum_loss: Decimal
    break_even: Decimal
    time_decay_state: str
    volatility_exposure_state: str
    liquidity_state: str
    evidence_maturity: str
    uncertainty: Decimal
    selection: str
    reason: str
    canonical_hash: str


def compare_stock_and_option_expression(
    *,
    underlying_canonical_security_id: str,
    stock_price: DecimalInput,
    expected_holding_sessions: int,
    candidate: DefinedRiskOptionCandidate | None,
) -> StockOptionExpressionComparison:
    price = parse_decimal(stock_price, allow_zero=False)
    stock_capital = price * 100
    if candidate is None or candidate.blockers:
        selection = "STOCK" if expected_holding_sessions > 0 else "ABSTAIN"
        reason = "NO_ELIGIBLE_DEFINED_RISK_OPTION"
        option_capital = None
        maximum_loss = stock_capital
        break_even = price
        time_decay = "NONE"
        volatility = "INDIRECT"
        liquidity = "UNDERLYING_ONLY"
        uncertainty = Decimal("0.5")
        candidate_id = None
    else:
        candidate_id = candidate.candidate_id
        option_capital = candidate.maximum_loss
        maximum_loss = candidate.maximum_loss
        break_even = candidate.break_even
        time_decay = "THETA_EVIDENCE" if candidate.theta is not None else "THETA_UNAVAILABLE"
        volatility = "VEGA_EVIDENCE" if candidate.vega is not None else "VEGA_UNAVAILABLE"
        liquidity = (
            "OPTION_LIQUIDITY_ACCEPTABLE"
            if candidate.liquidity_score >= Decimal("0.75")
            else "OPTION_LIQUIDITY_WEAK"
        )
        uncertainty = candidate.uncertainty
        if expected_holding_sessions > candidate.days_to_expiration:
            selection = "STOCK"
            reason = "HOLDING_HORIZON_EXCEEDS_OPTION_LIFE"
        elif candidate.uncertainty > Decimal("0.5"):
            selection = "STOCK"
            reason = "OPTION_EVIDENCE_UNCERTAIN"
        else:
            selection = "DEFINED_RISK_OPTION"
            reason = "BOUNDED_LOSS_WITH_MATCHED_HORIZON"
    payload = (
        STOCK_OPTION_EXPRESSION_COMPARISON_V1,
        underlying_canonical_security_id,
        stock_capital,
        expected_holding_sessions,
        candidate_id,
        option_capital,
        maximum_loss,
        break_even,
        selection,
        reason,
    )
    return StockOptionExpressionComparison(
        deterministic_id("stock_option_comparison", payload),
        underlying_canonical_security_id,
        stock_capital,
        candidate_id,
        option_capital,
        maximum_loss,
        break_even,
        time_decay,
        volatility,
        liquidity,
        "CURRENT_CHAIN_RESEARCH_EVIDENCE",
        uncertainty,
        selection,
        reason,
        content_digest(payload),
    )


@dataclass(frozen=True, slots=True)
class DefinedRiskOptionsShadowProposal:
    proposal_id: str
    discovery_run_id: str
    chain_snapshot_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    calculation_policy_ids: tuple[str, ...]
    hypothetical_contracts_each: int
    maximum_losses: tuple[tuple[str, Decimal], ...]
    expirations: tuple[tuple[str, str], ...]
    outcome_resolution_policy: str
    authority: str
    warnings: tuple[str, ...]
    canonical_hash: str


def create_options_shadow_proposal(
    *,
    discovery_run_id: str,
    chains: tuple[OptionChainSnapshot, ...],
    candidates: tuple[DefinedRiskOptionCandidate, ...],
    calculation_policy_ids: tuple[str, ...],
) -> DefinedRiskOptionsShadowProposal:
    known_contracts = {
        item.contract.canonical_contract_id for chain in chains for item in chain.contracts
    }
    if any(
        leg.canonical_contract_id not in known_contracts
        for candidate in candidates
        for leg in candidate.legs
    ):
        raise OptionsIntelligenceError("OPTION_PROPOSAL_CONTRACT_LINEAGE_MISSING")
    expirations = tuple(
        (candidate.candidate_id, candidate.legs[0].expiration) for candidate in candidates
    )
    losses = tuple((candidate.candidate_id, candidate.maximum_loss) for candidate in candidates)
    payload = (
        DEFINED_RISK_OPTIONS_SHADOW_PROPOSAL_V1,
        discovery_run_id,
        tuple(chain.chain_id for chain in chains),
        tuple(candidate.candidate_id for candidate in candidates),
        tuple(sorted(calculation_policy_ids)),
        1,
        losses,
        expirations,
        "EXPIRATION_PAYOFF_FROM_RETAINED_UNDERLYING_CLOSE_V1",
        "OBSERVATION_ONLY",
        OPTIONS_WARNINGS,
    )
    return DefinedRiskOptionsShadowProposal(
        deterministic_id("defined_risk_options_shadow", payload),
        discovery_run_id,
        tuple(chain.chain_id for chain in chains),
        tuple(candidate.candidate_id for candidate in candidates),
        tuple(sorted(calculation_policy_ids)),
        1,
        losses,
        expirations,
        "EXPIRATION_PAYOFF_FROM_RETAINED_UNDERLYING_CLOSE_V1",
        "OBSERVATION_ONLY",
        OPTIONS_WARNINGS,
        content_digest(payload),
    )


def _exact(value: Decimal) -> Decimal:
    return parse_decimal(value.quantize(Decimal("0.00000001")), allow_negative=True)


OPTIONABLE_UNDERLYING_DECISION_V1 = "OPTIONABLE_UNDERLYING_DECISION_V1"


@dataclass(frozen=True, slots=True)
class OptionableUnderlyingDecision:
    """Whether contract-reference evidence shows an underlying is optionable.

    This is a reference-only decision derived from retained contract identity
    records (strike/expiration/type).  It carries no quote, Greek, or implied-
    volatility evidence and grants no order, position, or PAPER/LIVE authority.
    """

    canonical_security_id: str
    optionable: bool
    contract_count: int
    distinct_expirations: int
    source_provider: str
    reasons: tuple[str, ...]
    reference_receipt_ids: tuple[str, ...]
    canonical_hash: str


def derive_optionable_underlyings(
    *,
    universe_security_ids: tuple[str, ...],
    contracts: tuple[OptionContract, ...],
    source_provider: str,
    as_of: str,
) -> tuple[OptionableUnderlyingDecision, ...]:
    """Group retained contract-reference evidence by underlying identity.

    ``universe_security_ids`` bounds the decision to a known, already-resolved
    security universe; an underlying with zero retained contracts is a genuine
    ``NOT_OPTIONABLE`` finding for that source, not a missing-evidence gap.
    """
    ids = tuple(sorted(set(universe_security_ids)))
    if not ids:
        raise OptionsIntelligenceError("OPTIONABLE_UNDERLYING_UNIVERSE_REQUIRED")
    if not source_provider:
        raise OptionsIntelligenceError("OPTIONABLE_UNDERLYING_SOURCE_REQUIRED")
    by_underlying: dict[str, list[OptionContract]] = {}
    for contract in contracts:
        by_underlying.setdefault(contract.underlying_canonical_security_id, []).append(contract)
    decisions: list[OptionableUnderlyingDecision] = []
    for security_id in ids:
        members = tuple(
            sorted(by_underlying.get(security_id, ()), key=lambda item: item.canonical_contract_id)
        )
        optionable = bool(members)
        reasons = () if optionable else ("NO_CONTRACTS_IN_CAPTURED_REFERENCE",)
        receipts = tuple(
            sorted({receipt for item in members for receipt in item.reference_receipt_ids})
        )
        payload = (
            OPTIONABLE_UNDERLYING_DECISION_V1,
            security_id,
            optionable,
            len(members),
            len({item.expiration for item in members}),
            source_provider,
            reasons,
            receipts,
            as_of,
        )
        decisions.append(
            OptionableUnderlyingDecision(
                security_id,
                optionable,
                len(members),
                len({item.expiration for item in members}),
                source_provider,
                reasons,
                receipts,
                content_digest(payload),
            )
        )
    return tuple(decisions)


# --- Options-directory underlying identity linkage ------------------------
#
# The official Nasdaq options-directory source identifies each option series
# by a provider-shaped "Underlying Symbol" string, not by our own resolved
# canonical security identity.  Everything below resolves that raw text to
# exactly one already-reconciled security identity, or explicitly refuses to
# guess: it never joins solely because normalized text happens to match when
# more than one identity remains possible.

OPTION_UNDERLYING_LINK_REJECTION_V1 = "OPTION_UNDERLYING_LINK_REJECTION_V1"
_UNDERLYING_PUNCTUATION = re.compile(r"[.\-/ ]")


class UnderlyingLinkOutcome(StrEnum):
    LINKED = "LINKED"
    UNLINKED = "UNLINKED"
    AMBIGUOUS = "AMBIGUOUS"


def normalize_option_underlying_symbol(value: str) -> str:
    """Collapse known class-share punctuation variants for fallback matching.

    Exact-symbol matching against the reconciled universe is always tried
    first; this normalization exists only to catch conventions such as
    ``BRK.A`` vs ``BRK/A`` vs ``BRK A``, and is applied only when it resolves
    to exactly one reconciled identity -- never when it would collapse
    multiple distinct identities together.
    """
    return _UNDERLYING_PUNCTUATION.sub("", value.strip().upper())


@dataclass(frozen=True, slots=True)
class OptionUnderlyingLinkResult:
    raw_underlying_symbol: str
    outcome: UnderlyingLinkOutcome
    canonical_security_id: str | None
    candidate_security_ids: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OptionUnderlyingLinkIndex:
    """A reviewable index from provider-shaped underlying symbol text to an
    already-reconciled canonical security identity, built once per capture
    from the exact resolved universe -- never from provider text alone."""

    exact: Mapping[str, str]
    normalized: Mapping[str, tuple[str, ...]]

    @classmethod
    def build(cls, universe_symbols: Mapping[str, str]) -> OptionUnderlyingLinkIndex:
        exact = {
            symbol.strip().upper(): canonical_id
            for symbol, canonical_id in universe_symbols.items()
        }
        grouped: dict[str, set[str]] = {}
        for symbol, canonical_id in exact.items():
            grouped.setdefault(normalize_option_underlying_symbol(symbol), set()).add(canonical_id)
        normalized = {key: tuple(sorted(value)) for key, value in grouped.items()}
        return cls(exact, normalized)

    def resolve(self, raw_symbol: str) -> OptionUnderlyingLinkResult:
        candidate = raw_symbol.strip().upper()
        direct = self.exact.get(candidate)
        if direct is not None:
            return OptionUnderlyingLinkResult(
                candidate, UnderlyingLinkOutcome.LINKED, direct, (direct,), ("EXACT_SYMBOL_MATCH",)
            )
        matches = self.normalized.get(normalize_option_underlying_symbol(candidate), ())
        if len(matches) == 1:
            return OptionUnderlyingLinkResult(
                candidate,
                UnderlyingLinkOutcome.LINKED,
                matches[0],
                matches,
                ("NORMALIZED_PUNCTUATION_MATCH",),
            )
        if len(matches) > 1:
            return OptionUnderlyingLinkResult(
                candidate,
                UnderlyingLinkOutcome.AMBIGUOUS,
                None,
                matches,
                ("MULTIPLE_NORMALIZED_IDENTITY_MATCHES",),
            )
        return OptionUnderlyingLinkResult(
            candidate,
            UnderlyingLinkOutcome.UNLINKED,
            None,
            (),
            ("NO_MATCHING_RECONCILED_IDENTITY",),
        )


@dataclass(frozen=True, slots=True)
class OptionUnderlyingLinkRejection:
    """One durably retainable rejection: a raw options-directory underlying
    symbol that could not be linked to exactly one reconciled security
    identity.  Retained once per distinct raw symbol (aggregating every row
    that named it), not once per option-series row, so the evidence ledger
    stays bounded by distinct-symbol count rather than total series count.
    """

    canonical_rejection_id: str
    raw_underlying_symbol: str
    outcome: UnderlyingLinkOutcome
    row_count: int
    candidate_security_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    source_payload_hash: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        raw_underlying_symbol: str,
        outcome: UnderlyingLinkOutcome | str,
        row_count: int,
        candidate_security_ids: tuple[str, ...],
        reasons: tuple[str, ...],
        source_payload_hash: str,
    ) -> OptionUnderlyingLinkRejection:
        outcome_value = UnderlyingLinkOutcome(outcome)
        if (
            not raw_underlying_symbol
            or row_count < 1
            or outcome_value is UnderlyingLinkOutcome.LINKED
            or not reasons
            or len(source_payload_hash) != 64
        ):
            raise OptionsIntelligenceError("OPTION_UNDERLYING_REJECTION_INVALID")
        payload = (
            OPTION_UNDERLYING_LINK_REJECTION_V1,
            raw_underlying_symbol,
            outcome_value,
            row_count,
            tuple(sorted(candidate_security_ids)),
            tuple(sorted(reasons)),
            source_payload_hash,
        )
        return cls(
            deterministic_id("option_underlying_rejection", payload),
            raw_underlying_symbol,
            outcome_value,
            row_count,
            tuple(sorted(candidate_security_ids)),
            tuple(sorted(reasons)),
            source_payload_hash,
            content_digest(payload),
        )
