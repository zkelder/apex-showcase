"""Defined-Risk Options Structure Economics and Research Integration v1.

Research-intelligence evidence only. This module never creates an Order,
Strategy, Shared Opportunity, PAPER order, or LIVE authority; every
persisted fact this module produces carries ``authority = "NONE"``.

It reuses -- and never re-derives -- the existing option-contract identity
model (``domain.options_intelligence.OptionContract``/``OptionType``) and
the existing genuine quote-chain evidence
(``domain.options_quote_intelligence.OptionQuoteObservation``/
``OptionsChainSnapshot``/``ApexImpliedVolatilityEstimate``/
``ApexGreeksEstimate``/``ExpectedMoveEstimate``). It is deliberately
separate from ``domain.options_intelligence.evaluate_defined_risk_options``
and its ``SHADOW`` proposal machinery -- an older, provider-neutral,
debit-only shadow-proposal system this module does not touch or extend.

Structure vocabulary is deliberately small: exactly four two-leg, same-
expiration, defined-risk vertical spreads (long call vertical, long put
vertical, short call credit vertical, short put credit vertical). No naked
short option, straddle/strangle, ratio spread, butterfly, calendar, or
condor is modeled.

Scope boundaries carried from the milestone brief, restated here because
every dataclass below is designed around them:

* research view -> eligible contracts -> candidate structure -> conservative
  economics -> liquidity/cost/risk evaluation -> retained research
  structure. Nothing in this module submits an options order, creates a
  PAPER order, creates a Shared Opportunity, or grants execution authority;
* descriptive pricing (bid/ask/midpoint) and executable pricing (side-aware
  conservative entry) are kept strictly separate -- midpoint never feeds a
  structure's debit/credit;
* option delta is never labeled a probability of profit, and no expected
  value is claimed from payoff geometry alone: every structure carries
  ``EXPECTED_VALUE_UNAVAILABLE`` unless a rigorous, explicit probability
  model is added in a later milestone;
* every payoff is a European-style terminal value at expiration only --
  early exercise and assignment are not modeled;
* candidate generation is bounded and deterministic, never an unbounded N^2
  scan of a dense chain.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from enum import StrEnum

from apex.domain.options_intelligence import OptionType
from apex.domain.options_quote_intelligence import (
    STALENESS_THRESHOLD_SECONDS,
    OptionQuoteObservation,
    OptionsChainSnapshot,
    QuoteObservationStatus,
)
from apex.domain.values import DecimalInput, content_digest, deterministic_id, parse_decimal

# --------------------------------------------------------------------------
# Content-hash namespaces (versioned, one per persisted/immutable fact)
# --------------------------------------------------------------------------

OPTIONS_FEE_POLICY_V1 = "OPTIONS_FEE_POLICY_V1"
OPTIONS_SLIPPAGE_POLICY_V1 = "OPTIONS_SLIPPAGE_POLICY_V1"
STRUCTURE_PRICING_POLICY_V1 = "STRUCTURE_PRICING_POLICY_V1"
OPTIONS_STRUCTURE_RESEARCH_POLICY_V1 = "OPTIONS_STRUCTURE_RESEARCH_POLICY_V1"
OPTION_STRUCTURE_CANDIDATE_V1 = "OPTION_STRUCTURE_CANDIDATE_V1"
STRUCTURE_RESEARCH_SNAPSHOT_V1 = "STRUCTURE_RESEARCH_SNAPSHOT_V1"
STRUCTURE_RESEARCH_FEATURE_V1 = "STRUCTURE_RESEARCH_FEATURE_V1"
STRUCTURE_EXPRESSION_COMPARISON_V1 = "STRUCTURE_EXPRESSION_COMPARISON_V1"

FEE_EVIDENCE_INCOMPLETE = "FEE_EVIDENCE_INCOMPLETE"
FEE_EVIDENCE_STATUSES = ("EVIDENCED", FEE_EVIDENCE_INCOMPLETE)
EXPECTED_VALUE_UNAVAILABLE = "EXPECTED_VALUE_UNAVAILABLE"

# --------------------------------------------------------------------------
# Genuine Alpaca options fee/commission documentation evidence, captured
# during "Fresh Options Evidence Operations and Structure Research Baseline
# v1" (research milestone; not a provider account/API read -- Alpaca's
# `/v2/account` endpoint used elsewhere for the separately governed EXTERNAL
# PAPER tier carries no commission/fee-schedule field). Read directly from
# Alpaca Securities LLC's own published brokerage fee schedule, revised
# 2026-04-01: https://files.alpaca.markets/disclosures/BrokFeeSched.pdf
# (support-page summary confirming zero commission cross-checked at
# https://alpaca.markets/support/what-are-the-commission-fees-per-option-contract).
# Retained as a *second*, explicitly evidenced fee-policy identity --
# ``OptionsFeePolicy.create()``'s own conservative $0.65/contract/leg
# ``FEE_EVIDENCE_INCOMPLETE`` default is never rewritten or removed.
#
# Alpaca itself charges no per-contract options commission ("we do not
# charge a commission for trades"; no options-specific commission line item
# exists in the schedule beyond the pass-through regulatory fees below).
# Three regulatory/exchange fees are passed through on every options
# transaction:
#   * FINRA Trading Activity Fee (TAF):  $0.00329/contract, sells only
#   * Options Regulatory Fee (ORF):      $0.02295/contract, buys and sells
#   * Options Clearing Corporation (OCC) fee: $0.02500/contract, buys and
#     sells, no cap
# (A FINRA CAT fee also applies to options but the schedule states it is
# currently $0.00/executed-equivalent-share; an SEC Section 31 fee applies
# on sells but this document's own per-$1,000,000 rate did not extract
# reliably and is deliberately not modeled here rather than guessed.)
#
# ``build_structure_economics`` applies ``exchange_regulatory_fee_per_
# contract`` identically to every leg regardless of buy/sell side (see
# ``OptionsFeePolicy`` below); TAF is genuinely sell-only, so blending all
# three into one flat per-contract-per-side rate slightly *overstates* true
# cost on a leg's opening buy side -- a documented, conservative
# simplification, never a precision claim, and never an under-estimate.
ALPACA_OPTIONS_REGULATORY_FEE_EVIDENCE_2026_04_01 = (
    "ALPACA_OPTIONS_REGULATORY_FEE_EVIDENCE_2026_04_01"
)
ALPACA_OPTIONS_COMMISSION_PER_CONTRACT_2026_04_01 = Decimal("0")
ALPACA_OPTIONS_TAF_PER_CONTRACT_2026_04_01 = Decimal("0.00329")
ALPACA_OPTIONS_ORF_PER_CONTRACT_2026_04_01 = Decimal("0.02295")
ALPACA_OPTIONS_OCC_FEE_PER_CONTRACT_2026_04_01 = Decimal("0.025")
# = TAF + ORF + OCC above, precomputed as a literal so the sum is auditable
# without depending on this module's later-defined ``_exact`` rounding
# helper; verify with a calculator, not by trusting this comment.
ALPACA_OPTIONS_BLENDED_REGULATORY_FEE_PER_CONTRACT_2026_04_01 = Decimal("0.05124")
TERMINAL_PAYOFF_MODEL_NOTE = (
    "EUROPEAN_STYLE_TERMINAL_PAYOFF_AT_EXPIRATION_ONLY_V1: no early exercise "
    "or assignment is modeled."
)

STRUCTURE_ECONOMICS_WARNINGS = (
    "DEFINED-RISK OPTIONS STRUCTURE ECONOMICS IS RESEARCH ONLY",
    "NO OPTION ORDER, PAPER ORDER, OR SHARED OPPORTUNITY IS EVER CREATED HERE",
    "CONSERVATIVE ENTRY PRICING IS NOT A GUARANTEED FILL",
    "FEES MAY BE A CONSERVATIVE RESEARCH ASSUMPTION, NOT A CONFIRMED PROVIDER RATE",
    "OPTION DELTA IS NOT PROBABILITY OF PROFIT",
    "NO EXPECTED VALUE IS CLAIMED WITHOUT A JUSTIFIED PROBABILITY MODEL",
    "NO EARLY EXERCISE OR ASSIGNMENT IS MODELED",
    "HIGH IV IS NOT AUTOMATICALLY GOOD FOR CREDIT SPREADS; LOW IV IS NOT "
    "AUTOMATICALLY GOOD FOR DEBIT SPREADS",
    "NO OPTION ORDER AUTHORITY IS ENABLED",
    "LIVE TRADING IS NOT ENABLED",
)

STRUCTURE_RESEARCH_LIMITATIONS = (
    "INDICATIVE_FEED_NOT_OPRA_CONSOLIDATED",
    "QUOTE_EVIDENCE_MAY_BE_STALE_SEE_SOURCE_SNAPSHOT_FRESHNESS",
    "FIXED_RISK_FREE_RATE_ASSUMPTION",
    "ZERO_DIVIDEND_YIELD_ASSUMPTION",
    "OPEN_INTEREST_UNAVAILABLE",
    "NO_HISTORICAL_OPTIONS_PRICING",
    "IV_RANK_MAY_BE_INSUFFICIENT_HISTORY",
    "NO_EARLY_EXERCISE_MODEL: American-style exercise is not modeled; every "
    "payoff is a European-style terminal value at expiration only.",
    "NO_ASSIGNMENT_MODEL",
    "NO_FILL_PROBABILITY_MODEL: conservative side-aware entry prices are not a guaranteed fill.",
    "NO_EXPECTED_VALUE_DISTRIBUTION: payoff geometry and IV context are "
    "retained without a justified probability model -- see "
    "EXPECTED_VALUE_UNAVAILABLE.",
    "NO_PROBABILITY_OF_PROFIT_FIELD: option delta is not represented as a "
    "probability anywhere in this module.",
    "FEE_EVIDENCE_MAY_BE_INCOMPLETE: see the bound fee policy's "
    "fee_evidence_status -- a FEE_EVIDENCE_INCOMPLETE policy uses a "
    "clearly-labeled conservative research assumption, not a confirmed "
    "provider rate.",
    "SINGLE_QUANTITY_V1: position sizing beyond a small fixed contract "
    "quantity per leg is not modeled this milestone.",
    "NO_IV_REGIME_SCORING: high/low implied volatility is never scored as "
    "automatically favorable for a credit or debit structure; that "
    "relationship is left to later research.",
)


class OptionsStructureError(ValueError):
    pass


class StructureType(StrEnum):
    CALL_DEBIT_VERTICAL = "CALL_DEBIT_VERTICAL"
    PUT_DEBIT_VERTICAL = "PUT_DEBIT_VERTICAL"
    CALL_CREDIT_VERTICAL = "CALL_CREDIT_VERTICAL"
    PUT_CREDIT_VERTICAL = "PUT_CREDIT_VERTICAL"


ALL_STRUCTURE_TYPES: tuple[StructureType, ...] = (
    StructureType.CALL_DEBIT_VERTICAL,
    StructureType.PUT_DEBIT_VERTICAL,
    StructureType.CALL_CREDIT_VERTICAL,
    StructureType.PUT_CREDIT_VERTICAL,
)


class StructureFamily(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class LegRole(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class StructureQualityLabel(StrEnum):
    HIGH_QUALITY = "HIGH_QUALITY"
    USABLE = "USABLE"
    MARGINAL = "MARGINAL"
    UNUSABLE = "UNUSABLE"


class ScenarioLabel(StrEnum):
    DOWN_EXPECTED_MOVE = "DOWN_EXPECTED_MOVE"
    UNCHANGED = "UNCHANGED"
    UP_EXPECTED_MOVE = "UP_EXPECTED_MOVE"
    AT_LOWER_STRIKE = "AT_LOWER_STRIKE"
    AT_UPPER_STRIKE = "AT_UPPER_STRIKE"
    FAR_BELOW_BOTH_STRIKES = "FAR_BELOW_BOTH_STRIKES"
    FAR_ABOVE_BOTH_STRIKES = "FAR_ABOVE_BOTH_STRIKES"


# Declared before any structure's economics is reviewed -- fixed lookup
# tables, never tuned in response to a given underlying's results.
_REQUIRED_OPTION_TYPE: dict[StructureType, OptionType] = {
    StructureType.CALL_DEBIT_VERTICAL: OptionType.CALL,
    StructureType.PUT_DEBIT_VERTICAL: OptionType.PUT,
    StructureType.CALL_CREDIT_VERTICAL: OptionType.CALL,
    StructureType.PUT_CREDIT_VERTICAL: OptionType.PUT,
}
_STRUCTURE_FAMILY: dict[StructureType, StructureFamily] = {
    StructureType.CALL_DEBIT_VERTICAL: StructureFamily.DEBIT,
    StructureType.PUT_DEBIT_VERTICAL: StructureFamily.DEBIT,
    StructureType.CALL_CREDIT_VERTICAL: StructureFamily.CREDIT,
    StructureType.PUT_CREDIT_VERTICAL: StructureFamily.CREDIT,
}
# Which leg role (LONG or SHORT) belongs at the *lower* of the pair's two
# strikes, for each structure type -- the single source of truth both
# eligibility's strike-order check and candidate generation's leg
# assignment read from, so the two can never silently disagree.
_REQUIRED_LEG_FOR_LOWER_STRIKE: dict[StructureType, LegRole] = {
    StructureType.CALL_DEBIT_VERTICAL: LegRole.LONG,
    StructureType.PUT_DEBIT_VERTICAL: LegRole.SHORT,
    StructureType.CALL_CREDIT_VERTICAL: LegRole.SHORT,
    StructureType.PUT_CREDIT_VERTICAL: LegRole.LONG,
}
_STRUCTURE_TYPES_FOR_OPTION_TYPE: dict[OptionType, tuple[StructureType, ...]] = {
    OptionType.CALL: (StructureType.CALL_DEBIT_VERTICAL, StructureType.CALL_CREDIT_VERTICAL),
    OptionType.PUT: (StructureType.PUT_DEBIT_VERTICAL, StructureType.PUT_CREDIT_VERTICAL),
}
_CRITICAL_QUALITY_BLOCKERS = (
    QuoteObservationStatus.INVALID,
    QuoteObservationStatus.CROSSED,
    QuoteObservationStatus.UNAVAILABLE,
)


def _exact(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return value.quantize(Decimal("0.00000001"))


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None:
        raise OptionsStructureError(f"{label}_MUST_BE_TIMEZONE_AWARE")
    return value


def structure_type_family(structure_type: StructureType) -> StructureFamily:
    return _STRUCTURE_FAMILY[structure_type]


def structure_type_option_type(structure_type: StructureType) -> OptionType:
    return _REQUIRED_OPTION_TYPE[structure_type]


def _strike_order_valid(
    structure_type: StructureType, long_strike: Decimal, short_strike: Decimal
) -> bool:
    lower_role = _REQUIRED_LEG_FOR_LOWER_STRIKE[structure_type]
    if long_strike == short_strike:
        return False
    lower_is_long = long_strike < short_strike
    return lower_is_long == (lower_role is LegRole.LONG)


# --------------------------------------------------------------------------
# Section 6: versioned options transaction-cost (fee) policy
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionsFeePolicy:
    """A declared, versioned per-contract fee assumption. ``fee_evidence_
    status`` stays honest about whether ``per_contract_fee`` is a genuinely
    evidenced provider rate or a clearly-labeled conservative research
    assumption -- zero fees are never used silently."""

    policy_id: str
    version: str
    per_contract_fee: Decimal
    exchange_regulatory_fee_per_contract: Decimal | None
    fee_evidence_status: str
    include_projected_closing_cost: bool
    methodology: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        version: str = "OPTIONS_FEE_POLICY_V1",
        per_contract_fee: DecimalInput = "0.65",
        exchange_regulatory_fee_per_contract: DecimalInput | None = None,
        fee_evidence_status: str = FEE_EVIDENCE_INCOMPLETE,
        include_projected_closing_cost: bool = True,
        methodology: str = (
            "No genuinely evidenced Alpaca options commission/regulatory-fee "
            "schedule was captured this session; per_contract_fee is a "
            "clearly-labeled conservative flat research assumption, not a "
            "confirmed provider rate. Applied per contract, per leg, per "
            "opening side; the projected closing side is a separate, "
            "informational round-trip figure, never baked into opening "
            "capital required or maximum loss."
        ),
    ) -> OptionsFeePolicy:
        if fee_evidence_status not in FEE_EVIDENCE_STATUSES:
            raise OptionsStructureError("FEE_POLICY_EVIDENCE_STATUS_INVALID")
        # Zero is only ever accepted for a policy explicitly declaring itself
        # ``EVIDENCED`` (e.g. Alpaca's own genuinely documented zero-
        # commission options policy, see
        # ``ALPACA_OPTIONS_REGULATORY_FEE_EVIDENCE_2026_04_01`` above) --
        # never for the default ``FEE_EVIDENCE_INCOMPLETE`` path, so an
        # unmodeled fee can never silently read as free.
        fee = parse_decimal(per_contract_fee, allow_zero=fee_evidence_status == "EVIDENCED")
        if not (Decimal("0") <= fee <= Decimal("25")):
            raise OptionsStructureError("FEE_POLICY_PER_CONTRACT_FEE_OUT_OF_RANGE")
        regulatory = (
            parse_decimal(exchange_regulatory_fee_per_contract)
            if exchange_regulatory_fee_per_contract is not None
            else None
        )
        if regulatory is not None and regulatory > Decimal("5"):
            raise OptionsStructureError("FEE_POLICY_REGULATORY_FEE_OUT_OF_RANGE")
        payload = (
            OPTIONS_FEE_POLICY_V1,
            version,
            fee,
            regulatory,
            fee_evidence_status,
            include_projected_closing_cost,
            methodology,
        )
        return cls(
            policy_id=deterministic_id("options_fee_policy", payload),
            version=version,
            per_contract_fee=fee,
            exchange_regulatory_fee_per_contract=regulatory,
            fee_evidence_status=fee_evidence_status,
            include_projected_closing_cost=include_projected_closing_cost,
            methodology=methodology,
            canonical_hash=content_digest(payload),
        )

    @classmethod
    def create_alpaca_regulatory_evidenced_2026_04_01(cls) -> OptionsFeePolicy:
        """A second, explicitly evidenced fee-policy identity -- mints a
        distinct ``policy_id`` from the default ``create()`` policy rather
        than rewriting it (milestone section 10). Every number here is read
        directly from Alpaca Securities LLC's own published brokerage fee
        schedule (revised 2026-04-01); see
        ``ALPACA_OPTIONS_REGULATORY_FEE_EVIDENCE_2026_04_01`` above for the
        exact source and the individual TAF/ORF/OCC figures this blends."""

        return cls.create(
            version=ALPACA_OPTIONS_REGULATORY_FEE_EVIDENCE_2026_04_01,
            per_contract_fee=ALPACA_OPTIONS_COMMISSION_PER_CONTRACT_2026_04_01,
            exchange_regulatory_fee_per_contract=(
                ALPACA_OPTIONS_BLENDED_REGULATORY_FEE_PER_CONTRACT_2026_04_01
            ),
            fee_evidence_status="EVIDENCED",
            include_projected_closing_cost=True,
            methodology=(
                "Alpaca Securities LLC charges no per-contract options "
                "commission (source: "
                "https://files.alpaca.markets/disclosures/BrokFeeSched.pdf, "
                "revised 2026-04-01, cross-checked against "
                "https://alpaca.markets/support/what-are-the-commission-fees-"
                "per-option-contract). exchange_regulatory_fee_per_contract "
                "blends three pass-through regulatory fees from the identical "
                "schedule: FINRA Trading Activity Fee $0.00329/contract "
                "(sells only), Options Regulatory Fee $0.02295/contract "
                "(buys and sells), and OCC clearing fee $0.025/contract, no "
                "cap (buys and sells) = $0.05124/contract flat. TAF is "
                "genuinely sell-only; blending it into a flat per-side rate "
                "slightly overstates true cost on a leg's opening buy side -- "
                "a documented, conservative simplification, never an "
                "under-estimate. FINRA CAT fee is $0.00/executed-equivalent-"
                "share per the identical schedule and is not separately "
                "added. The SEC Section 31 fee (sells only) is not modeled: "
                "this document's per-$1,000,000 rate did not extract "
                "reliably and is deliberately omitted rather than guessed."
            ),
        )


# --------------------------------------------------------------------------
# Section 7: conservative structure slippage policy
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionsSlippagePolicy:
    """Conservative modeled slippage, derived from quote evidence (leg
    spread) -- never a claim that a fill inside the spread is guaranteed."""

    policy_id: str
    version: str
    methodology: str
    adverse_fraction_of_spread: Decimal
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        version: str = "OPTIONS_SLIPPAGE_POLICY_V1",
        methodology: str = "SIDE_PRICE_PLUS_ADVERSE_FRACTION_V1",
        adverse_fraction_of_spread: DecimalInput = "0.25",
    ) -> OptionsSlippagePolicy:
        if methodology not in ("SIDE_PRICE_ONLY", "SIDE_PRICE_PLUS_ADVERSE_FRACTION_V1"):
            raise OptionsStructureError("SLIPPAGE_POLICY_METHODOLOGY_UNSUPPORTED")
        fraction = parse_decimal(adverse_fraction_of_spread)
        if methodology == "SIDE_PRICE_ONLY" and fraction != 0:
            raise OptionsStructureError("SLIPPAGE_POLICY_SIDE_PRICE_ONLY_REQUIRES_ZERO_FRACTION")
        if fraction > 1:
            raise OptionsStructureError("SLIPPAGE_POLICY_ADVERSE_FRACTION_OUT_OF_RANGE")
        payload = (OPTIONS_SLIPPAGE_POLICY_V1, version, methodology, fraction)
        return cls(
            policy_id=deterministic_id("options_slippage_policy", payload),
            version=version,
            methodology=methodology,
            adverse_fraction_of_spread=fraction,
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 5: descriptive-versus-executable pricing policy
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructurePricingPolicy:
    """The declared side-aware conservative entry-price convention. Midpoint
    is descriptive only and is never used to compute an executable
    debit/credit for a structure."""

    policy_id: str
    version: str
    methodology: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        version: str = "CONSERVATIVE_SIDE_AWARE_ENTRY_V1",
        methodology: str = (
            "Debit vertical: buy the long leg at its ask (or governed "
            "buy-price assumption), sell the short leg at its bid (or "
            "governed sell-price assumption). Credit vertical: sell the "
            "short leg at its bid, buy the protective long leg at its ask. "
            "Midpoint is descriptive only and is never used to compute an "
            "executable debit/credit."
        ),
    ) -> StructurePricingPolicy:
        payload = (STRUCTURE_PRICING_POLICY_V1, version, methodology)
        return cls(
            policy_id=deterministic_id("structure_pricing_policy", payload),
            version=version,
            methodology=methodology,
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 18: one versioned, bounded structure-research policy -- declared
# before any evaluation result exists, never tuned to produce attractive
# structures.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionsStructureResearchPolicy:
    policy_id: str
    version: str
    supported_structure_types: tuple[StructureType, ...]
    dte_minimum: int
    dte_maximum: int
    moneyness_band: Decimal
    max_adjacent_strike_span: int
    minimum_quote_quality: tuple[QuoteObservationStatus, ...]
    maximum_relative_spread: Decimal
    minimum_displayed_size: Decimal
    maximum_fee_burden: Decimal
    maximum_capital_required: Decimal
    require_greeks: bool
    allow_stale_quotes_for_research: bool
    maximum_candidate_pairs_per_underlying: int
    maximum_structures_per_underlying: int
    maximum_total_structures: int
    pricing_policy_id: str
    fee_policy_id: str
    slippage_policy_id: str
    authority: str
    created_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        pricing_policy: StructurePricingPolicy,
        fee_policy: OptionsFeePolicy,
        slippage_policy: OptionsSlippagePolicy,
        created_at: datetime,
        version: str = "OPTIONS_STRUCTURE_RESEARCH_POLICY_V1",
        supported_structure_types: tuple[StructureType, ...] = ALL_STRUCTURE_TYPES,
        dte_minimum: int = 1,
        dte_maximum: int = 60,
        moneyness_band: DecimalInput = "0.15",
        max_adjacent_strike_span: int = 3,
        minimum_quote_quality: tuple[QuoteObservationStatus, ...] = (
            QuoteObservationStatus.OBSERVED,
            QuoteObservationStatus.ZERO_LIQUIDITY,
        ),
        maximum_relative_spread: DecimalInput = "0.35",
        minimum_displayed_size: DecimalInput = "1",
        maximum_fee_burden: DecimalInput = "0.20",
        maximum_capital_required: DecimalInput = "5000",
        require_greeks: bool = False,
        allow_stale_quotes_for_research: bool = False,
        maximum_candidate_pairs_per_underlying: int = 300,
        maximum_structures_per_underlying: int = 20,
        maximum_total_structures: int = 200,
    ) -> OptionsStructureResearchPolicy:
        _aware(created_at, "STRUCTURE_POLICY_CREATED_AT")
        types = tuple(sorted(set(supported_structure_types)))
        if not types or set(types) - set(ALL_STRUCTURE_TYPES):
            raise OptionsStructureError("STRUCTURE_POLICY_SUPPORTED_TYPES_INVALID")
        if not (0 <= dte_minimum < dte_maximum <= 730):
            raise OptionsStructureError("STRUCTURE_POLICY_DTE_RANGE_INVALID")
        band = parse_decimal(moneyness_band, allow_zero=False)
        if not (Decimal("0.01") <= band <= Decimal("1")):
            raise OptionsStructureError("STRUCTURE_POLICY_MONEYNESS_BAND_OUT_OF_RANGE")
        if not (1 <= max_adjacent_strike_span <= 10):
            raise OptionsStructureError("STRUCTURE_POLICY_STRIKE_SPAN_OUT_OF_RANGE")
        quality = tuple(sorted(set(minimum_quote_quality)))
        if not quality:
            raise OptionsStructureError("STRUCTURE_POLICY_MINIMUM_QUOTE_QUALITY_REQUIRED")
        max_spread = parse_decimal(maximum_relative_spread, allow_zero=False)
        if not (Decimal("0.01") <= max_spread <= Decimal("2")):
            raise OptionsStructureError("STRUCTURE_POLICY_MAX_RELATIVE_SPREAD_OUT_OF_RANGE")
        min_size = parse_decimal(minimum_displayed_size)
        max_fee_burden = parse_decimal(maximum_fee_burden, allow_zero=False)
        if not (Decimal("0.01") <= max_fee_burden <= Decimal("1")):
            raise OptionsStructureError("STRUCTURE_POLICY_MAX_FEE_BURDEN_OUT_OF_RANGE")
        max_capital = parse_decimal(maximum_capital_required, allow_zero=False)
        if not (Decimal("100") <= max_capital <= Decimal("1000000")):
            raise OptionsStructureError("STRUCTURE_POLICY_MAX_CAPITAL_OUT_OF_RANGE")
        if not (1 <= maximum_candidate_pairs_per_underlying <= 2000):
            raise OptionsStructureError("STRUCTURE_POLICY_MAX_CANDIDATE_PAIRS_OUT_OF_RANGE")
        if not (1 <= maximum_structures_per_underlying <= 500):
            raise OptionsStructureError(
                "STRUCTURE_POLICY_MAX_STRUCTURES_PER_UNDERLYING_OUT_OF_RANGE"
            )
        if not (maximum_structures_per_underlying <= maximum_total_structures <= 5000):
            raise OptionsStructureError("STRUCTURE_POLICY_MAX_TOTAL_STRUCTURES_OUT_OF_RANGE")
        identity_payload = (
            OPTIONS_STRUCTURE_RESEARCH_POLICY_V1,
            version,
            types,
            dte_minimum,
            dte_maximum,
            band,
            max_adjacent_strike_span,
            quality,
            max_spread,
            min_size,
            max_fee_burden,
            max_capital,
            require_greeks,
            allow_stale_quotes_for_research,
            maximum_candidate_pairs_per_underlying,
            maximum_structures_per_underlying,
            maximum_total_structures,
            pricing_policy.policy_id,
            fee_policy.policy_id,
            slippage_policy.policy_id,
        )
        payload = (*identity_payload, created_at)
        return cls(
            policy_id=deterministic_id("options_structure_research_policy", identity_payload),
            version=version,
            supported_structure_types=types,
            dte_minimum=dte_minimum,
            dte_maximum=dte_maximum,
            moneyness_band=band,
            max_adjacent_strike_span=max_adjacent_strike_span,
            minimum_quote_quality=quality,
            maximum_relative_spread=max_spread,
            minimum_displayed_size=min_size,
            maximum_fee_burden=max_fee_burden,
            maximum_capital_required=max_capital,
            require_greeks=require_greeks,
            allow_stale_quotes_for_research=allow_stale_quotes_for_research,
            maximum_candidate_pairs_per_underlying=maximum_candidate_pairs_per_underlying,
            maximum_structures_per_underlying=maximum_structures_per_underlying,
            maximum_total_structures=maximum_total_structures,
            pricing_policy_id=pricing_policy.policy_id,
            fee_policy_id=fee_policy.policy_id,
            slippage_policy_id=slippage_policy.policy_id,
            authority="NONE",
            created_at=created_at,
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 2/5: one structure leg -- frozen evidence bundle self-contained
# enough to audit a structure without re-fetching its source observation.
# ``entry_price`` is the declared side-aware conservative price (ask for a
# LONG leg, bid for a SHORT leg) -- descriptive-only midpoint never appears
# here as an executable price.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructureLeg:
    role: LegRole
    canonical_contract_id: str
    option_type: OptionType
    strike: Decimal
    expiration: str
    multiplier: Decimal
    observation_id: str
    quote_status: QuoteObservationStatus
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    midpoint: Decimal | None
    staleness_seconds: int
    apex_implied_volatility: Decimal | None
    apex_delta: Decimal | None
    apex_gamma: Decimal | None
    apex_theta: Decimal | None
    apex_vega: Decimal | None
    entry_price: Decimal | None


def _build_leg(
    *, role: LegRole, observation: OptionQuoteObservation, lookups: _SnapshotGreekLookups
) -> StructureLeg:
    contract = observation.contract
    contract_id = contract.canonical_contract_id
    entry_price = observation.ask if role is LegRole.LONG else observation.bid
    return StructureLeg(
        role=role,
        canonical_contract_id=contract_id,
        option_type=contract.option_type,
        strike=contract.strike,
        expiration=contract.expiration,
        multiplier=contract.multiplier,
        observation_id=observation.observation_id,
        quote_status=observation.status,
        bid=observation.bid,
        ask=observation.ask,
        bid_size=observation.bid_size,
        ask_size=observation.ask_size,
        midpoint=observation.midpoint,
        staleness_seconds=observation.staleness_seconds,
        apex_implied_volatility=lookups.iv.get(contract_id),
        apex_delta=lookups.delta.get(contract_id),
        apex_gamma=lookups.gamma.get(contract_id),
        apex_theta=lookups.theta.get(contract_id),
        apex_vega=lookups.vega.get(contract_id),
        entry_price=entry_price,
    )


@dataclass(frozen=True, slots=True)
class _SnapshotGreekLookups:
    """Apex-model IV/Greeks keyed by contract id -- the *local* model
    sensitivities already computed and retained on the source snapshot,
    never re-solved here."""

    iv: dict[str, Decimal]
    delta: dict[str, Decimal]
    gamma: dict[str, Decimal]
    theta: dict[str, Decimal]
    vega: dict[str, Decimal]


def _snapshot_lookups(snapshot: OptionsChainSnapshot) -> _SnapshotGreekLookups:
    iv = {
        e.canonical_contract_id: e.implied_volatility
        for e in snapshot.iv_estimates
        if e.implied_volatility is not None
    }
    delta = {
        e.canonical_contract_id: e.delta for e in snapshot.greeks_estimates if e.delta is not None
    }
    gamma = {
        e.canonical_contract_id: e.gamma for e in snapshot.greeks_estimates if e.gamma is not None
    }
    theta = {
        e.canonical_contract_id: e.theta for e in snapshot.greeks_estimates if e.theta is not None
    }
    vega = {
        e.canonical_contract_id: e.vega for e in snapshot.greeks_estimates if e.vega is not None
    }
    return _SnapshotGreekLookups(iv, delta, gamma, theta, vega)


# --------------------------------------------------------------------------
# Section 3: eligibility rules for contract pairing. A rejected pairing is
# reported with explicit reason codes and never silently repaired (e.g. a
# reversed pair of legs is rejected, not swapped back into order).
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructureEligibilityResult:
    accepted: bool
    reasons: tuple[str, ...]


def evaluate_vertical_eligibility(
    *,
    structure_type: StructureType,
    long_observation: OptionQuoteObservation,
    short_observation: OptionQuoteObservation,
    policy: OptionsStructureResearchPolicy,
    as_of: date,
    chain_snapshot_captured_at: datetime,
) -> StructureEligibilityResult:
    _aware(chain_snapshot_captured_at, "ELIGIBILITY_CHAIN_SNAPSHOT_CAPTURED_AT")
    long_contract = long_observation.contract
    short_contract = short_observation.contract
    expected_type = _REQUIRED_OPTION_TYPE[structure_type]
    reasons: list[str] = []

    if (
        long_contract.underlying_canonical_security_id
        != short_contract.underlying_canonical_security_id
    ):
        reasons.append("UNDERLYING_MISMATCH")
    if long_contract.expiration != short_contract.expiration:
        reasons.append("EXPIRATION_MISMATCH")
    if (
        long_contract.option_type is not expected_type
        or short_contract.option_type is not expected_type
    ):
        reasons.append("OPTION_TYPE_MISMATCH")
    if long_contract.strike == short_contract.strike:
        reasons.append("IDENTICAL_STRIKES")
    elif not _strike_order_valid(structure_type, long_contract.strike, short_contract.strike):
        reasons.append("INVALID_STRIKE_ORDER")
    if long_contract.multiplier != short_contract.multiplier:
        reasons.append("MULTIPLIER_MISMATCH")
    elif long_contract.multiplier <= 0:
        reasons.append("INVALID_MULTIPLIER")
    if long_observation.midpoint is None:
        reasons.append("LONG_LEG_QUOTE_NOT_TWO_SIDED")
    if short_observation.midpoint is None:
        reasons.append("SHORT_LEG_QUOTE_NOT_TWO_SIDED")
    if long_observation.status in _CRITICAL_QUALITY_BLOCKERS:
        reasons.append("LONG_LEG_CRITICAL_DATA_QUALITY_BLOCKER")
    if short_observation.status in _CRITICAL_QUALITY_BLOCKERS:
        reasons.append("SHORT_LEG_CRITICAL_DATA_QUALITY_BLOCKER")
    # Staleness is a hard eligibility rejection by default -- never silently
    # treated as fresh. ``allow_stale_quotes_for_research`` is an explicit,
    # separately-versioned policy opt-in (never the default) that lets
    # candidate generation proceed against known-stale evidence purely for
    # research replay (e.g. proving structure construction/economics are
    # genuine against a retained stale chain when no fresher capture is
    # available). Even then, ``build_structure_quality`` independently
    # flags every such structure ``UNUSABLE`` with an explicit
    # ``QUOTE_STALE`` reason -- staleness is never hidden, only whether it
    # blocks construction outright is policy-controlled.
    if not policy.allow_stale_quotes_for_research:
        if long_observation.staleness_seconds > STALENESS_THRESHOLD_SECONDS:
            reasons.append("LONG_LEG_QUOTE_STALE")
        if short_observation.staleness_seconds > STALENESS_THRESHOLD_SECONDS:
            reasons.append("SHORT_LEG_QUOTE_STALE")
    if long_observation.bid_size is None or long_observation.ask_size is None:
        reasons.append("LONG_LEG_SIZE_MISSING")
    if short_observation.bid_size is None or short_observation.ask_size is None:
        reasons.append("SHORT_LEG_SIZE_MISSING")
    # "compatible chain snapshot": both legs' evidence must have been
    # ingested by the identical capture pass, never one fresh quote paired
    # with a different capture's stale leftover.
    if (
        long_observation.ingestion_timestamp != chain_snapshot_captured_at
        or short_observation.ingestion_timestamp != chain_snapshot_captured_at
    ):
        reasons.append("INCOMPATIBLE_CHAIN_SNAPSHOT_EVIDENCE")
    try:
        dte = (date.fromisoformat(long_contract.expiration) - as_of).days
    except ValueError:
        dte = None
    if dte is None or not (policy.dte_minimum <= dte <= policy.dte_maximum):
        reasons.append("DTE_OUT_OF_POLICY_RANGE")

    return StructureEligibilityResult(not reasons, tuple(reasons))


# --------------------------------------------------------------------------
# Sections 6-9: fee/slippage assessment and unified debit/credit economics.
# A structure that prices as economically invalid (nonpositive gross
# debit/credit, nonpositive max profit/loss after costs) is rejected --
# never clamped into looking sensible.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructureFeeAssessment:
    policy_id: str
    per_contract_fee: Decimal
    exchange_regulatory_fee_per_contract: Decimal | None
    fee_evidence_status: str
    legs: int
    contracts_per_leg: int
    opening_cost: Decimal
    projected_closing_cost: Decimal | None
    total_round_trip_cost: Decimal


@dataclass(frozen=True, slots=True)
class StructureSlippageAssessment:
    policy_id: str
    methodology: str
    adverse_fraction_of_spread: Decimal
    long_leg_slippage_per_share: Decimal
    short_leg_slippage_per_share: Decimal
    total_slippage: Decimal


@dataclass(frozen=True, slots=True)
class StructureEconomics:
    family: StructureFamily
    quantity: int
    multiplier: Decimal
    width: Decimal
    long_entry_price: Decimal
    short_entry_price: Decimal
    gross_amount: Decimal
    fees: StructureFeeAssessment
    slippage: StructureSlippageAssessment
    net_amount: Decimal
    capital_required: Decimal
    maximum_loss: Decimal
    maximum_profit: Decimal
    breakeven: Decimal
    reward_risk_ratio: Decimal


@dataclass(frozen=True, slots=True)
class StructureEconomicsResult:
    economics: StructureEconomics | None
    rejection_reasons: tuple[str, ...]


def _leg_spread(leg: StructureLeg) -> Decimal:
    if leg.bid is None or leg.ask is None:
        return Decimal(0)
    return max(Decimal(0), leg.ask - leg.bid)


def build_structure_economics(
    *,
    structure_type: StructureType,
    long_leg: StructureLeg,
    short_leg: StructureLeg,
    quantity: int,
    fee_policy: OptionsFeePolicy,
    slippage_policy: OptionsSlippagePolicy,
) -> StructureEconomicsResult:
    if not (1 <= quantity <= 10):
        raise OptionsStructureError("STRUCTURE_QUANTITY_OUT_OF_RANGE")
    if long_leg.entry_price is None or short_leg.entry_price is None:
        return StructureEconomicsResult(None, ("ENTRY_PRICE_UNAVAILABLE",))
    if long_leg.multiplier != short_leg.multiplier or long_leg.multiplier <= 0:
        return StructureEconomicsResult(None, ("INVALID_MULTIPLIER",))

    family = _STRUCTURE_FAMILY[structure_type]
    multiplier = long_leg.multiplier
    width = abs(long_leg.strike - short_leg.strike)

    long_slippage_per_share = _exact(
        _leg_spread(long_leg) * slippage_policy.adverse_fraction_of_spread
    )
    short_slippage_per_share = _exact(
        _leg_spread(short_leg) * slippage_policy.adverse_fraction_of_spread
    )
    total_slippage = _exact(
        (long_slippage_per_share + short_slippage_per_share) * multiplier * quantity
    )
    slippage_assessment = StructureSlippageAssessment(
        policy_id=slippage_policy.policy_id,
        methodology=slippage_policy.methodology,
        adverse_fraction_of_spread=slippage_policy.adverse_fraction_of_spread,
        long_leg_slippage_per_share=long_slippage_per_share,
        short_leg_slippage_per_share=short_slippage_per_share,
        total_slippage=total_slippage,
    )

    legs = 2
    regulatory = fee_policy.exchange_regulatory_fee_per_contract or Decimal(0)
    opening_cost = _exact((fee_policy.per_contract_fee + regulatory) * legs * quantity)
    projected_closing_cost = opening_cost if fee_policy.include_projected_closing_cost else None
    total_round_trip_cost = _exact(opening_cost + (projected_closing_cost or Decimal(0)))
    fees_assessment = StructureFeeAssessment(
        policy_id=fee_policy.policy_id,
        per_contract_fee=fee_policy.per_contract_fee,
        exchange_regulatory_fee_per_contract=fee_policy.exchange_regulatory_fee_per_contract,
        fee_evidence_status=fee_policy.fee_evidence_status,
        legs=legs,
        contracts_per_leg=quantity,
        opening_cost=opening_cost,
        projected_closing_cost=projected_closing_cost,
        total_round_trip_cost=total_round_trip_cost,
    )

    if family is StructureFamily.DEBIT:
        gross_per_share = long_leg.entry_price - short_leg.entry_price
        if gross_per_share <= 0:
            return StructureEconomicsResult(None, ("INVALID_GROSS_DEBIT_NONPOSITIVE",))
        gross_amount = _exact(gross_per_share * multiplier * quantity)
        total_capital_required = _exact(gross_amount + opening_cost + total_slippage)
        maximum_loss = total_capital_required
        maximum_profit = _exact(width * multiplier * quantity - total_capital_required)
        if maximum_profit <= 0:
            return StructureEconomicsResult(None, ("MAXIMUM_PROFIT_NONPOSITIVE",))
        per_share_cost = total_capital_required / (multiplier * quantity)
        breakeven = (
            _exact(long_leg.strike + per_share_cost)
            if structure_type is StructureType.CALL_DEBIT_VERTICAL
            else _exact(long_leg.strike - per_share_cost)
        )
        net_amount = total_capital_required
        capital_required = total_capital_required
    else:
        gross_per_share = short_leg.entry_price - long_leg.entry_price
        if gross_per_share <= 0:
            return StructureEconomicsResult(None, ("INVALID_GROSS_CREDIT_NONPOSITIVE",))
        gross_amount = _exact(gross_per_share * multiplier * quantity)
        net_credit = _exact(gross_amount - opening_cost - total_slippage)
        if net_credit <= 0:
            return StructureEconomicsResult(None, ("INVALID_NET_CREDIT_NONPOSITIVE",))
        maximum_profit = net_credit
        maximum_loss = _exact(width * multiplier * quantity - net_credit)
        if maximum_loss <= 0:
            return StructureEconomicsResult(None, ("MAXIMUM_LOSS_NONPOSITIVE",))
        per_share_credit = net_credit / (multiplier * quantity)
        breakeven = (
            _exact(short_leg.strike + per_share_credit)
            if structure_type is StructureType.CALL_CREDIT_VERTICAL
            else _exact(short_leg.strike - per_share_credit)
        )
        net_amount = net_credit
        capital_required = maximum_loss

    reward_risk_ratio = _exact(maximum_profit / maximum_loss)
    economics = StructureEconomics(
        family=family,
        quantity=quantity,
        multiplier=multiplier,
        width=width,
        long_entry_price=long_leg.entry_price,
        short_entry_price=short_leg.entry_price,
        gross_amount=gross_amount,
        fees=fees_assessment,
        slippage=slippage_assessment,
        net_amount=net_amount,
        capital_required=capital_required,
        maximum_loss=maximum_loss,
        maximum_profit=maximum_profit,
        breakeven=breakeven,
        reward_risk_ratio=reward_risk_ratio,
    )
    return StructureEconomicsResult(economics, ())


# --------------------------------------------------------------------------
# Section 10: deterministic expiration payoff model. Every payoff is a
# European-style terminal value at expiration only -- no early exercise.
# ``initial_cash_flow`` is the signed upfront cash flow (negative
# ``-capital_required`` for a debit, positive ``+net_amount`` for a credit)
# so one uniform formula prices every one of the four structure types:
# ``net_profit_loss = gross_structure_payoff + initial_cash_flow``.
# --------------------------------------------------------------------------


def option_leg_terminal_value(
    *, option_type: OptionType, strike: Decimal, underlying_price_at_expiration: Decimal
) -> Decimal:
    if option_type is OptionType.CALL:
        return max(Decimal(0), underlying_price_at_expiration - strike)
    return max(Decimal(0), strike - underlying_price_at_expiration)


@dataclass(frozen=True, slots=True)
class StructurePayoffPoint:
    underlying_price_at_expiration: Decimal
    long_leg_payoff: Decimal
    short_leg_payoff: Decimal
    gross_structure_payoff: Decimal
    net_profit_loss: Decimal


def structure_payoff_at(
    *,
    structure_type: StructureType,
    long_strike: Decimal,
    short_strike: Decimal,
    multiplier: Decimal,
    quantity: int,
    initial_cash_flow: Decimal,
    underlying_price_at_expiration: Decimal,
) -> StructurePayoffPoint:
    if underlying_price_at_expiration < 0:
        raise OptionsStructureError("PAYOFF_UNDERLYING_PRICE_MUST_BE_NON_NEGATIVE")
    option_type = _REQUIRED_OPTION_TYPE[structure_type]
    long_leg_payoff = _exact(
        option_leg_terminal_value(
            option_type=option_type,
            strike=long_strike,
            underlying_price_at_expiration=underlying_price_at_expiration,
        )
        * multiplier
        * quantity
    )
    short_leg_payoff = _exact(
        -option_leg_terminal_value(
            option_type=option_type,
            strike=short_strike,
            underlying_price_at_expiration=underlying_price_at_expiration,
        )
        * multiplier
        * quantity
    )
    gross = _exact(long_leg_payoff + short_leg_payoff)
    net = _exact(gross + initial_cash_flow)
    return StructurePayoffPoint(
        underlying_price_at_expiration=underlying_price_at_expiration,
        long_leg_payoff=long_leg_payoff,
        short_leg_payoff=short_leg_payoff,
        gross_structure_payoff=gross,
        net_profit_loss=net,
    )


def structure_initial_cash_flow(economics: StructureEconomics) -> Decimal:
    """The signed upfront cash flow ``structure_payoff_at`` needs: negative
    for a debit (capital paid out), positive for a credit (premium
    received)."""

    if economics.family is StructureFamily.DEBIT:
        return -economics.capital_required
    return economics.net_amount


# --------------------------------------------------------------------------
# Section 16: bounded, deterministic scenario analysis. Exactly seven fixed
# points -- never a Monte Carlo simulation.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructureScenario:
    label: ScenarioLabel
    status: str
    underlying_price_at_expiration: Decimal | None
    payoff: StructurePayoffPoint | None


def build_structure_scenarios(
    *,
    structure_type: StructureType,
    long_strike: Decimal,
    short_strike: Decimal,
    multiplier: Decimal,
    quantity: int,
    initial_cash_flow: Decimal,
    underlying_reference_price: Decimal,
    expected_move_percent: Decimal | None,
) -> tuple[StructureScenario, ...]:
    lower_strike = min(long_strike, short_strike)
    upper_strike = max(long_strike, short_strike)
    width = upper_strike - lower_strike
    far_below = max(Decimal("0.01"), lower_strike - 2 * width)
    far_above = upper_strike + 2 * width

    def _at(label: ScenarioLabel, price: Decimal) -> StructureScenario:
        payoff = structure_payoff_at(
            structure_type=structure_type,
            long_strike=long_strike,
            short_strike=short_strike,
            multiplier=multiplier,
            quantity=quantity,
            initial_cash_flow=initial_cash_flow,
            underlying_price_at_expiration=price,
        )
        return StructureScenario(label, "COMPUTED", price, payoff)

    def _move(label: ScenarioLabel, direction: int) -> StructureScenario:
        if expected_move_percent is None:
            return StructureScenario(label, "EXPECTED_MOVE_UNAVAILABLE", None, None)
        factor = Decimal(1) + direction * expected_move_percent
        price = max(Decimal("0.01"), _exact(underlying_reference_price * factor))
        return _at(label, price)

    return (
        _move(ScenarioLabel.DOWN_EXPECTED_MOVE, -1),
        _at(ScenarioLabel.UNCHANGED, underlying_reference_price),
        _move(ScenarioLabel.UP_EXPECTED_MOVE, 1),
        _at(ScenarioLabel.AT_LOWER_STRIKE, lower_strike),
        _at(ScenarioLabel.AT_UPPER_STRIKE, upper_strike),
        _at(ScenarioLabel.FAR_BELOW_BOTH_STRIKES, far_below),
        _at(ScenarioLabel.FAR_ABOVE_BOTH_STRIKES, far_above),
    )


# --------------------------------------------------------------------------
# Section 15: structure-level net Greeks. Local model sensitivities only,
# never a forecast -- ``None`` (never a fabricated value) whenever a leg's
# own Apex Greek is unavailable.
# --------------------------------------------------------------------------


_GREEKS_METHODOLOGY = (
    "Per-contract net = long-leg Apex Greek minus short-leg Apex Greek "
    "(long counted positive, short counted negative), in the identical "
    "per-share/per-one-lot convention as each leg's own Greek (see "
    "domain.options_quote_intelligence.ApexGreeksEstimate). Scale by "
    "multiplier and quantity for dollar sensitivity. Local Black-Scholes-"
    "Merton model sensitivities at the source snapshot only -- not a "
    "forecast of how the structure's value will actually move."
)


@dataclass(frozen=True, slots=True)
class StructureGreeks:
    available: bool
    net_delta: Decimal | None
    net_gamma: Decimal | None
    net_theta: Decimal | None
    net_vega: Decimal | None
    methodology: str
    reasons: tuple[str, ...]


def _net_greek(long_value: Decimal | None, short_value: Decimal | None) -> Decimal | None:
    if long_value is None or short_value is None:
        return None
    return _exact(long_value - short_value)


def build_structure_greeks(*, long_leg: StructureLeg, short_leg: StructureLeg) -> StructureGreeks:
    net_delta = _net_greek(long_leg.apex_delta, short_leg.apex_delta)
    net_gamma = _net_greek(long_leg.apex_gamma, short_leg.apex_gamma)
    net_theta = _net_greek(long_leg.apex_theta, short_leg.apex_theta)
    net_vega = _net_greek(long_leg.apex_vega, short_leg.apex_vega)
    reasons: list[str] = []
    if long_leg.apex_delta is None:
        reasons.append("LONG_LEG_GREEKS_UNAVAILABLE")
    if short_leg.apex_delta is None:
        reasons.append("SHORT_LEG_GREEKS_UNAVAILABLE")
    available = None not in (net_delta, net_gamma, net_theta, net_vega)
    return StructureGreeks(
        available=available,
        net_delta=net_delta,
        net_gamma=net_gamma,
        net_theta=net_theta,
        net_vega=net_vega,
        methodology=_GREEKS_METHODOLOGY,
        reasons=tuple(reasons),
    )


# --------------------------------------------------------------------------
# Section 14: bind each evaluation to already-existing options intelligence
# -- research context only. Never automatically scores high IV as good for
# credit spreads or low IV as good for debit spreads.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructureVolatilityContext:
    atm_iv_nearest_expiration: Decimal | None
    long_leg_apex_iv: Decimal | None
    short_leg_apex_iv: Decimal | None
    skew_state: str | None
    skew_target_delta: Decimal | None
    term_structure_shape: str
    term_structure_near_far_spread: Decimal | None
    expected_move_percent_straddle: Decimal | None
    expected_move_percent_iv_derived: Decimal | None
    iv_vs_realized_state: str | None
    iv_vs_realized_premium_ratio: Decimal | None
    liquidity_state: str
    data_quality_status: str

    @classmethod
    def from_snapshot(
        cls,
        *,
        snapshot: OptionsChainSnapshot,
        long_leg: StructureLeg,
        short_leg: StructureLeg,
        expiration: str,
    ) -> StructureVolatilityContext:
        atm_point = next(
            (p for p in snapshot.volatility_surface.atm_points if p.expiration == expiration), None
        )
        skew = next(
            (
                s
                for s in snapshot.volatility_surface.skew_by_expiration
                if s.expiration == expiration
            ),
            None,
        )
        straddle = next(
            (
                m
                for m in snapshot.expected_moves
                if m.expiration == expiration and m.method == "ATM_STRADDLE_V1"
            ),
            None,
        )
        iv_derived = next(
            (
                m
                for m in snapshot.expected_moves
                if m.expiration == expiration and m.method == "IV_DERIVED_V1"
            ),
            None,
        )
        return cls(
            atm_iv_nearest_expiration=atm_point.atm_iv if atm_point is not None else None,
            long_leg_apex_iv=long_leg.apex_implied_volatility,
            short_leg_apex_iv=short_leg.apex_implied_volatility,
            skew_state=skew.shape.value if skew is not None else None,
            skew_target_delta=skew.skew_target_delta if skew is not None else None,
            term_structure_shape=snapshot.volatility_surface.term_structure.shape.value,
            term_structure_near_far_spread=(
                snapshot.volatility_surface.term_structure.near_far_spread
            ),
            expected_move_percent_straddle=(
                straddle.percent_move
                if straddle is not None and straddle.status == "COMPUTED"
                else None
            ),
            expected_move_percent_iv_derived=(
                iv_derived.percent_move
                if iv_derived is not None and iv_derived.status == "COMPUTED"
                else None
            ),
            iv_vs_realized_state=(
                snapshot.iv_vs_realized.premium_state
                if snapshot.iv_vs_realized is not None
                else None
            ),
            iv_vs_realized_premium_ratio=(
                snapshot.iv_vs_realized.premium_ratio
                if snapshot.iv_vs_realized is not None
                else None
            ),
            liquidity_state=snapshot.liquidity_profile.coarse_liquidity_state,
            data_quality_status=snapshot.data_quality_status.value,
        )


# --------------------------------------------------------------------------
# Section 17: structured quality evidence. Component values are preferred
# over the opaque scalar; ``label`` is a convenience classification from
# fixed, declared-before-results thresholds -- never tuned to make a given
# structure look attractive.
# --------------------------------------------------------------------------

_QUALITY_TIGHT_RELATIVE_SPREAD = Decimal("0.10")
_QUALITY_ACCEPTABLE_RELATIVE_SPREAD = Decimal("0.25")
_QUALITY_TIGHT_FEE_BURDEN = Decimal("0.05")
_QUALITY_ACCEPTABLE_FEE_BURDEN = Decimal("0.20")
_QUALITY_HIGH_MINIMUM_DISPLAYED_SIZE = Decimal("5")


@dataclass(frozen=True, slots=True)
class StructureQualityProfile:
    label: StructureQualityLabel
    quote_freshness_seconds_max: int
    within_freshness_threshold: bool
    both_legs_two_sided: bool
    long_leg_relative_spread: Decimal | None
    short_leg_relative_spread: Decimal | None
    minimum_displayed_size: Decimal | None
    width: Decimal
    gross_amount_relative_to_width: Decimal | None
    fee_burden: Decimal | None
    slippage_burden: Decimal | None
    iv_inputs_available: bool
    reasons: tuple[str, ...]


def _relative_spread_for_leg(leg: StructureLeg) -> Decimal | None:
    if leg.bid is None or leg.ask is None or leg.midpoint is None or leg.midpoint <= 0:
        return None
    return _exact((leg.ask - leg.bid) / leg.midpoint)


def build_structure_quality(
    *,
    long_leg: StructureLeg,
    short_leg: StructureLeg,
    width: Decimal,
    economics: StructureEconomics,
) -> StructureQualityProfile:
    freshness_max = max(long_leg.staleness_seconds, short_leg.staleness_seconds)
    within_freshness = freshness_max <= STALENESS_THRESHOLD_SECONDS
    two_sided = long_leg.midpoint is not None and short_leg.midpoint is not None
    long_relative = _relative_spread_for_leg(long_leg)
    short_relative = _relative_spread_for_leg(short_leg)
    sizes = [
        v
        for v in (long_leg.bid_size, long_leg.ask_size, short_leg.bid_size, short_leg.ask_size)
        if v is not None
    ]
    minimum_size = min(sizes) if sizes else None
    width_notional = width * economics.multiplier * economics.quantity
    gross_relative = _exact(economics.gross_amount / width_notional) if width_notional > 0 else None
    fee_burden = (
        _exact(economics.fees.opening_cost / economics.gross_amount)
        if economics.gross_amount > 0
        else None
    )
    slippage_burden = (
        _exact(economics.slippage.total_slippage / economics.gross_amount)
        if economics.gross_amount > 0
        else None
    )
    iv_available = (
        long_leg.apex_implied_volatility is not None
        and short_leg.apex_implied_volatility is not None
    )

    worst_relative = max(
        (v for v in (long_relative, short_relative) if v is not None), default=None
    )
    reasons: list[str] = []
    if not within_freshness:
        reasons.append("QUOTE_STALE")
    if not two_sided:
        reasons.append("NOT_TWO_SIDED")
    if minimum_size is None or minimum_size == 0:
        reasons.append("ZERO_OR_UNKNOWN_DISPLAYED_SIZE")
    if worst_relative is not None and worst_relative > _QUALITY_ACCEPTABLE_RELATIVE_SPREAD:
        reasons.append("WIDE_RELATIVE_SPREAD")
    if fee_burden is not None and fee_burden > _QUALITY_ACCEPTABLE_FEE_BURDEN:
        reasons.append("HIGH_FEE_BURDEN")

    if not within_freshness or not two_sided or minimum_size in (None, Decimal(0)):
        label = StructureQualityLabel.UNUSABLE
    elif (worst_relative is not None and worst_relative > _QUALITY_ACCEPTABLE_RELATIVE_SPREAD) or (
        fee_burden is not None and fee_burden > _QUALITY_ACCEPTABLE_FEE_BURDEN
    ):
        label = StructureQualityLabel.MARGINAL
    elif (
        worst_relative is not None
        and worst_relative <= _QUALITY_TIGHT_RELATIVE_SPREAD
        and fee_burden is not None
        and fee_burden <= _QUALITY_TIGHT_FEE_BURDEN
        and minimum_size is not None
        and minimum_size >= _QUALITY_HIGH_MINIMUM_DISPLAYED_SIZE
        and iv_available
    ):
        label = StructureQualityLabel.HIGH_QUALITY
    else:
        label = StructureQualityLabel.USABLE

    return StructureQualityProfile(
        label=label,
        quote_freshness_seconds_max=freshness_max,
        within_freshness_threshold=within_freshness,
        both_legs_two_sided=two_sided,
        long_leg_relative_spread=long_relative,
        short_leg_relative_spread=short_relative,
        minimum_displayed_size=minimum_size,
        width=width,
        gross_amount_relative_to_width=gross_relative,
        fee_burden=fee_burden,
        slippage_burden=slippage_burden,
        iv_inputs_available=iv_available,
        reasons=tuple(reasons),
    )


# --------------------------------------------------------------------------
# Section 2: the immutable structure candidate. Identity
# (``structure_id``) is deliberately narrower than full content -- exactly
# the fields the milestone names ("changing either leg; quote evidence;
# pricing policy; cost assumption must create a different evaluation
# identity"): structure type, both legs' contract and observation ids, and
# the three cost-assumption policy ids. Descriptive/derived content
# (quality label, scenarios, ...) is retained but never part of identity,
# mirroring ``OptionsChainSnapshot.snapshot_id``'s identical narrower-than-
# content design one module over.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionStructureCandidate:
    structure_id: str
    structure_type: StructureType
    family: StructureFamily
    underlying_canonical_security_id: str
    underlying_symbol: str
    underlying_reference_price: Decimal
    expiration: str
    dte: int
    long_leg: StructureLeg
    short_leg: StructureLeg
    width: Decimal
    quantity: int
    economics: StructureEconomics
    payoff_at_strikes: tuple[StructurePayoffPoint, ...]
    scenarios: tuple[StructureScenario, ...]
    greeks: StructureGreeks
    volatility_context: StructureVolatilityContext
    quality: StructureQualityProfile
    source_chain_snapshot_id: str
    pricing_policy_id: str
    fee_policy_id: str
    slippage_policy_id: str
    structure_policy_id: str
    terminal_payoff_model_note: str
    expected_value_state: str
    evaluated_at: datetime
    authority: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        structure_type: StructureType,
        snapshot: OptionsChainSnapshot,
        long_leg: StructureLeg,
        short_leg: StructureLeg,
        economics: StructureEconomics,
        structure_policy: OptionsStructureResearchPolicy,
        as_of: date,
        evaluated_at: datetime,
    ) -> OptionStructureCandidate:
        _aware(evaluated_at, "STRUCTURE_EVALUATED_AT")
        if long_leg.expiration != short_leg.expiration:
            raise OptionsStructureError("STRUCTURE_LEG_EXPIRATION_MISMATCH")
        expiration = long_leg.expiration
        dte = (date.fromisoformat(expiration) - as_of).days
        initial_cash_flow = structure_initial_cash_flow(economics)
        lower_strike = min(long_leg.strike, short_leg.strike)
        upper_strike = max(long_leg.strike, short_leg.strike)
        payoff_at_strikes = (
            structure_payoff_at(
                structure_type=structure_type,
                long_strike=long_leg.strike,
                short_strike=short_leg.strike,
                multiplier=economics.multiplier,
                quantity=economics.quantity,
                initial_cash_flow=initial_cash_flow,
                underlying_price_at_expiration=lower_strike,
            ),
            structure_payoff_at(
                structure_type=structure_type,
                long_strike=long_leg.strike,
                short_strike=short_leg.strike,
                multiplier=economics.multiplier,
                quantity=economics.quantity,
                initial_cash_flow=initial_cash_flow,
                underlying_price_at_expiration=upper_strike,
            ),
        )
        volatility_context = StructureVolatilityContext.from_snapshot(
            snapshot=snapshot, long_leg=long_leg, short_leg=short_leg, expiration=expiration
        )
        expected_move = (
            volatility_context.expected_move_percent_straddle
            if volatility_context.expected_move_percent_straddle is not None
            else volatility_context.expected_move_percent_iv_derived
        )
        scenarios = build_structure_scenarios(
            structure_type=structure_type,
            long_strike=long_leg.strike,
            short_strike=short_leg.strike,
            multiplier=economics.multiplier,
            quantity=economics.quantity,
            initial_cash_flow=initial_cash_flow,
            underlying_reference_price=snapshot.underlying_reference_price,
            expected_move_percent=expected_move,
        )
        greeks = build_structure_greeks(long_leg=long_leg, short_leg=short_leg)
        width = abs(long_leg.strike - short_leg.strike)
        quality = build_structure_quality(
            long_leg=long_leg, short_leg=short_leg, width=width, economics=economics
        )
        identity_payload = (
            OPTION_STRUCTURE_CANDIDATE_V1,
            structure_type,
            long_leg.canonical_contract_id,
            short_leg.canonical_contract_id,
            long_leg.observation_id,
            short_leg.observation_id,
            structure_policy.pricing_policy_id,
            structure_policy.fee_policy_id,
            structure_policy.slippage_policy_id,
            economics.quantity,
        )
        structure_id = deterministic_id("options_structure", identity_payload)
        payload = (
            *identity_payload,
            snapshot.snapshot_id,
            snapshot.underlying_reference_price,
            economics,
            payoff_at_strikes,
            scenarios,
            greeks,
            volatility_context,
            quality,
            evaluated_at,
        )
        return cls(
            structure_id=structure_id,
            structure_type=structure_type,
            family=economics.family,
            underlying_canonical_security_id=snapshot.underlying_canonical_security_id,
            underlying_symbol=snapshot.underlying_symbol,
            underlying_reference_price=snapshot.underlying_reference_price,
            expiration=expiration,
            dte=dte,
            long_leg=long_leg,
            short_leg=short_leg,
            width=width,
            quantity=economics.quantity,
            economics=economics,
            payoff_at_strikes=payoff_at_strikes,
            scenarios=scenarios,
            greeks=greeks,
            volatility_context=volatility_context,
            quality=quality,
            source_chain_snapshot_id=snapshot.snapshot_id,
            pricing_policy_id=structure_policy.pricing_policy_id,
            fee_policy_id=structure_policy.fee_policy_id,
            slippage_policy_id=structure_policy.slippage_policy_id,
            structure_policy_id=structure_policy.policy_id,
            terminal_payoff_model_note=TERMINAL_PAYOFF_MODEL_NOTE,
            expected_value_state=EXPECTED_VALUE_UNAVAILABLE,
            evaluated_at=evaluated_at,
            authority="NONE",
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Sections 19-20: bounded, deterministic candidate generation and its
# economics filter. Bounded by expiration/DTE eligibility, an adjacent-or-
# bounded strike window, and a moneyness band -- never every possible N^2
# pair on a dense chain. Every considered pair ends up in exactly one of
# ``evaluated`` or ``rejected``, fully accounted for.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RejectedStructureCandidate:
    structure_type: StructureType
    long_canonical_contract_id: str
    short_canonical_contract_id: str
    stage: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StructureCandidateGenerationReport:
    underlying_canonical_security_id: str
    underlying_symbol: str
    potential_pairs: int
    considered_pairs: int
    rejected_pairs: int
    evaluated_structures: int
    rejection_reason_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class StructureGenerationResult:
    evaluated: tuple[OptionStructureCandidate, ...]
    rejected: tuple[RejectedStructureCandidate, ...]
    report: StructureCandidateGenerationReport


def _within_moneyness_band(underlying_price: Decimal, strike: Decimal, band: Decimal) -> bool:
    return abs(underlying_price / strike - 1) <= band


def _eligible_dte_expirations(
    expirations: Sequence[str], as_of: date, dte_minimum: int, dte_maximum: int
) -> frozenset[str]:
    eligible = set()
    for expiration in expirations:
        try:
            dte = (date.fromisoformat(expiration) - as_of).days
        except ValueError:
            continue
        if dte_minimum <= dte <= dte_maximum:
            eligible.add(expiration)
    return frozenset(eligible)


def _group_by_expiration_and_type(
    observations: Sequence[OptionQuoteObservation],
) -> dict[tuple[str, OptionType], list[OptionQuoteObservation]]:
    groups: dict[tuple[str, OptionType], list[OptionQuoteObservation]] = {}
    for observation in observations:
        key = (observation.contract.expiration, observation.contract.option_type)
        groups.setdefault(key, []).append(observation)
    return groups


def _apply_economics_filter(
    candidate: OptionStructureCandidate, policy: OptionsStructureResearchPolicy
) -> tuple[str, ...]:
    reasons: list[str] = []
    worst_relative_spread = max(
        (
            v
            for v in (
                candidate.quality.long_leg_relative_spread,
                candidate.quality.short_leg_relative_spread,
            )
            if v is not None
        ),
        default=None,
    )
    if worst_relative_spread is not None and worst_relative_spread > policy.maximum_relative_spread:
        reasons.append("RELATIVE_SPREAD_EXCEEDS_POLICY_MAXIMUM")
    if (
        candidate.quality.minimum_displayed_size is None
        or candidate.quality.minimum_displayed_size < policy.minimum_displayed_size
    ):
        reasons.append("DISPLAYED_SIZE_BELOW_POLICY_MINIMUM")
    if (
        candidate.quality.fee_burden is not None
        and candidate.quality.fee_burden > policy.maximum_fee_burden
    ):
        reasons.append("FEE_BURDEN_EXCEEDS_POLICY_MAXIMUM")
    if candidate.economics.capital_required > policy.maximum_capital_required:
        reasons.append("CAPITAL_REQUIRED_EXCEEDS_POLICY_MAXIMUM")
    if (
        candidate.long_leg.quote_status not in policy.minimum_quote_quality
        or candidate.short_leg.quote_status not in policy.minimum_quote_quality
    ):
        reasons.append("QUOTE_QUALITY_BELOW_POLICY_MINIMUM")
    if policy.require_greeks and not candidate.greeks.available:
        reasons.append("GREEKS_REQUIRED_BY_POLICY_BUT_UNAVAILABLE")
    return tuple(reasons)


def generate_structure_candidates(
    *,
    snapshot: OptionsChainSnapshot,
    observations: tuple[OptionQuoteObservation, ...],
    structure_policy: OptionsStructureResearchPolicy,
    fee_policy: OptionsFeePolicy,
    slippage_policy: OptionsSlippagePolicy,
    as_of: date,
    evaluated_at: datetime,
    quantity: int = 1,
) -> StructureGenerationResult:
    _aware(evaluated_at, "GENERATION_EVALUATED_AT")
    if not (1 <= quantity <= 10):
        raise OptionsStructureError("STRUCTURE_QUANTITY_OUT_OF_RANGE")
    lookups = _snapshot_lookups(snapshot)
    underlying_price = snapshot.underlying_reference_price
    expirations = {observation.contract.expiration for observation in observations}
    eligible_expirations = _eligible_dte_expirations(
        tuple(expirations), as_of, structure_policy.dte_minimum, structure_policy.dte_maximum
    )
    groups = _group_by_expiration_and_type(observations)

    potential: list[tuple[StructureType, OptionQuoteObservation, OptionQuoteObservation]] = []
    for (expiration, option_type), members in groups.items():
        if expiration not in eligible_expirations:
            continue
        by_strike = sorted(
            {m.contract.strike: m for m in members}.items(), key=lambda item: item[0]
        )
        count = len(by_strike)
        for i in range(count):
            strike_i, observation_i = by_strike[i]
            if not _within_moneyness_band(
                underlying_price, strike_i, structure_policy.moneyness_band
            ):
                continue
            upper_bound = min(i + 1 + structure_policy.max_adjacent_strike_span, count)
            for j in range(i + 1, upper_bound):
                strike_j, observation_j = by_strike[j]
                if not _within_moneyness_band(
                    underlying_price, strike_j, structure_policy.moneyness_band
                ):
                    continue
                for structure_type in _STRUCTURE_TYPES_FOR_OPTION_TYPE[option_type]:
                    if structure_type not in structure_policy.supported_structure_types:
                        continue
                    if _REQUIRED_LEG_FOR_LOWER_STRIKE[structure_type] is LegRole.LONG:
                        potential.append((structure_type, observation_i, observation_j))
                    else:
                        potential.append((structure_type, observation_j, observation_i))

    potential_pairs = len(potential)
    ordered = sorted(
        potential,
        key=lambda item: (
            item[1].contract.expiration,
            item[1].contract.option_type.value,
            item[0].value,
            item[1].contract.strike,
            item[2].contract.strike,
        ),
    )
    considered = ordered[: structure_policy.maximum_candidate_pairs_per_underlying]
    considered_pairs = len(considered)

    evaluated: list[OptionStructureCandidate] = []
    rejected: list[RejectedStructureCandidate] = []
    reason_counts: dict[str, int] = {}

    def _reject(
        structure_type: StructureType,
        long_observation: OptionQuoteObservation,
        short_observation: OptionQuoteObservation,
        stage: str,
        reasons: tuple[str, ...],
    ) -> None:
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        rejected.append(
            RejectedStructureCandidate(
                structure_type=structure_type,
                long_canonical_contract_id=long_observation.contract.canonical_contract_id,
                short_canonical_contract_id=short_observation.contract.canonical_contract_id,
                stage=stage,
                reasons=reasons,
            )
        )

    for structure_type, long_observation, short_observation in considered:
        eligibility = evaluate_vertical_eligibility(
            structure_type=structure_type,
            long_observation=long_observation,
            short_observation=short_observation,
            policy=structure_policy,
            as_of=as_of,
            chain_snapshot_captured_at=snapshot.captured_at,
        )
        if not eligibility.accepted:
            _reject(
                structure_type,
                long_observation,
                short_observation,
                "ELIGIBILITY",
                eligibility.reasons,
            )
            continue
        long_leg = _build_leg(role=LegRole.LONG, observation=long_observation, lookups=lookups)
        short_leg = _build_leg(role=LegRole.SHORT, observation=short_observation, lookups=lookups)
        economics_result = build_structure_economics(
            structure_type=structure_type,
            long_leg=long_leg,
            short_leg=short_leg,
            quantity=quantity,
            fee_policy=fee_policy,
            slippage_policy=slippage_policy,
        )
        if economics_result.economics is None:
            _reject(
                structure_type,
                long_observation,
                short_observation,
                "ECONOMICS",
                economics_result.rejection_reasons,
            )
            continue
        candidate = OptionStructureCandidate.create(
            structure_type=structure_type,
            snapshot=snapshot,
            long_leg=long_leg,
            short_leg=short_leg,
            economics=economics_result.economics,
            structure_policy=structure_policy,
            as_of=as_of,
            evaluated_at=evaluated_at,
        )
        filter_reasons = _apply_economics_filter(candidate, structure_policy)
        if filter_reasons:
            _reject(
                structure_type, long_observation, short_observation, "ECONOMICS", filter_reasons
            )
            continue
        if len(evaluated) >= structure_policy.maximum_structures_per_underlying:
            _reject(
                structure_type,
                long_observation,
                short_observation,
                "ECONOMICS",
                ("STRUCTURES_PER_UNDERLYING_CAP_REACHED",),
            )
            continue
        evaluated.append(candidate)

    report = StructureCandidateGenerationReport(
        underlying_canonical_security_id=snapshot.underlying_canonical_security_id,
        underlying_symbol=snapshot.underlying_symbol,
        potential_pairs=potential_pairs,
        considered_pairs=considered_pairs,
        rejected_pairs=len(rejected),
        evaluated_structures=len(evaluated),
        rejection_reason_counts=tuple(sorted(reason_counts.items())),
    )
    return StructureGenerationResult(tuple(evaluated), tuple(rejected), report)


# --------------------------------------------------------------------------
# Section 21: one immutable retained structure-research snapshot per
# underlying/capture. Identity excludes ``evaluated_at`` -- like
# ``OptionChainCapturePlan``/``OptionsChainSnapshot`` before it -- so exact
# replay from the identical evidence/policy resolves to the identical row.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructureResearchSnapshot:
    research_snapshot_id: str
    structure_policy_id: str
    source_chain_snapshot_id: str
    underlying_canonical_security_id: str
    underlying_symbol: str
    evaluated_at: datetime
    generation_report: StructureCandidateGenerationReport
    rejected_structures: tuple[RejectedStructureCandidate, ...]
    retained_structure_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    authority: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        structure_policy: OptionsStructureResearchPolicy,
        snapshot: OptionsChainSnapshot,
        result: StructureGenerationResult,
        evaluated_at: datetime,
    ) -> StructureResearchSnapshot:
        _aware(evaluated_at, "STRUCTURE_RESEARCH_SNAPSHOT_EVALUATED_AT")
        retained_ids = tuple(sorted(item.structure_id for item in result.evaluated))
        identity_payload = (
            STRUCTURE_RESEARCH_SNAPSHOT_V1,
            structure_policy.policy_id,
            snapshot.snapshot_id,
            retained_ids,
        )
        research_snapshot_id = deterministic_id(
            "options_structure_research_snapshot", identity_payload
        )
        payload = (
            *identity_payload,
            evaluated_at,
            result.report,
            result.rejected,
            STRUCTURE_RESEARCH_LIMITATIONS,
        )
        return cls(
            research_snapshot_id=research_snapshot_id,
            structure_policy_id=structure_policy.policy_id,
            source_chain_snapshot_id=snapshot.snapshot_id,
            underlying_canonical_security_id=snapshot.underlying_canonical_security_id,
            underlying_symbol=snapshot.underlying_symbol,
            evaluated_at=evaluated_at,
            generation_report=result.report,
            rejected_structures=result.rejected,
            retained_structure_ids=retained_ids,
            limitations=STRUCTURE_RESEARCH_LIMITATIONS,
            authority="NONE",
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 22: cross-market research-seam projection -- feature evidence
# only. Never creates a hypothesis or an Opportunity.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructureResearchFeatures:
    feature_id: str
    structure_id: str
    underlying_canonical_security_id: str
    underlying_symbol: str
    structure_type: StructureType
    as_of: str
    normalized_debit_or_credit: Decimal | None
    width: Decimal
    maximum_profit: Decimal
    maximum_loss: Decimal
    reward_risk_ratio: Decimal
    breakeven_distance_percent: Decimal
    net_delta: Decimal | None
    net_theta: Decimal | None
    net_vega: Decimal | None
    iv_vs_realized_state: str | None
    skew_state: str | None
    term_structure_shape: str
    liquidity_burden: Decimal | None
    cost_burden: Decimal | None
    quality_label: StructureQualityLabel
    expected_value_state: str
    canonical_hash: str

    @classmethod
    def from_candidate(cls, candidate: OptionStructureCandidate) -> StructureResearchFeatures:
        width_notional = (
            candidate.width * candidate.economics.multiplier * candidate.economics.quantity
        )
        normalized = (
            _exact(candidate.economics.gross_amount / width_notional)
            if width_notional > 0
            else None
        )
        breakeven_distance = _exact(
            (candidate.economics.breakeven - candidate.underlying_reference_price)
            / candidate.underlying_reference_price
        )
        liquidity_burden = max(
            (
                v
                for v in (
                    candidate.quality.long_leg_relative_spread,
                    candidate.quality.short_leg_relative_spread,
                )
                if v is not None
            ),
            default=None,
        )
        round_trip_cash_cost = (
            candidate.economics.fees.opening_cost + candidate.economics.slippage.total_slippage
        )
        cost_burden = (
            _exact(round_trip_cash_cost / candidate.economics.gross_amount)
            if candidate.economics.gross_amount > 0
            else None
        )
        as_of = candidate.evaluated_at.date().isoformat()
        payload = (
            STRUCTURE_RESEARCH_FEATURE_V1,
            candidate.structure_id,
            candidate.underlying_canonical_security_id,
            candidate.underlying_symbol,
            candidate.structure_type,
            as_of,
            normalized,
            candidate.width,
            candidate.economics.maximum_profit,
            candidate.economics.maximum_loss,
            candidate.economics.reward_risk_ratio,
            breakeven_distance,
            candidate.greeks.net_delta,
            candidate.greeks.net_theta,
            candidate.greeks.net_vega,
            candidate.volatility_context.iv_vs_realized_state,
            candidate.volatility_context.skew_state,
            candidate.volatility_context.term_structure_shape,
            liquidity_burden,
            cost_burden,
            candidate.quality.label,
            EXPECTED_VALUE_UNAVAILABLE,
        )
        return cls(
            feature_id=deterministic_id("options_structure_research_feature", payload),
            structure_id=candidate.structure_id,
            underlying_canonical_security_id=candidate.underlying_canonical_security_id,
            underlying_symbol=candidate.underlying_symbol,
            structure_type=candidate.structure_type,
            as_of=as_of,
            normalized_debit_or_credit=normalized,
            width=candidate.width,
            maximum_profit=candidate.economics.maximum_profit,
            maximum_loss=candidate.economics.maximum_loss,
            reward_risk_ratio=candidate.economics.reward_risk_ratio,
            breakeven_distance_percent=breakeven_distance,
            net_delta=candidate.greeks.net_delta,
            net_theta=candidate.greeks.net_theta,
            net_vega=candidate.greeks.net_vega,
            iv_vs_realized_state=candidate.volatility_context.iv_vs_realized_state,
            skew_state=candidate.volatility_context.skew_state,
            term_structure_shape=candidate.volatility_context.term_structure_shape,
            liquidity_burden=liquidity_burden,
            cost_burden=cost_burden,
            quality_label=candidate.quality.label,
            expected_value_state=EXPECTED_VALUE_UNAVAILABLE,
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 23: bounded options-only comparison across already-evaluated
# structures. Every dimension is reported separately; none is ever labeled
# "best trade".
# --------------------------------------------------------------------------

_QUALITY_RANK: dict[StructureQualityLabel, int] = {
    StructureQualityLabel.HIGH_QUALITY: 0,
    StructureQualityLabel.USABLE: 1,
    StructureQualityLabel.MARGINAL: 2,
    StructureQualityLabel.UNUSABLE: 3,
}


@dataclass(frozen=True, slots=True)
class StructureComparisonDimension:
    label: str
    winner_structure_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class StructureExpressionComparison:
    comparison_id: str
    underlying_canonical_security_id: str
    candidate_structure_ids: tuple[str, ...]
    dimensions: tuple[StructureComparisonDimension, ...]
    note: str
    canonical_hash: str


def compare_structure_expressions(
    candidates: tuple[OptionStructureCandidate, ...],
) -> StructureExpressionComparison:
    if len(candidates) < 2:
        raise OptionsStructureError("STRUCTURE_COMPARISON_REQUIRES_AT_LEAST_TWO_CANDIDATES")
    if len({c.underlying_canonical_security_id for c in candidates}) != 1:
        raise OptionsStructureError("STRUCTURE_COMPARISON_REQUIRES_ONE_UNDERLYING")

    def _pick(
        label: str,
        key: Callable[[OptionStructureCandidate], Decimal | int],
        detail: Callable[[OptionStructureCandidate], str],
    ) -> StructureComparisonDimension:
        chosen = min(candidates, key=key)
        return StructureComparisonDimension(label, chosen.structure_id, detail(chosen))

    dimensions = (
        _pick(
            "LOWER_CAPITAL_REQUIRED",
            lambda c: c.economics.capital_required,
            lambda c: f"capital_required={c.economics.capital_required}",
        ),
        _pick(
            "HIGHER_MAXIMUM_REWARD",
            lambda c: -c.economics.maximum_profit,
            lambda c: f"maximum_profit={c.economics.maximum_profit}",
        ),
        _pick(
            "LOWER_COST_BURDEN",
            lambda c: c.economics.fees.opening_cost + c.economics.slippage.total_slippage,
            lambda c: (
                f"opening_cost={c.economics.fees.opening_cost}, "
                f"slippage={c.economics.slippage.total_slippage}"
            ),
        ),
        _pick(
            "BETTER_LIQUIDITY",
            lambda c: _QUALITY_RANK[c.quality.label],
            lambda c: f"quality_label={c.quality.label.value}",
        ),
    )
    payload = (
        STRUCTURE_EXPRESSION_COMPARISON_V1,
        tuple(sorted(c.structure_id for c in candidates)),
        dimensions,
    )
    return StructureExpressionComparison(
        comparison_id=deterministic_id("structure_expression_comparison", payload),
        underlying_canonical_security_id=candidates[0].underlying_canonical_security_id,
        candidate_structure_ids=tuple(c.structure_id for c in candidates),
        dimensions=dimensions,
        note=(
            "Each dimension is reported separately (lower capital, higher "
            "max reward, lower cost burden, better liquidity); none is "
            "labeled the single best trade."
        ),
        canonical_hash=content_digest(payload),
    )
