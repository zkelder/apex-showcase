"""Genuine current options quote-chain and volatility intelligence.

Research-intelligence evidence only. This module never creates an Order,
Strategy, Shared Opportunity, PAPER order, or LIVE authority; every
persisted fact this module produces carries ``authority = "NONE"``.

It reuses -- and never re-derives -- three already-existing identity
layers: ``domain.security_identity`` (stable security identity),
``domain.options_intelligence.OptionContract`` (canonical option-contract
identity and its stable options-underlying linkage), and
``domain.research_cohort`` (the existing genuine multi-asset research
cohort, which this module's bounded quote-chain selection is drawn from
rather than re-declaring an independent symbol list).

Scope boundaries carried from the milestone brief, restated here because
every dataclass below is designed around them:

* current option-chain pricing and implied-volatility structure only --
  never a defined-risk structure, an Opportunity, or an execution proposal
  (``domain.options_intelligence.evaluate_defined_risk_options`` and its
  SHADOW proposal machinery are a separate, already-existing module this
  one does not touch or extend);
* never synthesize a missing bid or ask -- every derived price (midpoint,
  estimated buy/sell, IV, Greeks, expected move) is ``None`` when its
  genuine inputs are unavailable, and every failure carries an explicit
  status/reason rather than a fabricated substitute;
* current retained option-contract *reference* history
  (``options_intelligence_contract``) is not historical *quote* evidence;
  ``HISTORICAL_OPTIONS_PRICING_UNAVAILABLE`` stays explicit until genuine
  historical quote observations exist. This module's snapshots begin
  building that forward dataset one capture at a time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from itertools import pairwise
from math import erf, exp, log, pi, sqrt

from apex.domain.options_intelligence import (
    OptionContract,
    OptionProviderCapabilities,
    OptionType,
)
from apex.domain.research_cohort import ResolvedCohortMember
from apex.domain.values import DecimalInput, content_digest, deterministic_id, parse_decimal

# --------------------------------------------------------------------------
# Versioned content-hash namespaces
# --------------------------------------------------------------------------

OPTIONS_QUOTE_COHORT_SELECTION_V1 = "OPTIONS_QUOTE_COHORT_SELECTION_V1"
OPTIONS_CHAIN_CAPTURE_PLAN_V1 = "OPTIONS_CHAIN_CAPTURE_PLAN_V1"
OPTIONS_CHAIN_CAPTURE_RUN_V1 = "OPTIONS_CHAIN_CAPTURE_RUN_V1"
OPTION_QUOTE_OBSERVATION_V1 = "OPTION_QUOTE_OBSERVATION_V1"
OPTION_DESCRIPTIVE_PROFILE_V1 = "OPTION_DESCRIPTIVE_PROFILE_V1"
APEX_IMPLIED_VOLATILITY_ESTIMATE_V1 = "APEX_IMPLIED_VOLATILITY_ESTIMATE_V1"
APEX_GREEKS_ESTIMATE_V1 = "APEX_GREEKS_ESTIMATE_V1"
VOLATILITY_SURFACE_PROFILE_V1 = "VOLATILITY_SURFACE_PROFILE_V1"
EXPECTED_MOVE_ESTIMATE_V1 = "EXPECTED_MOVE_ESTIMATE_V1"
CHAIN_LIQUIDITY_PROFILE_V1 = "CHAIN_LIQUIDITY_PROFILE_V1"
IV_VS_REALIZED_VOLATILITY_V1 = "IV_VS_REALIZED_VOLATILITY_V1"
OPTIONS_CHAIN_SNAPSHOT_V1 = "OPTIONS_CHAIN_SNAPSHOT_V1"
OPTIONS_VOLATILITY_RESEARCH_FEATURE_V1 = "OPTIONS_VOLATILITY_RESEARCH_FEATURE_V1"
OPTIONS_QUOTE_PROVIDER_CAPABILITY_SNAPSHOT_V1 = "OPTIONS_QUOTE_PROVIDER_CAPABILITY_SNAPSHOT_V1"

HISTORICAL_OPTIONS_PRICING_UNAVAILABLE = "HISTORICAL_OPTIONS_PRICING_UNAVAILABLE"
IV_HISTORY_INSUFFICIENT = "IV_HISTORY_INSUFFICIENT"

OPTIONS_QUOTE_WARNINGS = (
    "OPTIONS QUOTE INTELLIGENCE IS RESEARCH ONLY",
    "MIDPOINT IS DESCRIPTIVE, NEVER EXECUTABLE",
    "IMPLIED VOLATILITY AND GREEKS ARE MODEL ESTIMATES, NOT GUARANTEES",
    "EXPECTED MOVE IS NOT A CONFIDENCE INTERVAL",
    "SKEW IS MARKET PRICING EVIDENCE, NOT A DIRECTIONAL SIGNAL",
    "IV-VERSUS-REALIZED PREMIUM IS NOT AN AUTOMATIC EDGE",
    "NO OPTION ORDER AUTHORITY IS ENABLED",
    "LIVE TRADING IS NOT ENABLED",
)

# Declared numeric policy constants (fixed and documented, never tuned
# against results). Every one is retained on the objects that use it so a
# reviewer can see exactly which assumption produced a given number.
STALENESS_THRESHOLD_SECONDS = 900
ATM_MONEYNESS_BAND = Decimal("0.01")
TARGET_SKEW_DELTA = Decimal("0.25")
RISK_FREE_RATE_DEFAULT = Decimal("0.05")
RISK_FREE_RATE_SOURCE_DEFAULT = "FIXED_FLAT_ASSUMPTION_V1"
DIVIDEND_YIELD_DEFAULT = Decimal("0")
DIVIDEND_YIELD_SOURCE_DEFAULT = "ZERO_ASSUMPTION_UNCONFIRMED_V1"
IV_SOLVE_MIN = Decimal("0.0001")
IV_SOLVE_MAX = Decimal("5")
IV_SOLVE_MAX_ITERATIONS = 100
IV_SOLVE_TOLERANCE = Decimal("0.000001")
IV_SOLVE_METHOD = "BOUNDED_BISECTION_V1"
PRICING_MODEL = "BLACK_SCHOLES_MERTON_V1"
TIME_CONVENTION = "ACT_365_CALENDAR_DAYS_V1"
IV_RANK_MINIMUM_HISTORY = 20
REALIZED_VOLATILITY_WINDOW_SESSIONS = 20
TRADING_DAYS_PER_YEAR = 252


class OptionsQuoteIntelligenceError(ValueError):
    pass


class QuoteObservationStatus(StrEnum):
    OBSERVED = "OBSERVED"
    ONE_SIDED = "ONE_SIDED"
    ZERO_LIQUIDITY = "ZERO_LIQUIDITY"
    STALE = "STALE"
    CROSSED = "CROSSED"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


class MoneynessClassification(StrEnum):
    ITM = "ITM"
    ATM = "ATM"
    OTM = "OTM"


class ExpirationBucket(StrEnum):
    DAYS_0_7 = "DAYS_0_7"
    DAYS_8_30 = "DAYS_8_30"
    DAYS_31_60 = "DAYS_31_60"
    DAYS_61_120 = "DAYS_61_120"
    DAYS_121_365 = "DAYS_121_365"
    DAYS_365_PLUS = "DAYS_365_PLUS"


class IVConvergenceStatus(StrEnum):
    CONVERGED = "CONVERGED"
    FAILED_NO_CONVERGENCE = "FAILED_NO_CONVERGENCE"
    FAILED_INPUT_INVALID = "FAILED_INPUT_INVALID"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class ValueSource(StrEnum):
    PROVIDER = "PROVIDER"
    APEX_CALCULATED = "APEX_CALCULATED"
    UNAVAILABLE = "UNAVAILABLE"


class TermStructureShape(StrEnum):
    CONTANGO_LIKE = "CONTANGO_LIKE"
    BACKWARDATION_LIKE = "BACKWARDATION_LIKE"
    FLAT = "FLAT"
    INSUFFICIENT_EXPIRATIONS = "INSUFFICIENT_EXPIRATIONS"


class SkewShape(StrEnum):
    SMIRK_PUT = "SMIRK_PUT"
    SMIRK_CALL = "SMIRK_CALL"
    SMILE = "SMILE"
    FLAT = "FLAT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class IvRankStatus(StrEnum):
    COMPUTED = "COMPUTED"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"


class ChainCompletionStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    EMPTY = "EMPTY"
    BLOCKED = "BLOCKED"


class CaptureRunStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    BLOCKED_CREDENTIALS_ABSENT = "BLOCKED_CREDENTIALS_ABSENT"
    BLOCKED_PROVIDER_ERROR = "BLOCKED_PROVIDER_ERROR"
    EMPTY = "EMPTY"


def _exact(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return value.quantize(Decimal("0.00000001"))


def _optional_decimal(
    value: DecimalInput | None, *, allow_negative: bool = False
) -> Decimal | None:
    return parse_decimal(value, allow_negative=allow_negative) if value is not None else None


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None:
        raise OptionsQuoteIntelligenceError(f"{label}_MUST_BE_TIMEZONE_AWARE")
    return value


# --------------------------------------------------------------------------
# Section 5: bounded, deterministic chain-selection policy
# --------------------------------------------------------------------------
#
# Drawn from the existing genuine multi-asset research cohort
# (``domain.research_cohort.GENUINE_MULTI_ASSET_COHORT_POLICY_V1``), never
# an independently invented symbol list: every symbol below is one of that
# cohort's already-declared members. Declared here, before any quote
# result exists.

DEFAULT_OPTIONS_COHORT_SYMBOLS: tuple[str, ...] = tuple(
    sorted(
        {
            # Broad index ETFs
            "SPY",
            "QQQ",
            "IWM",
            "DIA",
            # GICS sector SPDRs
            "XLK",
            "XLF",
            "XLE",
            "XLV",
            "XLI",
            "XLY",
            "XLP",
            "XLU",
            "XLB",
            "XLRE",
            "XLC",
            # Highly liquid large-cap single names
            "AAPL",
            "MSFT",
            "NVDA",
            "AMZN",
        }
    )
)


@dataclass(frozen=True, slots=True)
class OptionChainCaptureBounds:
    """Declared, per-underlying capture bounds -- fixed before any request
    is made, never widened in response to a result."""

    max_expirations: int
    dte_minimum: int
    dte_maximum: int
    moneyness_band: Decimal
    max_contracts_per_underlying: int
    max_requests_per_underlying: int
    max_bytes_per_underlying: int
    max_runtime_seconds_per_underlying: int
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        max_expirations: int = 6,
        dte_minimum: int = 0,
        dte_maximum: int = 60,
        moneyness_band: DecimalInput = "0.20",
        max_contracts_per_underlying: int = 200,
        max_requests_per_underlying: int = 3,
        max_bytes_per_underlying: int = 2_000_000,
        max_runtime_seconds_per_underlying: int = 20,
    ) -> OptionChainCaptureBounds:
        band = parse_decimal(moneyness_band, allow_zero=False)
        if not (1 <= max_expirations <= 20):
            raise OptionsQuoteIntelligenceError("BOUNDS_MAX_EXPIRATIONS_OUT_OF_RANGE")
        if not (0 <= dte_minimum < dte_maximum <= 730):
            raise OptionsQuoteIntelligenceError("BOUNDS_DTE_RANGE_INVALID")
        if not (Decimal("0.01") <= band <= Decimal("2")):
            raise OptionsQuoteIntelligenceError("BOUNDS_MONEYNESS_BAND_OUT_OF_RANGE")
        if not (1 <= max_contracts_per_underlying <= 1000):
            raise OptionsQuoteIntelligenceError("BOUNDS_MAX_CONTRACTS_OUT_OF_RANGE")
        if not (1 <= max_requests_per_underlying <= 10):
            raise OptionsQuoteIntelligenceError("BOUNDS_MAX_REQUESTS_OUT_OF_RANGE")
        if not (10_000 <= max_bytes_per_underlying <= 20_000_000):
            raise OptionsQuoteIntelligenceError("BOUNDS_MAX_BYTES_OUT_OF_RANGE")
        if not (1 <= max_runtime_seconds_per_underlying <= 120):
            raise OptionsQuoteIntelligenceError("BOUNDS_MAX_RUNTIME_OUT_OF_RANGE")
        payload = (
            max_expirations,
            dte_minimum,
            dte_maximum,
            band,
            max_contracts_per_underlying,
            max_requests_per_underlying,
            max_bytes_per_underlying,
            max_runtime_seconds_per_underlying,
        )
        return cls(*payload, content_digest((OPTIONS_CHAIN_CAPTURE_PLAN_V1, "BOUNDS", payload)))


@dataclass(frozen=True, slots=True)
class OptionChainCapturePlan:
    """A declared, bounded cohort of underlyings plus fixed per-underlying
    request bounds. Immutable once created; a changed cohort or bound
    requires a new plan, never an in-place edit."""

    plan_id: str
    cohort_policy_id: str
    cohort_policy_hash: str
    selection_version: str
    members: tuple[ResolvedCohortMember, ...]
    unresolved_symbols: tuple[str, ...]
    bounds: OptionChainCaptureBounds
    provider: str
    feed: str
    created_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        cohort_policy_id: str,
        cohort_policy_hash: str,
        members: tuple[ResolvedCohortMember, ...],
        unresolved_symbols: tuple[str, ...],
        bounds: OptionChainCaptureBounds,
        provider: str = "ALPACA_OPTIONS_MARKET_DATA",
        feed: str = "indicative",
        created_at: datetime,
    ) -> OptionChainCapturePlan:
        _aware(created_at, "PLAN_CREATED_AT")
        if feed not in ("indicative", "opra"):
            raise OptionsQuoteIntelligenceError("PLAN_FEED_UNSUPPORTED")
        if not provider:
            raise OptionsQuoteIntelligenceError("PLAN_PROVIDER_REQUIRED")
        ordered = tuple(sorted(members, key=lambda item: item.symbol))
        symbols = [item.symbol for item in ordered]
        if len(set(symbols)) != len(symbols):
            raise OptionsQuoteIntelligenceError("PLAN_DUPLICATE_SYMBOL")
        if not (10 <= len(ordered) <= 25):
            raise OptionsQuoteIntelligenceError("PLAN_COHORT_SIZE_OUT_OF_BOUNDS")
        if set(symbols) - set(DEFAULT_OPTIONS_COHORT_SYMBOLS):
            raise OptionsQuoteIntelligenceError("PLAN_SYMBOL_NOT_IN_DECLARED_SELECTION")
        # Identity is the declared configuration only (cohort, bounds,
        # provider, feed) -- deliberately excluding ``created_at`` -- so
        # ``application.options_quote_intelligence.CreateOptionChainCapturePlan``
        # can be genuinely idempotent: re-declaring the identical policy
        # (e.g. an operator re-running this command days apart with an
        # unchanged cohort/bounds) always resolves to the identical
        # ``plan_id`` instead of accumulating near-duplicate plan rows.
        identity_payload = (
            OPTIONS_CHAIN_CAPTURE_PLAN_V1,
            cohort_policy_id,
            cohort_policy_hash,
            "OPTIONS_QUOTE_COHORT_V1",
            tuple(
                (item.stable_security_id, item.symbol, item.sector.value, item.asset_class)
                for item in ordered
            ),
            tuple(sorted(unresolved_symbols)),
            bounds.canonical_hash,
            provider,
            feed,
        )
        payload = (*identity_payload, created_at)
        return cls(
            deterministic_id("options_chain_capture_plan", identity_payload),
            cohort_policy_id,
            cohort_policy_hash,
            "OPTIONS_QUOTE_COHORT_V1",
            ordered,
            tuple(sorted(unresolved_symbols)),
            bounds,
            provider,
            feed,
            created_at,
            content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 1: genuine provider capability
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionsQuoteProviderCapabilitySnapshot:
    """The genuine, credential-gated outcome of one capability check --
    never inferred from authentication alone. ``requests_made`` is the
    exact count of network requests the check itself issued (zero when
    credentials are absent)."""

    checked_at: datetime
    credentials_available: bool
    requests_made: int
    capability: OptionProviderCapabilities
    unavailable_reason: str | None
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        checked_at: datetime,
        credentials_available: bool,
        requests_made: int,
        capability: OptionProviderCapabilities,
        unavailable_reason: str | None,
    ) -> OptionsQuoteProviderCapabilitySnapshot:
        _aware(checked_at, "CAPABILITY_CHECKED_AT")
        if requests_made < 0:
            raise OptionsQuoteIntelligenceError("CAPABILITY_REQUEST_COUNT_INVALID")
        if not credentials_available and requests_made != 0:
            raise OptionsQuoteIntelligenceError(
                "CAPABILITY_ABSENT_CREDENTIALS_MUST_MAKE_NO_REQUEST"
            )
        payload = (
            OPTIONS_QUOTE_PROVIDER_CAPABILITY_SNAPSHOT_V1,
            checked_at,
            credentials_available,
            requests_made,
            capability.canonical_hash,
            unavailable_reason,
        )
        return cls(
            checked_at,
            credentials_available,
            requests_made,
            capability,
            unavailable_reason,
            content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 3-4: the quote observation model and its data-quality rules
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionQuoteObservation:
    """One genuine current option observation bound to a stable contract
    identity. Every derived price field is ``None``, not a fabricated
    zero, when its inputs are unavailable."""

    observation_id: str
    contract: OptionContract
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    last_trade: Decimal | None
    trade_size: Decimal | None
    midpoint: Decimal | None
    estimated_buy_price: Decimal | None
    estimated_sell_price: Decimal | None
    underlying_reference_price: Decimal
    volume: Decimal | None
    open_interest: Decimal | None
    provider_implied_volatility: Decimal | None
    provider_delta: Decimal | None
    provider_gamma: Decimal | None
    provider_theta: Decimal | None
    provider_vega: Decimal | None
    provider_rho: Decimal | None
    market_status: str
    quote_timestamp: datetime
    availability_timestamp: datetime
    ingestion_timestamp: datetime
    staleness_seconds: int
    provider: str
    source_receipt_ids: tuple[str, ...]
    status: QuoteObservationStatus
    exclusion_reasons: tuple[str, ...]
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        contract: OptionContract,
        expected_underlying_canonical_security_id: str,
        bid: DecimalInput | None,
        ask: DecimalInput | None,
        bid_size: DecimalInput | None,
        ask_size: DecimalInput | None,
        last_trade: DecimalInput | None,
        trade_size: DecimalInput | None,
        underlying_reference_price: DecimalInput,
        volume: DecimalInput | None,
        open_interest: DecimalInput | None,
        provider_implied_volatility: DecimalInput | None,
        provider_delta: DecimalInput | None,
        provider_gamma: DecimalInput | None,
        provider_theta: DecimalInput | None,
        provider_vega: DecimalInput | None,
        provider_rho: DecimalInput | None,
        market_status: str,
        quote_timestamp: datetime,
        availability_timestamp: datetime,
        ingestion_timestamp: datetime,
        captured_at: datetime,
        provider: str,
        source_receipt_ids: tuple[str, ...],
    ) -> OptionQuoteObservation:
        if contract.underlying_canonical_security_id != expected_underlying_canonical_security_id:
            raise OptionsQuoteIntelligenceError("OPTION_QUOTE_UNDERLYING_MISMATCH")
        _aware(quote_timestamp, "QUOTE_TIMESTAMP")
        _aware(availability_timestamp, "AVAILABILITY_TIMESTAMP")
        _aware(ingestion_timestamp, "INGESTION_TIMESTAMP")
        _aware(captured_at, "CAPTURED_AT")
        if not (quote_timestamp <= availability_timestamp <= ingestion_timestamp):
            raise OptionsQuoteIntelligenceError("OPTION_QUOTE_TIMESTAMP_ORDERING_INVALID")
        if not provider or not source_receipt_ids:
            raise OptionsQuoteIntelligenceError("OPTION_QUOTE_PROVENANCE_REQUIRED")
        price = parse_decimal(underlying_reference_price, allow_zero=False)
        parsed_bid = _optional_decimal(bid)
        parsed_ask = _optional_decimal(ask)
        parsed_bid_size = _optional_decimal(bid_size)
        parsed_ask_size = _optional_decimal(ask_size)
        expiry = date.fromisoformat(contract.expiration)
        status, reasons = _classify_quote(
            bid=parsed_bid,
            ask=parsed_ask,
            bid_size=parsed_bid_size,
            ask_size=parsed_ask_size,
            quote_timestamp=quote_timestamp,
            captured_at=captured_at,
            expiration=expiry,
        )
        staleness_seconds = max(0, int((captured_at - quote_timestamp).total_seconds()))
        two_sided_valid = (
            status not in (QuoteObservationStatus.UNAVAILABLE, QuoteObservationStatus.INVALID)
            and parsed_bid is not None
            and parsed_ask is not None
            and parsed_ask >= parsed_bid
        )
        midpoint = (
            _exact((parsed_bid + parsed_ask) / 2)
            if two_sided_valid and parsed_bid is not None and parsed_ask is not None
            else None
        )
        buy_price = (
            parsed_ask
            if status is not QuoteObservationStatus.INVALID and parsed_ask is not None
            else None
        )
        sell_price = (
            parsed_bid
            if status not in (QuoteObservationStatus.INVALID, QuoteObservationStatus.CROSSED)
            and parsed_bid is not None
            else None
        )
        greeks = (
            _optional_decimal(provider_delta, allow_negative=True),
            _optional_decimal(provider_gamma, allow_negative=True),
            _optional_decimal(provider_theta, allow_negative=True),
            _optional_decimal(provider_vega, allow_negative=True),
            _optional_decimal(provider_rho, allow_negative=True),
        )
        parsed_last_trade = _optional_decimal(last_trade)
        parsed_trade_size = _optional_decimal(trade_size)
        parsed_volume = _optional_decimal(volume)
        parsed_oi = _optional_decimal(open_interest)
        parsed_iv = _optional_decimal(provider_implied_volatility)
        payload = (
            OPTION_QUOTE_OBSERVATION_V1,
            contract.canonical_hash,
            parsed_bid,
            parsed_ask,
            parsed_bid_size,
            parsed_ask_size,
            parsed_last_trade,
            parsed_trade_size,
            midpoint,
            buy_price,
            sell_price,
            price,
            parsed_volume,
            parsed_oi,
            parsed_iv,
            greeks,
            market_status,
            quote_timestamp,
            availability_timestamp,
            ingestion_timestamp,
            staleness_seconds,
            provider,
            tuple(sorted(source_receipt_ids)),
            status,
            tuple(sorted(set(reasons))),
        )
        return cls(
            deterministic_id("option_quote_observation", payload),
            contract,
            parsed_bid,
            parsed_ask,
            parsed_bid_size,
            parsed_ask_size,
            parsed_last_trade,
            parsed_trade_size,
            midpoint,
            buy_price,
            sell_price,
            price,
            parsed_volume,
            parsed_oi,
            parsed_iv,
            *greeks,
            market_status,
            quote_timestamp,
            availability_timestamp,
            ingestion_timestamp,
            staleness_seconds,
            provider,
            tuple(sorted(source_receipt_ids)),
            status,
            tuple(sorted(set(reasons))),
            content_digest(payload),
        )

    @property
    def is_quotable(self) -> bool:
        return self.status in (
            QuoteObservationStatus.OBSERVED,
            QuoteObservationStatus.ZERO_LIQUIDITY,
        )

    @property
    def spread_width(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


def _classify_quote(
    *,
    bid: Decimal | None,
    ask: Decimal | None,
    bid_size: Decimal | None,
    ask_size: Decimal | None,
    quote_timestamp: datetime,
    captured_at: datetime,
    expiration: date,
) -> tuple[QuoteObservationStatus, list[str]]:
    reasons: list[str] = []
    if expiration < captured_at.date():
        return QuoteObservationStatus.INVALID, ["CONTRACT_EXPIRED"]
    if ask is not None and ask <= 0:
        return QuoteObservationStatus.INVALID, ["NONPOSITIVE_ASK"]
    if bid is not None and ask is not None and ask < bid:
        return QuoteObservationStatus.CROSSED, ["CROSSED_QUOTE"]
    if bid is None and ask is None:
        return QuoteObservationStatus.UNAVAILABLE, ["BID_ASK_BOTH_MISSING"]
    is_stale = (captured_at - quote_timestamp).total_seconds() > STALENESS_THRESHOLD_SECONDS
    if is_stale:
        reasons.append("STALE_QUOTE")
    if bid is None:
        reasons.append("MISSING_BID")
    if ask is None:
        reasons.append("MISSING_ASK")
    zero_liquidity = False
    if bid is not None and bid == 0:
        reasons.append("ZERO_BID")
        zero_liquidity = True
    if bid_size is not None and bid_size == 0 and bid is not None and bid > 0:
        reasons.append("ZERO_BID_SIZE")
        zero_liquidity = True
    if ask_size is not None and ask_size == 0:
        reasons.append("ZERO_ASK_SIZE")
        zero_liquidity = True
    if is_stale:
        return QuoteObservationStatus.STALE, reasons
    if bid is None or ask is None:
        return QuoteObservationStatus.ONE_SIDED, reasons
    if zero_liquidity:
        return QuoteObservationStatus.ZERO_LIQUIDITY, reasons
    return QuoteObservationStatus.OBSERVED, reasons


# --------------------------------------------------------------------------
# Section 6: moneyness and maturity descriptive metadata
# --------------------------------------------------------------------------


def classify_moneyness(
    *, underlying_price: Decimal, strike: Decimal, option_type: OptionType
) -> MoneynessClassification:
    """The one ATM/ITM/OTM rule, shared by ``OptionDescriptiveProfile`` and
    the deterministic stratified chain-sampling selector below -- never
    re-derived in a second place."""

    percent_moneyness = _exact(underlying_price / strike - 1)
    if abs(percent_moneyness) <= ATM_MONEYNESS_BAND:
        return MoneynessClassification.ATM
    if option_type is OptionType.CALL:
        return (
            MoneynessClassification.ITM
            if underlying_price > strike
            else MoneynessClassification.OTM
        )
    return MoneynessClassification.ITM if underlying_price < strike else MoneynessClassification.OTM


@dataclass(frozen=True, slots=True)
class OptionDescriptiveProfile:
    """Deterministic descriptive metadata only -- never an attractiveness
    or ranking signal."""

    canonical_contract_id: str
    moneyness_classification: MoneynessClassification
    percent_moneyness: Decimal
    strike_distance: Decimal
    dte: int
    expiration_bucket: ExpirationBucket
    option_type: OptionType
    canonical_hash: str

    @classmethod
    def create(
        cls, *, contract: OptionContract, underlying_price: Decimal, as_of: date
    ) -> OptionDescriptiveProfile:
        dte = (date.fromisoformat(contract.expiration) - as_of).days
        percent_moneyness = _exact(underlying_price / contract.strike - 1)
        strike_distance = _exact(abs(underlying_price - contract.strike))
        classification = classify_moneyness(
            underlying_price=underlying_price,
            strike=contract.strike,
            option_type=contract.option_type,
        )
        bucket = _expiration_bucket(dte)
        payload = (
            OPTION_DESCRIPTIVE_PROFILE_V1,
            contract.canonical_contract_id,
            classification,
            percent_moneyness,
            strike_distance,
            dte,
            bucket,
            contract.option_type,
        )
        return cls(
            contract.canonical_contract_id,
            classification,
            percent_moneyness,
            strike_distance,
            dte,
            bucket,
            contract.option_type,
            content_digest(payload),
        )


def _expiration_bucket(dte: int) -> ExpirationBucket:
    if dte <= 7:
        return ExpirationBucket.DAYS_0_7
    if dte <= 30:
        return ExpirationBucket.DAYS_8_30
    if dte <= 60:
        return ExpirationBucket.DAYS_31_60
    if dte <= 120:
        return ExpirationBucket.DAYS_61_120
    if dte <= 365:
        return ExpirationBucket.DAYS_121_365
    return ExpirationBucket.DAYS_365_PLUS


# --------------------------------------------------------------------------
# Pure Black-Scholes-Merton pricing kernel (float internally; every
# authoritative input/output at the boundary is Decimal). Bisection is
# used for IV solving instead of Newton-Raphson because BS price is
# strictly monotonic increasing in sigma: bisection over a fixed bracket
# always converges without a vega-near-zero derivative blow-up, at the
# cost of more iterations than Newton would need -- an explicit, bounded
# trade-off, not an oversight.
# --------------------------------------------------------------------------


def _norm_pdf(x: float) -> float:
    return exp(-x * x / 2.0) / sqrt(2.0 * pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _d1_d2(
    *, spot: float, strike: float, t: float, r: float, q: float, sigma: float
) -> tuple[float, float]:
    d1 = (log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * sqrt(t))
    d2 = d1 - sigma * sqrt(t)
    return d1, d2


def _bs_price(
    *, spot: float, strike: float, t: float, r: float, q: float, sigma: float, is_call: bool
) -> float:
    if t <= 0 or sigma <= 0:
        intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
        return intrinsic
    d1, d2 = _d1_d2(spot=spot, strike=strike, t=t, r=r, q=q, sigma=sigma)
    if is_call:
        return spot * exp(-q * t) * _norm_cdf(d1) - strike * exp(-r * t) * _norm_cdf(d2)
    return strike * exp(-r * t) * _norm_cdf(-d2) - spot * exp(-q * t) * _norm_cdf(-d1)


def _discounted_intrinsic(
    *, spot: float, strike: float, t: float, r: float, q: float, is_call: bool
) -> float:
    forward_spot = spot * exp(-q * t)
    forward_strike = strike * exp(-r * t)
    return (
        max(0.0, forward_spot - forward_strike)
        if is_call
        else max(0.0, forward_strike - forward_spot)
    )


@dataclass(frozen=True, slots=True)
class ApexImpliedVolatilityEstimate:
    """A deterministic Apex-calculated IV, fully distinguishable from any
    provider-supplied IV retained on the observation. ``implied_volatility``
    is ``None`` -- never a substituted zero -- whenever the solve fails."""

    estimate_id: str
    canonical_contract_id: str
    pricing_model: str
    underlying_price: Decimal
    strike: Decimal
    time_to_expiry_years: Decimal
    time_convention: str
    option_price_source: str
    option_price: Decimal
    risk_free_rate: Decimal
    risk_free_rate_source: str
    dividend_yield: Decimal
    dividend_yield_source: str
    solve_method: str
    iterations: int
    convergence_status: IVConvergenceStatus
    implied_volatility: Decimal | None
    canonical_hash: str

    @classmethod
    def solve(
        cls,
        *,
        contract: OptionContract,
        underlying_price: Decimal,
        option_price: Decimal,
        option_price_source: str,
        as_of: date,
        risk_free_rate: Decimal = RISK_FREE_RATE_DEFAULT,
        risk_free_rate_source: str = RISK_FREE_RATE_SOURCE_DEFAULT,
        dividend_yield: Decimal = DIVIDEND_YIELD_DEFAULT,
        dividend_yield_source: str = DIVIDEND_YIELD_SOURCE_DEFAULT,
    ) -> ApexImpliedVolatilityEstimate:
        dte = (date.fromisoformat(contract.expiration) - as_of).days
        t_years = _exact(Decimal(dte) / Decimal(365))
        spot = float(underlying_price)
        strike = float(contract.strike)
        t = float(t_years)
        r = float(risk_free_rate)
        q = float(dividend_yield)
        price = float(option_price)
        is_call = contract.option_type is OptionType.CALL
        status = IVConvergenceStatus.NOT_ATTEMPTED
        iv_value: Decimal | None = None
        iterations = 0
        if dte <= 0 or underlying_price <= 0 or contract.strike <= 0 or option_price <= 0:
            status = IVConvergenceStatus.FAILED_INPUT_INVALID
        else:
            lower_bound = _discounted_intrinsic(
                spot=spot, strike=strike, t=t, r=r, q=q, is_call=is_call
            )
            if price < lower_bound - 1e-9:
                status = IVConvergenceStatus.FAILED_INPUT_INVALID
            else:
                low, high = float(IV_SOLVE_MIN), float(IV_SOLVE_MAX)
                price_at_high = _bs_price(
                    spot=spot, strike=strike, t=t, r=r, q=q, sigma=high, is_call=is_call
                )
                if price > price_at_high:
                    status = IVConvergenceStatus.FAILED_NO_CONVERGENCE
                else:
                    tolerance = float(IV_SOLVE_TOLERANCE)
                    solved = None
                    for iteration in range(1, IV_SOLVE_MAX_ITERATIONS + 1):
                        iterations = iteration
                        mid = (low + high) / 2.0
                        mid_price = _bs_price(
                            spot=spot, strike=strike, t=t, r=r, q=q, sigma=mid, is_call=is_call
                        )
                        if abs(mid_price - price) <= tolerance or (high - low) <= tolerance:
                            solved = mid
                            break
                        if mid_price < price:
                            low = mid
                        else:
                            high = mid
                    if solved is None:
                        solved = (low + high) / 2.0
                        mid_price = _bs_price(
                            spot=spot, strike=strike, t=t, r=r, q=q, sigma=solved, is_call=is_call
                        )
                        if abs(mid_price - price) > tolerance * 100:
                            status = IVConvergenceStatus.FAILED_NO_CONVERGENCE
                            solved = None
                    if solved is not None:
                        status = IVConvergenceStatus.CONVERGED
                        iv_value = _exact(Decimal(str(round(solved, 10))))
        payload = (
            APEX_IMPLIED_VOLATILITY_ESTIMATE_V1,
            contract.canonical_contract_id,
            PRICING_MODEL,
            underlying_price,
            contract.strike,
            t_years,
            TIME_CONVENTION,
            option_price_source,
            option_price,
            risk_free_rate,
            risk_free_rate_source,
            dividend_yield,
            dividend_yield_source,
            IV_SOLVE_METHOD,
            iterations,
            status,
            iv_value,
        )
        return cls(
            deterministic_id("apex_iv_estimate", payload),
            contract.canonical_contract_id,
            PRICING_MODEL,
            underlying_price,
            contract.strike,
            t_years,
            TIME_CONVENTION,
            option_price_source,
            option_price,
            risk_free_rate,
            risk_free_rate_source,
            dividend_yield,
            dividend_yield_source,
            IV_SOLVE_METHOD,
            iterations,
            status,
            iv_value,
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class ApexGreeksEstimate:
    """Closed-form Black-Scholes-Merton Greeks computed from the identical
    assumptions retained on the paired IV estimate. Distinct from any
    provider-supplied Greeks retained on the observation."""

    estimate_id: str
    canonical_contract_id: str
    pricing_model: str
    iv_source: ValueSource
    volatility_used: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    canonical_hash: str

    @classmethod
    def compute(
        cls,
        *,
        contract: OptionContract,
        underlying_price: Decimal,
        as_of: date,
        volatility: Decimal | None,
        iv_source: ValueSource,
        risk_free_rate: Decimal = RISK_FREE_RATE_DEFAULT,
        dividend_yield: Decimal = DIVIDEND_YIELD_DEFAULT,
    ) -> ApexGreeksEstimate:
        dte = (date.fromisoformat(contract.expiration) - as_of).days
        delta = gamma = theta = vega = None
        if volatility is not None and volatility > 0 and dte > 0 and underlying_price > 0:
            spot = float(underlying_price)
            strike = float(contract.strike)
            t = float(Decimal(dte) / Decimal(365))
            r = float(risk_free_rate)
            q = float(dividend_yield)
            sigma = float(volatility)
            is_call = contract.option_type is OptionType.CALL
            d1, d2 = _d1_d2(spot=spot, strike=strike, t=t, r=r, q=q, sigma=sigma)
            disc_q = exp(-q * t)
            disc_r = exp(-r * t)
            pdf_d1 = _norm_pdf(d1)
            raw_delta = disc_q * _norm_cdf(d1) if is_call else -disc_q * _norm_cdf(-d1)
            raw_gamma = disc_q * pdf_d1 / (spot * sigma * sqrt(t))
            raw_vega = spot * disc_q * pdf_d1 * sqrt(t) / 100.0
            if is_call:
                raw_theta = (
                    -(spot * pdf_d1 * sigma * disc_q) / (2 * sqrt(t))
                    - r * strike * disc_r * _norm_cdf(d2)
                    + q * spot * disc_q * _norm_cdf(d1)
                ) / 365.0
            else:
                raw_theta = (
                    -(spot * pdf_d1 * sigma * disc_q) / (2 * sqrt(t))
                    + r * strike * disc_r * _norm_cdf(-d2)
                    - q * spot * disc_q * _norm_cdf(-d1)
                ) / 365.0
            delta = _exact(Decimal(str(round(raw_delta, 10))))
            gamma = _exact(Decimal(str(round(raw_gamma, 10))))
            theta = _exact(Decimal(str(round(raw_theta, 10))))
            vega = _exact(Decimal(str(round(raw_vega, 10))))
        payload = (
            APEX_GREEKS_ESTIMATE_V1,
            contract.canonical_contract_id,
            PRICING_MODEL,
            iv_source,
            volatility,
            delta,
            gamma,
            theta,
            vega,
        )
        return cls(
            deterministic_id("apex_greeks_estimate", payload),
            contract.canonical_contract_id,
            PRICING_MODEL,
            iv_source,
            volatility,
            delta,
            gamma,
            theta,
            vega,
            content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 10-14: volatility surface, skew, term structure, liquidity
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AtmImpliedVolatilityPoint:
    expiration: str
    dte: int
    atm_iv: Decimal | None
    source: ValueSource
    contracts_considered: int


@dataclass(frozen=True, slots=True)
class TermStructureSummary:
    points: tuple[AtmImpliedVolatilityPoint, ...]
    shape: TermStructureShape
    near_far_spread: Decimal | None


@dataclass(frozen=True, slots=True)
class SkewProfile:
    expiration: str
    dte: int
    put_iv_target_delta: Decimal | None
    call_iv_target_delta: Decimal | None
    skew_target_delta: Decimal | None
    downside_atm_premium: Decimal | None
    slope: Decimal | None
    shape: SkewShape


@dataclass(frozen=True, slots=True)
class CallPutLiquidityAsymmetry:
    call_avg_relative_spread: Decimal | None
    put_avg_relative_spread: Decimal | None
    call_zero_bid_rate: Decimal
    put_zero_bid_rate: Decimal
    call_avg_displayed_size: Decimal | None
    put_avg_displayed_size: Decimal | None


@dataclass(frozen=True, slots=True)
class IvRankResult:
    status: IvRankStatus
    percentile: Decimal | None
    retained_snapshot_count: int
    minimum_required: int


@dataclass(frozen=True, slots=True)
class VolatilitySurfaceProfile:
    underlying_canonical_security_id: str
    captured_at: datetime
    atm_points: tuple[AtmImpliedVolatilityPoint, ...]
    term_structure: TermStructureSummary
    skew_by_expiration: tuple[SkewProfile, ...]
    iv_rank: IvRankResult
    liquidity_asymmetry: CallPutLiquidityAsymmetry
    canonical_hash: str


def _relative_spread(
    bid: Decimal | None, ask: Decimal | None, midpoint: Decimal | None
) -> Decimal | None:
    if bid is None or ask is None or midpoint is None or midpoint <= 0:
        return None
    return _exact((ask - bid) / midpoint)


def build_atm_points(
    observations: tuple[OptionQuoteObservation, ...],
    ivs_by_contract: dict[str, Decimal],
) -> tuple[AtmImpliedVolatilityPoint, ...]:
    """One ATM IV point per distinct expiration -- the quotable contract
    (either side, call or put) with the smallest absolute moneyness."""

    by_expiration: dict[str, list[OptionQuoteObservation]] = {}
    for observation in observations:
        by_expiration.setdefault(observation.contract.expiration, []).append(observation)
    points: list[AtmImpliedVolatilityPoint] = []
    for expiration, members in sorted(by_expiration.items()):
        quotable = [item for item in members if item.is_quotable]
        candidates = quotable or members
        nearest = min(
            candidates,
            key=lambda item: abs(item.underlying_reference_price - item.contract.strike),
        )
        dte = (date.fromisoformat(expiration) - nearest.quote_timestamp.date()).days
        iv = ivs_by_contract.get(nearest.contract.canonical_contract_id)
        source = ValueSource.APEX_CALCULATED if iv is not None else ValueSource.UNAVAILABLE
        if iv is None and nearest.provider_implied_volatility is not None:
            iv = nearest.provider_implied_volatility
            source = ValueSource.PROVIDER
        points.append(AtmImpliedVolatilityPoint(expiration, dte, iv, source, len(quotable)))
    return tuple(points)


def build_term_structure(points: tuple[AtmImpliedVolatilityPoint, ...]) -> TermStructureSummary:
    usable = tuple(sorted((p for p in points if p.atm_iv is not None), key=lambda item: item.dte))
    if len(usable) < 2:
        return TermStructureSummary(points, TermStructureShape.INSUFFICIENT_EXPIRATIONS, None)
    spread = _exact(usable[-1].atm_iv - usable[0].atm_iv)  # type: ignore[operator]
    flat_band = Decimal("0.01")
    if abs(spread) <= flat_band:
        shape = TermStructureShape.FLAT
    elif spread > 0:
        shape = TermStructureShape.CONTANGO_LIKE
    else:
        shape = TermStructureShape.BACKWARDATION_LIKE
    return TermStructureSummary(points, shape, spread)


def build_skew_profile(
    expiration: str,
    dte: int,
    observations: tuple[OptionQuoteObservation, ...],
    ivs_by_contract: dict[str, Decimal],
    deltas_by_contract: dict[str, Decimal],
) -> SkewProfile:
    members = [item for item in observations if item.contract.expiration == expiration]
    calls = [item for item in members if item.contract.option_type is OptionType.CALL]
    puts = [item for item in members if item.contract.option_type is OptionType.PUT]

    def _iv_of(item: OptionQuoteObservation) -> Decimal | None:
        return ivs_by_contract.get(
            item.contract.canonical_contract_id, item.provider_implied_volatility
        )

    def _delta_of(item: OptionQuoteObservation) -> Decimal | None:
        value = deltas_by_contract.get(item.contract.canonical_contract_id)
        return value if value is not None else item.provider_delta

    def _nearest_target_delta(
        pool: list[OptionQuoteObservation], target: Decimal
    ) -> Decimal | None:
        scored = [
            (abs(abs(_delta_of(item)) - target), _iv_of(item))  # type: ignore[arg-type]
            for item in pool
            if _delta_of(item) is not None and _iv_of(item) is not None
        ]
        if not scored:
            return None
        return min(scored, key=lambda pair: pair[0])[1]

    put_iv = _nearest_target_delta(puts, TARGET_SKEW_DELTA)
    call_iv = _nearest_target_delta(calls, TARGET_SKEW_DELTA)
    skew = _exact(put_iv - call_iv) if put_iv is not None and call_iv is not None else None

    atm_candidates = [item for item in members if _iv_of(item) is not None]
    downside_premium = None
    slope = None
    if atm_candidates:
        atm = min(
            atm_candidates,
            key=lambda item: abs(item.underlying_reference_price - item.contract.strike),
        )
        atm_iv = _iv_of(atm)
        downside_pool = [
            item
            for item in puts
            if _iv_of(item) is not None
            and item.contract.strike < item.underlying_reference_price * Decimal("0.9")
        ]
        if downside_pool and atm_iv is not None:
            reference_strike = atm.underlying_reference_price * Decimal("0.9")
            nearest_downside = min(
                downside_pool, key=lambda item: abs(item.contract.strike - reference_strike)
            )
            downside_iv = _iv_of(nearest_downside)
            if downside_iv is not None:
                downside_premium = _exact(downside_iv - atm_iv)
        points: list[tuple[Decimal, Decimal]] = []
        for item in members:
            iv_value = _iv_of(item)
            if iv_value is not None:
                points.append(
                    (_exact(item.underlying_reference_price / item.contract.strike - 1), iv_value)
                )
        if len(points) >= 3:
            slope = _linear_slope(points)

    if skew is None and slope is None:
        shape = SkewShape.INSUFFICIENT_DATA
    else:
        threshold = Decimal("0.02")
        reference = skew if skew is not None else slope
        assert reference is not None
        if abs(reference) <= threshold:
            shape = SkewShape.FLAT
        elif reference > 0:
            shape = SkewShape.SMIRK_PUT
        else:
            shape = SkewShape.SMIRK_CALL
    return SkewProfile(expiration, dte, put_iv, call_iv, skew, downside_premium, slope, shape)


def _linear_slope(points: list[tuple[Decimal, Decimal]]) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        n = Decimal(len(points))
        sum_x = sum((p[0] for p in points), Decimal(0))
        sum_y = sum((p[1] for p in points), Decimal(0))
        mean_x = sum_x / n
        mean_y = sum_y / n
        numerator = sum(((x - mean_x) * (y - mean_y) for x, y in points), Decimal(0))
        denominator = sum(((x - mean_x) ** 2 for x, _ in points), Decimal(0))
        if denominator == 0:
            return Decimal(0)
        return _exact(numerator / denominator)


def build_liquidity_asymmetry(
    observations: tuple[OptionQuoteObservation, ...],
) -> CallPutLiquidityAsymmetry:
    calls = [item for item in observations if item.contract.option_type is OptionType.CALL]
    puts = [item for item in observations if item.contract.option_type is OptionType.PUT]

    def _summary(
        pool: list[OptionQuoteObservation],
    ) -> tuple[Decimal | None, Decimal, Decimal | None]:
        if not pool:
            return None, Decimal(0), None
        spreads = [
            s
            for item in pool
            if (s := _relative_spread(item.bid, item.ask, item.midpoint)) is not None
        ]
        avg_spread = _exact(sum(spreads, Decimal(0)) / Decimal(len(spreads))) if spreads else None
        zero_bid = sum(1 for item in pool if item.bid is None or item.bid == 0)
        zero_bid_rate = _exact(Decimal(zero_bid) / Decimal(len(pool)))
        sizes = [
            (item.bid_size or Decimal(0)) + (item.ask_size or Decimal(0))
            for item in pool
            if item.bid_size is not None or item.ask_size is not None
        ]
        avg_size = _exact(sum(sizes, Decimal(0)) / Decimal(len(sizes)) / 2) if sizes else None
        return avg_spread, zero_bid_rate, avg_size

    call_spread, call_zero_bid, call_size = _summary(calls)
    put_spread, put_zero_bid, put_size = _summary(puts)
    return CallPutLiquidityAsymmetry(
        call_spread, put_spread, call_zero_bid, put_zero_bid, call_size, put_size
    )


def build_iv_rank(
    *,
    current_atm_iv: Decimal | None,
    prior_atm_iv_history: tuple[Decimal, ...],
    minimum_required: int = IV_RANK_MINIMUM_HISTORY,
) -> IvRankResult:
    """Never fabricates a rank from one snapshot. ``IV_HISTORY_INSUFFICIENT``
    (``IvRankStatus.INSUFFICIENT_HISTORY``) stays explicit below
    ``minimum_required`` retained prior snapshots."""

    count = len(prior_atm_iv_history)
    if current_atm_iv is None or count < minimum_required:
        return IvRankResult(IvRankStatus.INSUFFICIENT_HISTORY, None, count, minimum_required)
    below = sum(1 for value in prior_atm_iv_history if value <= current_atm_iv)
    percentile = _exact(Decimal(below) / Decimal(count))
    return IvRankResult(IvRankStatus.COMPUTED, percentile, count, minimum_required)


def build_volatility_surface(
    *,
    underlying_canonical_security_id: str,
    captured_at: datetime,
    observations: tuple[OptionQuoteObservation, ...],
    ivs_by_contract: dict[str, Decimal],
    deltas_by_contract: dict[str, Decimal],
    prior_atm_iv_history: tuple[Decimal, ...] = (),
) -> VolatilitySurfaceProfile:
    atm_points = build_atm_points(observations, ivs_by_contract)
    term_structure = build_term_structure(atm_points)
    expirations = sorted({item.contract.expiration for item in observations})
    skew_profiles = tuple(
        build_skew_profile(
            expiration,
            (date.fromisoformat(expiration) - captured_at.date()).days,
            observations,
            ivs_by_contract,
            deltas_by_contract,
        )
        for expiration in expirations
    )
    nearest_atm_iv = atm_points[0].atm_iv if atm_points else None
    iv_rank = build_iv_rank(
        current_atm_iv=nearest_atm_iv, prior_atm_iv_history=prior_atm_iv_history
    )
    liquidity_asymmetry = build_liquidity_asymmetry(observations)
    payload = (
        VOLATILITY_SURFACE_PROFILE_V1,
        underlying_canonical_security_id,
        captured_at,
        atm_points,
        term_structure,
        skew_profiles,
        iv_rank,
        liquidity_asymmetry,
    )
    return VolatilitySurfaceProfile(
        underlying_canonical_security_id,
        captured_at,
        atm_points,
        term_structure,
        skew_profiles,
        iv_rank,
        liquidity_asymmetry,
        content_digest(payload),
    )


class TermStructureChangeDirection(StrEnum):
    STEEPENED = "STEEPENED"
    FLATTENED = "FLATTENED"
    INVERTED_SHIFT = "INVERTED_SHIFT"
    UNCHANGED = "UNCHANGED"
    INSUFFICIENT_PRIOR_SNAPSHOTS = "INSUFFICIENT_PRIOR_SNAPSHOTS"


@dataclass(frozen=True, slots=True)
class TermStructureChange:
    """Temporal term-structure movement -- computed only from two genuinely
    retained surfaces of the identical underlying, never inferred from one
    snapshot (section 13)."""

    underlying_canonical_security_id: str
    previous_captured_at: datetime | None
    current_captured_at: datetime
    previous_near_far_spread: Decimal | None
    current_near_far_spread: Decimal | None
    spread_delta: Decimal | None
    direction: TermStructureChangeDirection


_TERM_STRUCTURE_FLAT_BAND = Decimal("0.005")


def compare_term_structures(
    *,
    underlying_canonical_security_id: str,
    previous: VolatilitySurfaceProfile | None,
    current: VolatilitySurfaceProfile,
) -> TermStructureChange:
    previous_spread = previous.term_structure.near_far_spread if previous is not None else None
    current_spread = current.term_structure.near_far_spread
    if previous is None or previous_spread is None or current_spread is None:
        return TermStructureChange(
            underlying_canonical_security_id,
            previous.captured_at if previous is not None else None,
            current.captured_at,
            previous_spread,
            current_spread,
            None,
            TermStructureChangeDirection.INSUFFICIENT_PRIOR_SNAPSHOTS,
        )
    delta = _exact(current_spread - previous_spread)
    if (
        (previous_spread > 0) != (current_spread > 0)
        and previous_spread != 0
        and current_spread != 0
    ):
        direction = TermStructureChangeDirection.INVERTED_SHIFT
    elif abs(delta) <= _TERM_STRUCTURE_FLAT_BAND:
        direction = TermStructureChangeDirection.UNCHANGED
    elif abs(current_spread) > abs(previous_spread):
        direction = TermStructureChangeDirection.STEEPENED
    else:
        direction = TermStructureChangeDirection.FLATTENED
    return TermStructureChange(
        underlying_canonical_security_id,
        previous.captured_at,
        current.captured_at,
        previous_spread,
        current_spread,
        delta,
        direction,
    )


# --------------------------------------------------------------------------
# Section 11: expected move
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedMoveEstimate:
    underlying_canonical_security_id: str
    expiration: str
    dte: int
    method: str
    dollar_move: Decimal | None
    percent_move: Decimal | None
    inputs_used: tuple[str, ...]
    status: str
    methodology_note: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        underlying_canonical_security_id: str,
        expiration: str,
        dte: int,
        method: str,
        dollar_move: Decimal | None,
        percent_move: Decimal | None,
        inputs_used: tuple[str, ...],
        status: str,
        methodology_note: str,
    ) -> ExpectedMoveEstimate:
        payload = (
            EXPECTED_MOVE_ESTIMATE_V1,
            underlying_canonical_security_id,
            expiration,
            dte,
            method,
            dollar_move,
            percent_move,
            tuple(sorted(inputs_used)),
            status,
            methodology_note,
        )
        return cls(
            underlying_canonical_security_id,
            expiration,
            dte,
            method,
            dollar_move,
            percent_move,
            tuple(sorted(inputs_used)),
            status,
            methodology_note,
            content_digest(payload),
        )


def expected_move_from_straddle(
    *,
    underlying_canonical_security_id: str,
    underlying_price: Decimal,
    expiration: str,
    dte: int,
    observations: tuple[OptionQuoteObservation, ...],
) -> ExpectedMoveEstimate:
    """The market-implied straddle breakeven move: what a long ATM
    straddle would need the underlying to move by expiration, priced
    conservatively at the ask side (a debit entry) per section 7."""

    members = [item for item in observations if item.contract.expiration == expiration]
    calls = [
        item
        for item in members
        if item.contract.option_type is OptionType.CALL and item.estimated_buy_price is not None
    ]
    puts = [
        item
        for item in members
        if item.contract.option_type is OptionType.PUT and item.estimated_buy_price is not None
    ]
    if not calls or not puts:
        return ExpectedMoveEstimate.create(
            underlying_canonical_security_id=underlying_canonical_security_id,
            expiration=expiration,
            dte=dte,
            method="ATM_STRADDLE_V1",
            dollar_move=None,
            percent_move=None,
            inputs_used=(),
            status="INSUFFICIENT_DATA",
            methodology_note=(
                "Sum of the nearest-the-money call and put ask prices (debit-side, "
                "never executable at midpoint); requires both a quotable call and "
                "put ask at the identical expiration."
            ),
        )
    atm_call = min(calls, key=lambda item: abs(underlying_price - item.contract.strike))
    atm_put = min(puts, key=lambda item: abs(underlying_price - item.contract.strike))
    assert atm_call.estimated_buy_price is not None and atm_put.estimated_buy_price is not None
    dollar_move = _exact(atm_call.estimated_buy_price + atm_put.estimated_buy_price)
    percent_move = _exact(dollar_move / underlying_price)
    return ExpectedMoveEstimate.create(
        underlying_canonical_security_id=underlying_canonical_security_id,
        expiration=expiration,
        dte=dte,
        method="ATM_STRADDLE_V1",
        dollar_move=dollar_move,
        percent_move=percent_move,
        inputs_used=(
            atm_call.contract.canonical_contract_id,
            atm_put.contract.canonical_contract_id,
        ),
        status="COMPUTED",
        methodology_note=(
            "Sum of the nearest-the-money call and put ask prices (debit-side, "
            "never executable at midpoint); requires both a quotable call and put "
            "ask at the identical expiration."
        ),
    )


def expected_move_from_iv(
    *,
    underlying_canonical_security_id: str,
    underlying_price: Decimal,
    expiration: str,
    dte: int,
    atm_iv: Decimal | None,
) -> ExpectedMoveEstimate:
    """A one-standard-deviation-equivalent move under the lognormal
    assumption ATM implied volatility encodes. This is a modeled estimate
    under that assumption, not a guaranteed or empirically-validated
    confidence interval."""

    if atm_iv is None or dte <= 0:
        return ExpectedMoveEstimate.create(
            underlying_canonical_security_id=underlying_canonical_security_id,
            expiration=expiration,
            dte=dte,
            method="IV_DERIVED_V1",
            dollar_move=None,
            percent_move=None,
            inputs_used=(),
            status="INSUFFICIENT_DATA",
            methodology_note=(
                "underlying_price * atm_iv * sqrt(dte / 365): a one-standard-"
                "deviation-equivalent move under the lognormal assumption implied "
                "volatility encodes, not an empirically validated confidence "
                "interval."
            ),
        )
    t_years = float(Decimal(dte) / Decimal(365))
    move = float(underlying_price) * float(atm_iv) * sqrt(t_years)
    dollar_move = _exact(Decimal(str(round(move, 10))))
    percent_move = _exact(dollar_move / underlying_price)
    return ExpectedMoveEstimate.create(
        underlying_canonical_security_id=underlying_canonical_security_id,
        expiration=expiration,
        dte=dte,
        method="IV_DERIVED_V1",
        dollar_move=dollar_move,
        percent_move=percent_move,
        inputs_used=("ATM_IV",),
        status="COMPUTED",
        methodology_note=(
            "underlying_price * atm_iv * sqrt(dte / 365): a one-standard-deviation-"
            "equivalent move under the lognormal assumption implied volatility "
            "encodes, not an empirically validated confidence interval."
        ),
    )


# --------------------------------------------------------------------------
# Section 14: chain liquidity profile
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainLiquidityProfile:
    underlying_canonical_security_id: str
    captured_at: datetime
    contracts_considered: int
    contracts_quoted_ok: int
    avg_spread_width: Decimal | None
    avg_relative_spread: Decimal | None
    avg_displayed_size: Decimal | None
    zero_bid_rate: Decimal
    one_sided_rate: Decimal
    stale_rate: Decimal
    contract_coverage_ratio: Decimal
    liquidity_asymmetry: CallPutLiquidityAsymmetry
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        underlying_canonical_security_id: str,
        captured_at: datetime,
        observations: tuple[OptionQuoteObservation, ...],
        liquidity_asymmetry: CallPutLiquidityAsymmetry,
    ) -> ChainLiquidityProfile:
        considered = len(observations)
        if considered == 0:
            return cls(
                underlying_canonical_security_id,
                captured_at,
                0,
                0,
                None,
                None,
                None,
                Decimal(0),
                Decimal(0),
                Decimal(0),
                Decimal(0),
                liquidity_asymmetry,
                content_digest(
                    (CHAIN_LIQUIDITY_PROFILE_V1, underlying_canonical_security_id, captured_at, 0)
                ),
            )
        quoted_ok = sum(
            1 for item in observations if item.status is QuoteObservationStatus.OBSERVED
        )
        widths = [
            item.ask - item.bid
            for item in observations
            if item.bid is not None and item.ask is not None
        ]
        avg_width = _exact(sum(widths, Decimal(0)) / Decimal(len(widths))) if widths else None
        spreads = [
            s
            for item in observations
            if (s := _relative_spread(item.bid, item.ask, item.midpoint)) is not None
        ]
        avg_relative = _exact(sum(spreads, Decimal(0)) / Decimal(len(spreads))) if spreads else None
        sizes = [
            (item.bid_size or Decimal(0)) + (item.ask_size or Decimal(0))
            for item in observations
            if item.bid_size is not None or item.ask_size is not None
        ]
        avg_size = _exact(sum(sizes, Decimal(0)) / Decimal(len(sizes)) / 2) if sizes else None
        zero_bid = sum(1 for item in observations if item.bid is None or item.bid == 0)
        one_sided = sum(
            1 for item in observations if item.status is QuoteObservationStatus.ONE_SIDED
        )
        stale = sum(1 for item in observations if item.status is QuoteObservationStatus.STALE)
        payload = (
            CHAIN_LIQUIDITY_PROFILE_V1,
            underlying_canonical_security_id,
            captured_at,
            considered,
            quoted_ok,
            avg_width,
            avg_relative,
            avg_size,
            zero_bid,
            one_sided,
            stale,
        )
        return cls(
            underlying_canonical_security_id,
            captured_at,
            considered,
            quoted_ok,
            avg_width,
            avg_relative,
            avg_size,
            _exact(Decimal(zero_bid) / Decimal(considered)),
            _exact(Decimal(one_sided) / Decimal(considered)),
            _exact(Decimal(stale) / Decimal(considered)),
            _exact(Decimal(quoted_ok) / Decimal(considered)),
            liquidity_asymmetry,
            content_digest(payload),
        )

    @property
    def coarse_liquidity_state(self) -> str:
        if self.contracts_considered == 0:
            return "NO_EVIDENCE"
        if self.contract_coverage_ratio >= Decimal("0.7") and (
            self.avg_relative_spread is None or self.avg_relative_spread <= Decimal("0.15")
        ):
            return "ACCEPTABLE"
        if self.contract_coverage_ratio >= Decimal("0.3"):
            return "THIN"
        return "UNRELIABLE"


# --------------------------------------------------------------------------
# Section 17: implied-versus-realized volatility
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IvVsRealizedVolatility:
    underlying_canonical_security_id: str
    horizon_sessions: int
    realized_volatility_annualized: Decimal | None
    implied_volatility_reference: Decimal | None
    annualization_basis: int
    premium_ratio: Decimal | None
    premium_state: str
    methodology: str
    limitations: tuple[str, ...]
    status: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        underlying_canonical_security_id: str,
        horizon_sessions: int,
        realized_volatility_annualized: Decimal | None,
        implied_volatility_reference: Decimal | None,
        realized_volatility_status: str,
    ) -> IvVsRealizedVolatility:
        limitations = [
            "REALIZED_VOLATILITY_IS_BACKWARD_LOOKING_AND_IMPLIED_IS_FORWARD_LOOKING",
            "PREMIUM_OR_DISCOUNT_IS_NOT_AUTOMATICALLY_AN_EDGE",
            "ANNUALIZATION_ASSUMES_252_TRADING_SESSIONS_PER_YEAR",
        ]
        if realized_volatility_annualized is None or implied_volatility_reference is None:
            status = (
                "REALIZED_VOLATILITY_UNAVAILABLE"
                if realized_volatility_annualized is None
                else "IMPLIED_VOLATILITY_UNAVAILABLE"
            )
            if realized_volatility_status != "COMPUTED":
                limitations.append(realized_volatility_status)
            payload: tuple[object, ...] = (
                IV_VS_REALIZED_VOLATILITY_V1,
                underlying_canonical_security_id,
                horizon_sessions,
                None,
                None,
                TRADING_DAYS_PER_YEAR,
                None,
                "UNAVAILABLE",
                "SIMPLE_RETURN_POPULATION_STDEV_VS_NEAREST_EXPIRATION_ATM_IV_V1",
                tuple(limitations),
                status,
            )
            return cls(
                underlying_canonical_security_id,
                horizon_sessions,
                None,
                None,
                TRADING_DAYS_PER_YEAR,
                None,
                "UNAVAILABLE",
                "SIMPLE_RETURN_POPULATION_STDEV_VS_NEAREST_EXPIRATION_ATM_IV_V1",
                tuple(limitations),
                status,
                content_digest(payload),
            )
        ratio = _exact(implied_volatility_reference / realized_volatility_annualized)
        if ratio > Decimal("1.1"):
            state = "IV_PREMIUM_TO_RV"
        elif ratio < Decimal("0.9"):
            state = "IV_DISCOUNT_TO_RV"
        else:
            state = "COMPARABLE"
        payload = (
            IV_VS_REALIZED_VOLATILITY_V1,
            underlying_canonical_security_id,
            horizon_sessions,
            realized_volatility_annualized,
            implied_volatility_reference,
            TRADING_DAYS_PER_YEAR,
            ratio,
            state,
            "SIMPLE_RETURN_POPULATION_STDEV_VS_NEAREST_EXPIRATION_ATM_IV_V1",
            tuple(limitations),
            "COMPUTED",
        )
        return cls(
            underlying_canonical_security_id,
            horizon_sessions,
            realized_volatility_annualized,
            implied_volatility_reference,
            TRADING_DAYS_PER_YEAR,
            ratio,
            state,
            "SIMPLE_RETURN_POPULATION_STDEV_VS_NEAREST_EXPIRATION_ATM_IV_V1",
            tuple(limitations),
            "COMPUTED",
            content_digest(payload),
        )


def annualize_realized_volatility(period_volatility: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return _exact(period_volatility * (+Decimal(TRADING_DAYS_PER_YEAR).sqrt()))


# --------------------------------------------------------------------------
# Section 23: arbitrage-obvious anomaly flags (quality control, never
# incorporated into pricing or acted on)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArbitrageAnomalyFlag:
    underlying_canonical_security_id: str
    anomaly_type: str
    canonical_contract_ids: tuple[str, ...]
    magnitude: Decimal
    description: str


def detect_arbitrage_anomalies(
    underlying_canonical_security_id: str,
    observations: tuple[OptionQuoteObservation, ...],
) -> tuple[ArbitrageAnomalyFlag, ...]:
    flags: list[ArbitrageAnomalyFlag] = []
    by_expiration: dict[str, list[OptionQuoteObservation]] = {}
    for item in observations:
        by_expiration.setdefault(item.contract.expiration, []).append(item)
    for expiration, members in sorted(by_expiration.items()):
        calls = sorted(
            (
                item
                for item in members
                if item.contract.option_type is OptionType.CALL and item.midpoint
            ),
            key=lambda item: item.contract.strike,
        )
        puts = sorted(
            (
                item
                for item in members
                if item.contract.option_type is OptionType.PUT and item.midpoint
            ),
            key=lambda item: item.contract.strike,
        )
        for pool, direction in ((calls, "CALL"), (puts, "PUT")):
            for earlier, later in pairwise(pool):
                assert earlier.midpoint is not None and later.midpoint is not None
                violated = (
                    later.midpoint > earlier.midpoint
                    if direction == "CALL"
                    else later.midpoint < earlier.midpoint
                )
                if violated:
                    flags.append(
                        ArbitrageAnomalyFlag(
                            underlying_canonical_security_id,
                            f"STRIKE_MONOTONICITY_VIOLATION_{direction}",
                            (
                                earlier.contract.canonical_contract_id,
                                later.contract.canonical_contract_id,
                            ),
                            _exact(abs(later.midpoint - earlier.midpoint)),
                            (
                                f"{direction} midpoint at strike {later.contract.strike} is not "
                                "monotonic relative to strike "
                                f"{earlier.contract.strike} at expiration {expiration}"
                            ),
                        )
                    )
        by_strike: dict[
            Decimal, tuple[OptionQuoteObservation | None, OptionQuoteObservation | None]
        ] = {}
        for item in calls:
            by_strike[item.contract.strike] = (
                item,
                by_strike.get(item.contract.strike, (None, None))[1],
            )
        for item in puts:
            by_strike[item.contract.strike] = (
                by_strike.get(item.contract.strike, (None, None))[0],
                item,
            )
        for strike, (call, put) in sorted(by_strike.items()):
            if call is None or put is None or call.midpoint is None or put.midpoint is None:
                continue
            spot = call.underlying_reference_price
            dte = (date.fromisoformat(expiration) - call.quote_timestamp.date()).days
            if dte <= 0:
                continue
            t = Decimal(dte) / Decimal(365)
            with localcontext() as context:
                context.prec = 28
                discount = Decimal(str(exp(-float(RISK_FREE_RATE_DEFAULT) * float(t))))
            parity_rhs = _exact(spot - strike * discount)
            parity_lhs = _exact(call.midpoint - put.midpoint)
            gap = abs(parity_lhs - parity_rhs)
            tolerance = max(
                (call.spread_width or Decimal("0.05")) + (put.spread_width or Decimal("0.05")),
                Decimal("0.10"),
            )
            if gap > tolerance:
                flags.append(
                    ArbitrageAnomalyFlag(
                        underlying_canonical_security_id,
                        "PUT_CALL_PARITY_DEVIATION",
                        (call.contract.canonical_contract_id, put.contract.canonical_contract_id),
                        _exact(gap),
                        (
                            f"call-put midpoint spread {parity_lhs} deviates from the "
                            f"forward-parity estimate {parity_rhs} by more than the combined "
                            f"quoted spread tolerance at strike {strike}, expiration {expiration}"
                        ),
                    )
                )
    return tuple(flags)


# --------------------------------------------------------------------------
# IV/Greeks orchestration over a whole retained chain
# --------------------------------------------------------------------------


def build_iv_and_greeks_for_chain(
    *,
    observations: tuple[OptionQuoteObservation, ...],
    as_of: date,
    risk_free_rate: Decimal = RISK_FREE_RATE_DEFAULT,
    risk_free_rate_source: str = RISK_FREE_RATE_SOURCE_DEFAULT,
    dividend_yield: Decimal = DIVIDEND_YIELD_DEFAULT,
    dividend_yield_source: str = DIVIDEND_YIELD_SOURCE_DEFAULT,
) -> tuple[tuple[ApexImpliedVolatilityEstimate, ...], tuple[ApexGreeksEstimate, ...]]:
    """One IV solve attempt (from the descriptive midpoint, never a
    fabricated price) and one paired Greeks estimate per quotable
    observation. A failed solve still retains its full assumption lineage;
    the paired Greeks estimate then falls back to provider IV, and finally
    to an explicit ``UNAVAILABLE`` source with every Greek left ``None``."""

    iv_estimates: list[ApexImpliedVolatilityEstimate] = []
    greeks_estimates: list[ApexGreeksEstimate] = []
    for observation in observations:
        if observation.midpoint is None:
            continue
        iv_estimate = ApexImpliedVolatilityEstimate.solve(
            contract=observation.contract,
            underlying_price=observation.underlying_reference_price,
            option_price=observation.midpoint,
            option_price_source="MIDPOINT",
            as_of=as_of,
            risk_free_rate=risk_free_rate,
            risk_free_rate_source=risk_free_rate_source,
            dividend_yield=dividend_yield,
            dividend_yield_source=dividend_yield_source,
        )
        iv_estimates.append(iv_estimate)
        volatility: Decimal | None
        source: ValueSource
        if iv_estimate.implied_volatility is not None:
            volatility, source = iv_estimate.implied_volatility, ValueSource.APEX_CALCULATED
        elif observation.provider_implied_volatility is not None:
            volatility, source = observation.provider_implied_volatility, ValueSource.PROVIDER
        else:
            volatility, source = None, ValueSource.UNAVAILABLE
        greeks_estimates.append(
            ApexGreeksEstimate.compute(
                contract=observation.contract,
                underlying_price=observation.underlying_reference_price,
                as_of=as_of,
                volatility=volatility,
                iv_source=source,
                risk_free_rate=risk_free_rate,
                dividend_yield=dividend_yield,
            )
        )
    return tuple(iv_estimates), tuple(greeks_estimates)


# --------------------------------------------------------------------------
# Section 15: the immutable current options-intelligence snapshot
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionsChainSnapshot:
    """One immutable binding of every genuine evidence family this module
    produces for one underlying at one capture time. Deterministic replay
    from identical quote evidence (same plan, underlying, ``captured_at``,
    and observation set) reproduces the identical ``snapshot_id`` -- so a
    repeated identical capture is a durable no-op, never a duplicate row.
    """

    snapshot_id: str
    plan_id: str
    run_id: str
    underlying_canonical_security_id: str
    underlying_symbol: str
    underlying_reference_price: Decimal
    underlying_price_source: str
    captured_at: datetime
    contracts_considered: int
    contracts_quoted: int
    contracts_excluded: int
    exclusion_reason_counts: tuple[tuple[str, int], ...]
    observation_ids: tuple[str, ...]
    observation_evidence_hash: str
    descriptive_profiles: tuple[OptionDescriptiveProfile, ...]
    iv_estimates: tuple[ApexImpliedVolatilityEstimate, ...]
    greeks_estimates: tuple[ApexGreeksEstimate, ...]
    volatility_surface: VolatilitySurfaceProfile
    expected_moves: tuple[ExpectedMoveEstimate, ...]
    liquidity_profile: ChainLiquidityProfile
    iv_vs_realized: IvVsRealizedVolatility | None
    arbitrage_flags: tuple[ArbitrageAnomalyFlag, ...]
    data_quality_status: ChainCompletionStatus
    data_quality_notes: tuple[str, ...]
    historical_options_pricing_note: str
    provider: str
    source_receipt_ids: tuple[str, ...]
    authority: str
    canonical_hash: str


def derive_options_chain_snapshot_id(
    *,
    underlying_canonical_security_id: str,
    captured_at: datetime,
    observations: tuple[OptionQuoteObservation, ...],
) -> str:
    """The snapshot's identity, computable from its observation evidence
    alone -- mirroring ``OptionChainCaptureRun.derive_run_id``'s identical
    "know the id before the full object exists" pattern (section 19/24).
    Lets a caller bind a not-yet-built snapshot's id into its own capture
    run's summary (whose ``snapshot_ids`` a persisted
    ``options_chain_capture_run`` row must carry, and whose own row must
    exist before ``options_chain_snapshot.run_id`` -- a foreign key --
    can reference it) before persisting either row, without recomputing
    the expensive IV/Greeks/surface pipeline twice. Must stay in exact
    lockstep with the identity ``create_options_chain_snapshot`` derives
    internally; both sort observations identically and hash only the
    observation evidence, never the derived IV/Greeks/surface content."""

    _aware(captured_at, "SNAPSHOT_CAPTURED_AT")
    ordered = tuple(sorted(observations, key=lambda item: item.contract.canonical_contract_id))
    evidence_hash = content_digest(tuple(item.canonical_hash for item in ordered))
    return deterministic_id(
        "options_chain_snapshot",
        (OPTIONS_CHAIN_SNAPSHOT_V1, underlying_canonical_security_id, captured_at, evidence_hash),
    )


def create_options_chain_snapshot(
    *,
    plan_id: str,
    run_id: str,
    underlying_canonical_security_id: str,
    underlying_symbol: str,
    underlying_reference_price: Decimal,
    underlying_price_source: str,
    captured_at: datetime,
    observations: tuple[OptionQuoteObservation, ...],
    as_of: date,
    provider: str,
    source_receipt_ids: tuple[str, ...],
    prior_atm_iv_history: tuple[Decimal, ...] = (),
    realized_volatility_annualized: Decimal | None = None,
    realized_volatility_horizon_sessions: int = REALIZED_VOLATILITY_WINDOW_SESSIONS,
    realized_volatility_status: str = "REALIZED_VOLATILITY_UNAVAILABLE",
) -> OptionsChainSnapshot:
    _aware(captured_at, "SNAPSHOT_CAPTURED_AT")
    for observation in observations:
        if (
            observation.contract.underlying_canonical_security_id
            != underlying_canonical_security_id
        ):
            raise OptionsQuoteIntelligenceError("SNAPSHOT_UNDERLYING_MISMATCH")
    ordered = tuple(sorted(observations, key=lambda item: item.contract.canonical_contract_id))
    seen: set[str] = set()
    for item in ordered:
        if item.contract.canonical_contract_id in seen:
            raise OptionsQuoteIntelligenceError("SNAPSHOT_DUPLICATE_CONTRACT")
        seen.add(item.contract.canonical_contract_id)

    considered = len(ordered)
    quoted = sum(1 for item in ordered if item.status is QuoteObservationStatus.OBSERVED)
    reason_counts: dict[str, int] = {}
    for item in ordered:
        for reason in item.exclusion_reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    descriptive_profiles = tuple(
        OptionDescriptiveProfile.create(
            contract=item.contract, underlying_price=underlying_reference_price, as_of=as_of
        )
        for item in ordered
    )
    iv_estimates, greeks_estimates = build_iv_and_greeks_for_chain(
        observations=ordered, as_of=as_of
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
    volatility_surface = build_volatility_surface(
        underlying_canonical_security_id=underlying_canonical_security_id,
        captured_at=captured_at,
        observations=ordered,
        ivs_by_contract=ivs_by_contract,
        deltas_by_contract=deltas_by_contract,
        prior_atm_iv_history=prior_atm_iv_history,
    )
    expirations = sorted({item.contract.expiration for item in ordered})
    expected_moves: list[ExpectedMoveEstimate] = []
    for expiration in expirations:
        dte = (date.fromisoformat(expiration) - as_of).days
        expected_moves.append(
            expected_move_from_straddle(
                underlying_canonical_security_id=underlying_canonical_security_id,
                underlying_price=underlying_reference_price,
                expiration=expiration,
                dte=dte,
                observations=ordered,
            )
        )
        point = next((p for p in volatility_surface.atm_points if p.expiration == expiration), None)
        expected_moves.append(
            expected_move_from_iv(
                underlying_canonical_security_id=underlying_canonical_security_id,
                underlying_price=underlying_reference_price,
                expiration=expiration,
                dte=dte,
                atm_iv=point.atm_iv if point is not None else None,
            )
        )
    liquidity_profile = ChainLiquidityProfile.create(
        underlying_canonical_security_id=underlying_canonical_security_id,
        captured_at=captured_at,
        observations=ordered,
        liquidity_asymmetry=volatility_surface.liquidity_asymmetry,
    )
    nearest_atm_iv = (
        volatility_surface.atm_points[0].atm_iv if volatility_surface.atm_points else None
    )
    iv_vs_realized = IvVsRealizedVolatility.create(
        underlying_canonical_security_id=underlying_canonical_security_id,
        horizon_sessions=realized_volatility_horizon_sessions,
        realized_volatility_annualized=realized_volatility_annualized,
        implied_volatility_reference=nearest_atm_iv,
        realized_volatility_status=realized_volatility_status,
    )
    arbitrage_flags = detect_arbitrage_anomalies(underlying_canonical_security_id, ordered)

    if considered == 0:
        quality_status = ChainCompletionStatus.EMPTY
    elif quoted == considered:
        quality_status = ChainCompletionStatus.COMPLETE
    else:
        quality_status = ChainCompletionStatus.PARTIAL
    quality_notes = tuple(
        sorted(
            {
                HISTORICAL_OPTIONS_PRICING_UNAVAILABLE,
                *(
                    (IV_HISTORY_INSUFFICIENT,)
                    if volatility_surface.iv_rank.status is IvRankStatus.INSUFFICIENT_HISTORY
                    else ()
                ),
            }
        )
    )
    observation_ids = tuple(item.observation_id for item in ordered)
    evidence_hash = content_digest(tuple(item.canonical_hash for item in ordered))
    # Identity is deliberately narrower than full content: only the
    # underlying, the declared capture time, and the observed-evidence
    # hash determine ``snapshot_id`` (section 15/19 -- "deterministic
    # replay from identical quote evidence" and "repeated identical
    # evidence does not duplicate"). Every other field, including
    # ``volatility_surface`` (whose IV-rank genuinely depends on how many
    # prior snapshots happen to be retained at computation time), is
    # retained content but never part of identity -- so replaying the
    # identical evidence always resolves to the identical row, and that
    # first-computed row's history-dependent features are never silently
    # rewritten by a later replay that sees more accumulated history.
    snapshot_id = derive_options_chain_snapshot_id(
        underlying_canonical_security_id=underlying_canonical_security_id,
        captured_at=captured_at,
        observations=ordered,
    )
    payload = (
        OPTIONS_CHAIN_SNAPSHOT_V1,
        plan_id,
        run_id,
        underlying_canonical_security_id,
        underlying_symbol,
        underlying_reference_price,
        underlying_price_source,
        captured_at,
        considered,
        quoted,
        considered - quoted,
        tuple(sorted(reason_counts.items())),
        observation_ids,
        evidence_hash,
        tuple(item.canonical_hash for item in descriptive_profiles),
        tuple(item.canonical_hash for item in iv_estimates),
        tuple(item.canonical_hash for item in greeks_estimates),
        volatility_surface.canonical_hash,
        tuple(item.canonical_hash for item in expected_moves),
        liquidity_profile.canonical_hash,
        iv_vs_realized.canonical_hash,
        tuple(item.canonical_contract_ids for item in arbitrage_flags),
        quality_status,
        quality_notes,
        provider,
        tuple(sorted(source_receipt_ids)),
    )
    return OptionsChainSnapshot(
        snapshot_id,
        plan_id,
        run_id,
        underlying_canonical_security_id,
        underlying_symbol,
        underlying_reference_price,
        underlying_price_source,
        captured_at,
        considered,
        quoted,
        considered - quoted,
        tuple(sorted(reason_counts.items())),
        observation_ids,
        evidence_hash,
        descriptive_profiles,
        iv_estimates,
        greeks_estimates,
        volatility_surface,
        tuple(expected_moves),
        liquidity_profile,
        iv_vs_realized,
        arbitrage_flags,
        quality_status,
        quality_notes,
        HISTORICAL_OPTIONS_PRICING_UNAVAILABLE,
        provider,
        tuple(sorted(source_receipt_ids)),
        "NONE",
        content_digest(payload),
    )


# --------------------------------------------------------------------------
# Section 16: cross-market research seam -- features only, no hypothesis
# generation happens here
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionsVolatilityResearchFeatures:
    feature_id: str
    underlying_canonical_security_id: str
    underlying_symbol: str
    as_of: str
    snapshot_id: str
    atm_iv_nearest_expiration: Decimal | None
    skew_target_delta_nearest_expiration: Decimal | None
    term_structure_near_far_spread: Decimal | None
    expected_move_percent_straddle: Decimal | None
    expected_move_percent_iv_derived: Decimal | None
    iv_vs_realized_premium_ratio: Decimal | None
    liquidity_state: str
    data_quality_status: ChainCompletionStatus
    canonical_hash: str

    @classmethod
    def from_snapshot(cls, snapshot: OptionsChainSnapshot) -> OptionsVolatilityResearchFeatures:
        nearest_atm = (
            snapshot.volatility_surface.atm_points[0]
            if snapshot.volatility_surface.atm_points
            else None
        )
        nearest_skew = (
            snapshot.volatility_surface.skew_by_expiration[0]
            if snapshot.volatility_surface.skew_by_expiration
            else None
        )
        straddle = next(
            (
                m
                for m in snapshot.expected_moves
                if m.method == "ATM_STRADDLE_V1" and m.status == "COMPUTED"
            ),
            None,
        )
        iv_derived = next(
            (
                m
                for m in snapshot.expected_moves
                if m.method == "IV_DERIVED_V1" and m.status == "COMPUTED"
            ),
            None,
        )
        as_of = snapshot.captured_at.date().isoformat()
        payload = (
            OPTIONS_VOLATILITY_RESEARCH_FEATURE_V1,
            snapshot.underlying_canonical_security_id,
            snapshot.underlying_symbol,
            as_of,
            snapshot.snapshot_id,
            nearest_atm.atm_iv if nearest_atm else None,
            nearest_skew.skew_target_delta if nearest_skew else None,
            snapshot.volatility_surface.term_structure.near_far_spread,
            straddle.percent_move if straddle else None,
            iv_derived.percent_move if iv_derived else None,
            snapshot.iv_vs_realized.premium_ratio if snapshot.iv_vs_realized else None,
            snapshot.liquidity_profile.coarse_liquidity_state,
            snapshot.data_quality_status,
        )
        return cls(
            deterministic_id("options_volatility_research_feature", payload),
            snapshot.underlying_canonical_security_id,
            snapshot.underlying_symbol,
            as_of,
            snapshot.snapshot_id,
            nearest_atm.atm_iv if nearest_atm else None,
            nearest_skew.skew_target_delta if nearest_skew else None,
            snapshot.volatility_surface.term_structure.near_far_spread,
            straddle.percent_move if straddle else None,
            iv_derived.percent_move if iv_derived else None,
            snapshot.iv_vs_realized.premium_ratio if snapshot.iv_vs_realized else None,
            snapshot.liquidity_profile.coarse_liquidity_state,
            snapshot.data_quality_status,
            content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 19 / 24: one durable row per bounded capture invocation -- the
# forward-accumulation seam a later recurring operator command reuses
# unmodified.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionChainCaptureRun:
    run_id: str
    plan_id: str
    requested_at: datetime
    completed_at: datetime
    status: CaptureRunStatus
    underlyings_requested: int
    underlyings_captured: int
    provider_request_count: int
    payload_bytes: int
    contracts_considered: int
    contracts_quoted: int
    runtime_seconds: Decimal
    snapshot_ids: tuple[str, ...]
    reason: str | None
    canonical_hash: str

    @staticmethod
    def derive_run_id(*, plan_id: str, requested_at: datetime) -> str:
        """The run's identity is fixed by ``(plan_id, requested_at)`` alone
        -- computable *before* any underlying is captured -- so the caller
        can bind every snapshot captured under this run to the identical
        ``run_id`` it will later pass to ``create()``, without a circular
        dependency on this run's own eventual ``snapshot_ids``."""

        _aware(requested_at, "RUN_REQUESTED_AT")
        return deterministic_id(
            "options_chain_capture_run", (OPTIONS_CHAIN_CAPTURE_RUN_V1, plan_id, requested_at)
        )

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        requested_at: datetime,
        completed_at: datetime,
        status: CaptureRunStatus,
        underlyings_requested: int,
        underlyings_captured: int,
        provider_request_count: int,
        payload_bytes: int,
        contracts_considered: int,
        contracts_quoted: int,
        snapshot_ids: tuple[str, ...],
        reason: str | None,
    ) -> OptionChainCaptureRun:
        _aware(requested_at, "RUN_REQUESTED_AT")
        _aware(completed_at, "RUN_COMPLETED_AT")
        if completed_at < requested_at:
            raise OptionsQuoteIntelligenceError("RUN_COMPLETED_BEFORE_REQUESTED")
        if provider_request_count < 0 or payload_bytes < 0:
            raise OptionsQuoteIntelligenceError("RUN_COUNTERS_MUST_BE_NON_NEGATIVE")
        if status is CaptureRunStatus.BLOCKED_CREDENTIALS_ABSENT and provider_request_count != 0:
            raise OptionsQuoteIntelligenceError("RUN_CREDENTIALS_ABSENT_MUST_MAKE_NO_REQUEST")
        runtime = _exact(Decimal(str((completed_at - requested_at).total_seconds())))
        run_id = cls.derive_run_id(plan_id=plan_id, requested_at=requested_at)
        payload = (
            OPTIONS_CHAIN_CAPTURE_RUN_V1,
            plan_id,
            requested_at,
            completed_at,
            status,
            underlyings_requested,
            underlyings_captured,
            provider_request_count,
            payload_bytes,
            contracts_considered,
            contracts_quoted,
            runtime,
            tuple(sorted(snapshot_ids)),
            reason,
        )
        return cls(
            run_id,
            plan_id,
            requested_at,
            completed_at,
            status,
            underlyings_requested,
            underlyings_captured,
            provider_request_count,
            payload_bytes,
            contracts_considered,
            contracts_quoted,
            runtime,
            tuple(sorted(snapshot_ids)),
            reason,
            content_digest(payload),
        )


# ==========================================================================
# Genuine Options Quote-Chain Bring-Up and Volatility Validation v1
# additions: quote-quality slicing (underlying / expiration-bucket /
# call-put), provider-versus-Apex IV/Greeks comparison, Greeks sanity
# checks, bounded cross-sectional summaries, and risk-free-rate/dividend-
# assumption sensitivity. Every function below is a pure, deterministic
# reduction over already-retained evidence (or, for assumption
# sensitivity, a bounded re-solve of an already-captured midpoint under
# one alternate declared assumption) -- no provider request, no
# persistence, and never an Opportunity ranking or trade recommendation.
# None of these are content-addressed/immutable evidence in their own
# right (they carry no ``canonical_hash``): each is fully and
# deterministically reproducible from the retained observations/snapshots
# it summarizes, so persisting a fresh copy on every report request would
# be schema growth for a computed summary that already lives faithfully
# in existing rows.
# ==========================================================================

# Declared before any comparison result is reviewed (never loosened or
# tightened to make a given underlying's agreement look better or worse).
IV_COMPARISON_CLOSE_AGREEMENT_THRESHOLD = Decimal("0.02")
IV_COMPARISON_MODERATE_DISAGREEMENT_THRESHOLD = Decimal("0.10")
GREEKS_VALIDATION_TOLERANCE = Decimal("0.0005")
GREEKS_CALL_PUT_DELTA_PARITY_TOLERANCE = Decimal("0.05")
# Representative alternate assumptions for the bounded sensitivity checks
# (sections 10/11) -- diagnostic only, never a second "official" input.
SENSITIVITY_ALTERNATE_RISK_FREE_RATE = Decimal("0.03")
SENSITIVITY_ALTERNATE_RISK_FREE_RATE_SOURCE = "SENSITIVITY_ALTERNATE_RATE_V1"
SENSITIVITY_ALTERNATE_DIVIDEND_YIELD = Decimal("0.015")
SENSITIVITY_ALTERNATE_DIVIDEND_YIELD_SOURCE = "SENSITIVITY_ALTERNATE_DIVIDEND_V1"


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    middle = n // 2
    if n % 2:
        return ordered[middle]
    return _exact((ordered[middle - 1] + ordered[middle]) / 2)


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return _exact(sum(values, Decimal(0)) / Decimal(len(values)))


# --------------------------------------------------------------------------
# Quote-quality slicing (milestone section 6): by underlying, expiration
# bucket, and call/put -- reported separately because staleness and
# sidedness are independent quality axes; ``QuoteObservationStatus`` alone
# conflates them (a genuinely two-sided quote is still classified ``STALE``
# once past the freshness threshold, section 5).
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuoteQualitySlice:
    dimension: str
    key: str
    contracts_considered: int
    two_sided_count: int
    one_sided_count: int
    zero_bid_count: int
    crossed_count: int
    stale_count: int
    invalid_count: int
    unavailable_count: int
    usable_count: int
    current_quotable_count: int
    median_absolute_spread: Decimal | None
    median_relative_spread: Decimal | None
    median_displayed_size: Decimal | None

    def _rate(self, count: int) -> Decimal:
        if self.contracts_considered == 0:
            return Decimal(0)
        return _exact(Decimal(count) / Decimal(self.contracts_considered))

    @property
    def two_sided_rate(self) -> Decimal:
        return self._rate(self.two_sided_count)

    @property
    def one_sided_rate(self) -> Decimal:
        return self._rate(self.one_sided_count)

    @property
    def zero_bid_rate(self) -> Decimal:
        return self._rate(self.zero_bid_count)

    @property
    def stale_rate(self) -> Decimal:
        return self._rate(self.stale_count)

    @property
    def usable_rate(self) -> Decimal:
        """Fraction with a valid, non-crossed two-sided midpoint --
        descriptive-IV usable, regardless of freshness. Distinct from
        ``current_quotable_count`` (``is_quotable``: usable *and* fresh)."""
        return self._rate(self.usable_count)


def build_quote_quality_slice(
    observations: tuple[OptionQuoteObservation, ...], *, dimension: str, key: str
) -> QuoteQualitySlice:
    considered = len(observations)
    two_sided = sum(1 for o in observations if o.bid is not None and o.ask is not None)
    one_sided = sum(1 for o in observations if (o.bid is None) != (o.ask is None))
    zero_bid = sum(1 for o in observations if o.bid is None or o.bid == 0)
    crossed = sum(1 for o in observations if o.status is QuoteObservationStatus.CROSSED)
    stale = sum(1 for o in observations if o.status is QuoteObservationStatus.STALE)
    invalid = sum(1 for o in observations if o.status is QuoteObservationStatus.INVALID)
    unavailable = sum(1 for o in observations if o.status is QuoteObservationStatus.UNAVAILABLE)
    usable = sum(1 for o in observations if o.midpoint is not None)
    current_quotable = sum(1 for o in observations if o.is_quotable)
    spreads = [o.spread_width for o in observations if o.spread_width is not None]
    relative_spreads = [
        s for o in observations if (s := _relative_spread(o.bid, o.ask, o.midpoint)) is not None
    ]
    sizes = [
        (o.bid_size or Decimal(0)) + (o.ask_size or Decimal(0))
        for o in observations
        if o.bid_size is not None or o.ask_size is not None
    ]
    return QuoteQualitySlice(
        dimension,
        key,
        considered,
        two_sided,
        one_sided,
        zero_bid,
        crossed,
        stale,
        invalid,
        unavailable,
        usable,
        current_quotable,
        _median(spreads),
        _median(relative_spreads),
        _median(sizes),
    )


@dataclass(frozen=True, slots=True)
class CohortQuoteQualityReport:
    generated_at: datetime
    underlyings_included: tuple[str, ...]
    snapshot_ids: tuple[str, ...]
    overall: QuoteQualitySlice
    by_underlying: tuple[QuoteQualitySlice, ...]
    by_expiration_bucket: tuple[QuoteQualitySlice, ...]
    by_call_put: tuple[QuoteQualitySlice, ...]


def build_cohort_quote_quality_report(
    *,
    generated_at: datetime,
    members: tuple[tuple[str, OptionsChainSnapshot, tuple[OptionQuoteObservation, ...]], ...],
) -> CohortQuoteQualityReport:
    """One quote-quality report across every already-captured underlying's
    latest snapshot -- ``members`` is ``(symbol, snapshot, observations)``
    with ``observations`` fetched via ``observations_by_ids(snapshot.
    observation_ids)`` (bulk repository read, not a re-derivation)."""

    _aware(generated_at, "QUALITY_REPORT_GENERATED_AT")
    all_observations: list[OptionQuoteObservation] = []
    by_underlying: list[QuoteQualitySlice] = []
    profiles_by_contract: dict[str, OptionDescriptiveProfile] = {}
    for symbol, snapshot, observations in members:
        all_observations.extend(observations)
        by_underlying.append(
            build_quote_quality_slice(observations, dimension="UNDERLYING", key=symbol)
        )
        for profile in snapshot.descriptive_profiles:
            profiles_by_contract[profile.canonical_contract_id] = profile
    bucket_pool: dict[str, list[OptionQuoteObservation]] = {}
    type_pool: dict[str, list[OptionQuoteObservation]] = {}
    for observation in all_observations:
        contract_profile = profiles_by_contract.get(observation.contract.canonical_contract_id)
        bucket = (
            contract_profile.expiration_bucket.value if contract_profile is not None else "UNKNOWN"
        )
        bucket_pool.setdefault(bucket, []).append(observation)
        type_pool.setdefault(observation.contract.option_type.value, []).append(observation)
    by_bucket = tuple(
        build_quote_quality_slice(tuple(items), dimension="EXPIRATION_BUCKET", key=bucket_key)
        for bucket_key, items in sorted(bucket_pool.items())
    )
    by_type = tuple(
        build_quote_quality_slice(tuple(items), dimension="OPTION_TYPE", key=type_key)
        for type_key, items in sorted(type_pool.items())
    )
    overall = build_quote_quality_slice(tuple(all_observations), dimension="OVERALL", key="ALL")
    return CohortQuoteQualityReport(
        generated_at,
        tuple(symbol for symbol, _snapshot, _observations in members),
        tuple(snapshot.snapshot_id for _symbol, snapshot, _observations in members),
        overall,
        tuple(by_underlying),
        by_bucket,
        by_type,
    )


# --------------------------------------------------------------------------
# Provider-versus-Apex IV comparison (milestone section 12).
# --------------------------------------------------------------------------


class IvComparisonState(StrEnum):
    CLOSE_AGREEMENT = "CLOSE_AGREEMENT"
    MODERATE_DISAGREEMENT = "MODERATE_DISAGREEMENT"
    LARGE_DISAGREEMENT = "LARGE_DISAGREEMENT"
    PROVIDER_IV_UNAVAILABLE = "PROVIDER_IV_UNAVAILABLE"
    APEX_IV_UNAVAILABLE = "APEX_IV_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class IvComparisonRow:
    canonical_contract_id: str
    underlying_symbol: str
    option_type: OptionType
    dte: int | None
    expiration_bucket: ExpirationBucket | None
    moneyness_classification: MoneynessClassification | None
    quote_status: QuoteObservationStatus
    provider_iv: Decimal | None
    apex_iv: Decimal | None
    absolute_difference: Decimal | None
    relative_difference: Decimal | None
    comparison_state: IvComparisonState


def build_iv_comparison_rows(
    *,
    underlying_symbol: str,
    snapshot: OptionsChainSnapshot,
    observations: tuple[OptionQuoteObservation, ...],
) -> tuple[IvComparisonRow, ...]:
    """Never forces Apex IV to agree with provider IV -- every row is
    retained regardless of agreement, and a large disagreement is
    diagnostic evidence, not filtered out."""

    profiles = {p.canonical_contract_id: p for p in snapshot.descriptive_profiles}
    apex_iv_by_contract = {
        e.canonical_contract_id: e.implied_volatility for e in snapshot.iv_estimates
    }
    rows: list[IvComparisonRow] = []
    for observation in observations:
        contract_id = observation.contract.canonical_contract_id
        provider_iv = observation.provider_implied_volatility
        apex_iv = apex_iv_by_contract.get(contract_id)
        if provider_iv is None and apex_iv is None:
            continue
        profile = profiles.get(contract_id)
        absolute_difference: Decimal | None
        relative_difference: Decimal | None
        if provider_iv is None:
            state = IvComparisonState.PROVIDER_IV_UNAVAILABLE
            absolute_difference = relative_difference = None
        elif apex_iv is None:
            state = IvComparisonState.APEX_IV_UNAVAILABLE
            absolute_difference = relative_difference = None
        else:
            absolute_difference = _exact(abs(provider_iv - apex_iv))
            relative_difference = (
                _exact(absolute_difference / provider_iv) if provider_iv != 0 else None
            )
            if absolute_difference <= IV_COMPARISON_CLOSE_AGREEMENT_THRESHOLD:
                state = IvComparisonState.CLOSE_AGREEMENT
            elif absolute_difference <= IV_COMPARISON_MODERATE_DISAGREEMENT_THRESHOLD:
                state = IvComparisonState.MODERATE_DISAGREEMENT
            else:
                state = IvComparisonState.LARGE_DISAGREEMENT
        rows.append(
            IvComparisonRow(
                contract_id,
                underlying_symbol,
                observation.contract.option_type,
                profile.dte if profile is not None else None,
                profile.expiration_bucket if profile is not None else None,
                profile.moneyness_classification if profile is not None else None,
                observation.status,
                provider_iv,
                apex_iv,
                absolute_difference,
                relative_difference,
                state,
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class IvComparisonSegmentSummary:
    dimension: str
    key: str
    row_count: int
    both_available_count: int
    close_agreement_count: int
    moderate_disagreement_count: int
    large_disagreement_count: int
    mean_absolute_difference: Decimal | None
    median_absolute_difference: Decimal | None
    mean_relative_difference: Decimal | None


def summarize_iv_comparison(
    rows: tuple[IvComparisonRow, ...], *, dimension: str, key: str
) -> IvComparisonSegmentSummary:
    both = [row for row in rows if row.absolute_difference is not None]
    absolute_diffs = [row.absolute_difference for row in both if row.absolute_difference]
    relative_diffs = [
        row.relative_difference for row in both if row.relative_difference is not None
    ]
    return IvComparisonSegmentSummary(
        dimension,
        key,
        len(rows),
        len(both),
        sum(1 for row in both if row.comparison_state is IvComparisonState.CLOSE_AGREEMENT),
        sum(1 for row in both if row.comparison_state is IvComparisonState.MODERATE_DISAGREEMENT),
        sum(1 for row in both if row.comparison_state is IvComparisonState.LARGE_DISAGREEMENT),
        _mean(absolute_diffs),
        _median(absolute_diffs),
        _mean(relative_diffs),
    )


def segment_iv_comparison(
    rows: tuple[IvComparisonRow, ...],
) -> tuple[IvComparisonSegmentSummary, ...]:
    """Segmented by call/put, DTE-expiration bucket, moneyness, and
    quote-liquidity state (milestone section 12), plus one ``OVERALL`` row."""

    summaries = [summarize_iv_comparison(rows, dimension="OVERALL", key="ALL")]
    groupers: tuple[tuple[str, dict[str, list[IvComparisonRow]]], ...] = (
        ("OPTION_TYPE", {}),
        ("EXPIRATION_BUCKET", {}),
        ("MONEYNESS", {}),
        ("LIQUIDITY_STATUS", {}),
    )
    pools = dict(groupers)
    for row in rows:
        pools["OPTION_TYPE"].setdefault(row.option_type.value, []).append(row)
        pools["EXPIRATION_BUCKET"].setdefault(
            row.expiration_bucket.value if row.expiration_bucket is not None else "UNKNOWN", []
        ).append(row)
        pools["MONEYNESS"].setdefault(
            row.moneyness_classification.value
            if row.moneyness_classification is not None
            else "UNKNOWN",
            [],
        ).append(row)
        pools["LIQUIDITY_STATUS"].setdefault(row.quote_status.value, []).append(row)
    for dimension, pool in pools.items():
        for key, items in sorted(pool.items()):
            summaries.append(summarize_iv_comparison(tuple(items), dimension=dimension, key=key))
    return tuple(summaries)


# --------------------------------------------------------------------------
# Greeks comparison and validation (milestone sections 12/14).
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GreeksComparisonSummary:
    underlying_symbol: str
    contracts_considered: int
    both_available_count: int
    mean_absolute_delta_difference: Decimal | None
    mean_absolute_gamma_difference: Decimal | None
    mean_absolute_theta_difference: Decimal | None
    mean_absolute_vega_difference: Decimal | None


def build_greeks_comparison(
    *,
    underlying_symbol: str,
    snapshot: OptionsChainSnapshot,
    observations: tuple[OptionQuoteObservation, ...],
) -> GreeksComparisonSummary:
    apex_by_contract = {g.canonical_contract_id: g for g in snapshot.greeks_estimates}
    delta_diffs: list[Decimal] = []
    gamma_diffs: list[Decimal] = []
    theta_diffs: list[Decimal] = []
    vega_diffs: list[Decimal] = []
    both = 0
    for observation in observations:
        estimate = apex_by_contract.get(observation.contract.canonical_contract_id)
        if estimate is None:
            continue
        if observation.provider_delta is not None and estimate.delta is not None:
            both += 1
            delta_diffs.append(abs(observation.provider_delta - estimate.delta))
        if observation.provider_gamma is not None and estimate.gamma is not None:
            gamma_diffs.append(abs(observation.provider_gamma - estimate.gamma))
        if observation.provider_theta is not None and estimate.theta is not None:
            theta_diffs.append(abs(observation.provider_theta - estimate.theta))
        if observation.provider_vega is not None and estimate.vega is not None:
            vega_diffs.append(abs(observation.provider_vega - estimate.vega))
    return GreeksComparisonSummary(
        underlying_symbol,
        len(observations),
        both,
        _mean(delta_diffs),
        _mean(gamma_diffs),
        _mean(theta_diffs),
        _mean(vega_diffs),
    )


@dataclass(frozen=True, slots=True)
class GreeksValidationReport:
    underlying_symbol: str
    contracts_checked: int
    delta_range_violations: int
    gamma_negative_violations: int
    vega_negative_violations: int
    theta_positive_count: int
    theta_negative_count: int
    delta_monotonicity_violations: int
    call_put_delta_parity_violations: int
    findings: tuple[str, ...]


def validate_apex_greeks(
    *,
    underlying_symbol: str,
    snapshot: OptionsChainSnapshot,
    observations: tuple[OptionQuoteObservation, ...],
) -> GreeksValidationReport:
    """Descriptive Greeks sanity checks (section 14) over one snapshot's
    Apex-computed Greeks: delta bounds (call in ``[0, 1]``, put in
    ``[-1, 0]``, both with a small numerical-solve tolerance slack), gamma/
    vega non-negativity, theta sign distribution (reported, not asserted --
    the sign convention itself is descriptive), monotonic delta by strike
    within one expiration/side, and the same-strike call/put delta
    relationship (``call_delta - put_delta`` approximately ``1`` under the
    zero-dividend assumption). A violation is retained as diagnostic
    evidence, never silently hidden or auto-corrected."""

    contracts = {o.contract.canonical_contract_id: o.contract for o in observations}
    checked = 0
    delta_violations = 0
    gamma_violations = 0
    vega_violations = 0
    theta_positive = 0
    theta_negative = 0
    by_expiration_type: dict[tuple[str, OptionType], list[tuple[Decimal, Decimal]]] = {}
    by_expiration_strike: dict[tuple[str, Decimal], dict[OptionType, Decimal]] = {}
    for estimate in snapshot.greeks_estimates:
        contract = contracts.get(estimate.canonical_contract_id)
        if contract is None or estimate.delta is None:
            continue
        checked += 1
        if contract.option_type is OptionType.CALL:
            if not (
                -GREEKS_VALIDATION_TOLERANCE <= estimate.delta <= 1 + GREEKS_VALIDATION_TOLERANCE
            ):
                delta_violations += 1
        elif not (
            -1 - GREEKS_VALIDATION_TOLERANCE <= estimate.delta <= GREEKS_VALIDATION_TOLERANCE
        ):
            delta_violations += 1
        if estimate.gamma is not None and estimate.gamma < -GREEKS_VALIDATION_TOLERANCE:
            gamma_violations += 1
        if estimate.vega is not None and estimate.vega < -GREEKS_VALIDATION_TOLERANCE:
            vega_violations += 1
        if estimate.theta is not None:
            if estimate.theta > 0:
                theta_positive += 1
            elif estimate.theta < 0:
                theta_negative += 1
        by_expiration_type.setdefault((contract.expiration, contract.option_type), []).append(
            (contract.strike, estimate.delta)
        )
        by_expiration_strike.setdefault((contract.expiration, contract.strike), {})[
            contract.option_type
        ] = estimate.delta

    monotonic_violations = 0
    for (_expiration, option_type), points in by_expiration_type.items():
        ordered = sorted(points, key=lambda item: item[0])
        for (strike_a, delta_a), (strike_b, delta_b) in pairwise(ordered):
            if strike_a == strike_b:
                continue
            if option_type is OptionType.CALL and delta_b > delta_a + GREEKS_VALIDATION_TOLERANCE:
                monotonic_violations += 1
            if option_type is OptionType.PUT and delta_b < delta_a - GREEKS_VALIDATION_TOLERANCE:
                monotonic_violations += 1

    parity_violations = 0
    for sides in by_expiration_strike.values():
        call_delta = sides.get(OptionType.CALL)
        put_delta = sides.get(OptionType.PUT)
        if call_delta is None or put_delta is None:
            continue
        if abs((call_delta - put_delta) - 1) > GREEKS_CALL_PUT_DELTA_PARITY_TOLERANCE:
            parity_violations += 1

    findings: list[str] = []
    if delta_violations:
        findings.append(f"{delta_violations}_OF_{checked}_CONTRACTS_DELTA_OUTSIDE_EXPECTED_RANGE")
    if gamma_violations:
        findings.append(f"{gamma_violations}_OF_{checked}_CONTRACTS_NEGATIVE_GAMMA")
    if vega_violations:
        findings.append(f"{vega_violations}_OF_{checked}_CONTRACTS_NEGATIVE_VEGA")
    if monotonic_violations:
        findings.append(f"{monotonic_violations}_STRIKE_PAIRS_VIOLATED_DELTA_MONOTONICITY")
    if parity_violations:
        findings.append(
            f"{parity_violations}_STRIKE_PAIRS_VIOLATED_CALL_PUT_DELTA_PARITY_APPROXIMATION"
        )
    if not findings:
        findings.append("NO_VIOLATIONS_DETECTED")
    return GreeksValidationReport(
        underlying_symbol,
        checked,
        delta_violations,
        gamma_violations,
        vega_violations,
        theta_positive,
        theta_negative,
        monotonic_violations,
        parity_violations,
        tuple(findings),
    )


# --------------------------------------------------------------------------
# Cross-sectional intelligence (milestone section 17): bounded, descriptive
# comparisons across the cohort. Observations only -- never an Opportunity
# ranking or a trade recommendation.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrossSectionalHighlight:
    metric: str
    underlying_symbol: str
    underlying_canonical_security_id: str
    value: Decimal
    detail: str


@dataclass(frozen=True, slots=True)
class CrossSectionalIntelligenceSummary:
    generated_at: datetime
    underlyings_considered: int
    highlights: tuple[CrossSectionalHighlight, ...]
    warnings: tuple[str, ...]


def build_cross_sectional_summary(
    *, generated_at: datetime, snapshots: tuple[OptionsChainSnapshot, ...]
) -> CrossSectionalIntelligenceSummary:
    _aware(generated_at, "CROSS_SECTIONAL_GENERATED_AT")
    highlights: list[CrossSectionalHighlight] = []
    warnings: list[str] = []

    def _pick(
        label: str,
        values: list[tuple[OptionsChainSnapshot, Decimal | None]],
        *,
        pick_max: bool,
        detail: str,
    ) -> None:
        usable = [(snap, value) for snap, value in values if value is not None]
        if not usable:
            warnings.append(f"{label}_UNAVAILABLE_NO_USABLE_SNAPSHOTS")
            return
        chosen_snapshot, chosen_value = (
            max(usable, key=lambda item: item[1])
            if pick_max
            else min(usable, key=lambda item: item[1])
        )
        highlights.append(
            CrossSectionalHighlight(
                label,
                chosen_snapshot.underlying_symbol,
                chosen_snapshot.underlying_canonical_security_id,
                chosen_value,
                f"{detail} = {chosen_value}",
            )
        )

    atm_ivs = [
        (s, s.volatility_surface.atm_points[0].atm_iv if s.volatility_surface.atm_points else None)
        for s in snapshots
    ]
    _pick("HIGHEST_ATM_IV", atm_ivs, pick_max=True, detail="nearest-expiration ATM IV")
    _pick("LOWEST_ATM_IV", atm_ivs, pick_max=False, detail="nearest-expiration ATM IV")

    skews = [
        (
            s,
            s.volatility_surface.skew_by_expiration[0].skew_target_delta
            if s.volatility_surface.skew_by_expiration
            else None,
        )
        for s in snapshots
    ]
    _pick(
        "STEEPEST_PUT_SKEW",
        skews,
        pick_max=True,
        detail="nearest-expiration 25-delta put-minus-call IV",
    )
    _pick(
        "STEEPEST_CALL_SKEW",
        skews,
        pick_max=False,
        detail="nearest-expiration 25-delta put-minus-call IV",
    )

    term_spreads = [(s, s.volatility_surface.term_structure.near_far_spread) for s in snapshots]
    _pick(
        "STEEPEST_CONTANGO_TERM_STRUCTURE",
        term_spreads,
        pick_max=True,
        detail="far-minus-near ATM IV spread",
    )
    _pick(
        "MOST_INVERTED_TERM_STRUCTURE",
        term_spreads,
        pick_max=False,
        detail="far-minus-near ATM IV spread",
    )

    moves: list[tuple[OptionsChainSnapshot, Decimal | None]] = []
    for s in snapshots:
        straddle = next(
            (
                m
                for m in s.expected_moves
                if m.method == "ATM_STRADDLE_V1" and m.status == "COMPUTED"
            ),
            None,
        )
        moves.append((s, straddle.percent_move if straddle is not None else None))
    _pick(
        "LARGEST_EXPECTED_MOVE",
        moves,
        pick_max=True,
        detail="nearest-expiration ATM straddle-implied percent move",
    )

    spreads = [(s, s.liquidity_profile.avg_relative_spread) for s in snapshots]
    _pick("WIDEST_RELATIVE_SPREAD", spreads, pick_max=True, detail="chain-average relative spread")
    _pick(
        "NARROWEST_RELATIVE_SPREAD", spreads, pick_max=False, detail="chain-average relative spread"
    )

    iv_vs_rv = [
        (s, s.iv_vs_realized.premium_ratio if s.iv_vs_realized is not None else None)
        for s in snapshots
    ]
    _pick(
        "HIGHEST_IV_VS_RV_RATIO",
        iv_vs_rv,
        pick_max=True,
        detail="ATM IV / 20-session realized-volatility ratio",
    )
    _pick(
        "LOWEST_IV_VS_RV_RATIO",
        iv_vs_rv,
        pick_max=False,
        detail="ATM IV / 20-session realized-volatility ratio",
    )

    return CrossSectionalIntelligenceSummary(
        generated_at, len(snapshots), tuple(highlights), tuple(warnings)
    )


# --------------------------------------------------------------------------
# Risk-free-rate and dividend-yield assumption sensitivity (milestone
# sections 10/11): re-solves Apex IV for already-captured midpoints under
# one alternate declared rate and one alternate declared dividend yield --
# diagnostic only, never a second "official" IV or a forecast.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssumptionSensitivityRow:
    canonical_contract_id: str
    underlying_symbol: str
    option_type: OptionType
    baseline_risk_free_rate: Decimal
    baseline_dividend_yield: Decimal
    baseline_implied_volatility: Decimal | None
    alternate_risk_free_rate: Decimal
    iv_under_alternate_rate: Decimal | None
    iv_rate_sensitivity: Decimal | None
    alternate_dividend_yield: Decimal
    iv_under_alternate_dividend: Decimal | None
    iv_dividend_sensitivity: Decimal | None


def build_assumption_sensitivity_row(
    *, underlying_symbol: str, observation: OptionQuoteObservation, as_of: date
) -> AssumptionSensitivityRow | None:
    if observation.midpoint is None:
        return None
    contract = observation.contract
    price = observation.underlying_reference_price
    baseline = ApexImpliedVolatilityEstimate.solve(
        contract=contract,
        underlying_price=price,
        option_price=observation.midpoint,
        option_price_source="MIDPOINT",
        as_of=as_of,
    )
    rate_alternate = ApexImpliedVolatilityEstimate.solve(
        contract=contract,
        underlying_price=price,
        option_price=observation.midpoint,
        option_price_source="MIDPOINT",
        as_of=as_of,
        risk_free_rate=SENSITIVITY_ALTERNATE_RISK_FREE_RATE,
        risk_free_rate_source=SENSITIVITY_ALTERNATE_RISK_FREE_RATE_SOURCE,
    )
    dividend_alternate = ApexImpliedVolatilityEstimate.solve(
        contract=contract,
        underlying_price=price,
        option_price=observation.midpoint,
        option_price_source="MIDPOINT",
        as_of=as_of,
        dividend_yield=SENSITIVITY_ALTERNATE_DIVIDEND_YIELD,
        dividend_yield_source=SENSITIVITY_ALTERNATE_DIVIDEND_YIELD_SOURCE,
    )
    rate_sensitivity = (
        _exact(rate_alternate.implied_volatility - baseline.implied_volatility)
        if baseline.implied_volatility is not None and rate_alternate.implied_volatility is not None
        else None
    )
    dividend_sensitivity = (
        _exact(dividend_alternate.implied_volatility - baseline.implied_volatility)
        if baseline.implied_volatility is not None
        and dividend_alternate.implied_volatility is not None
        else None
    )
    return AssumptionSensitivityRow(
        contract.canonical_contract_id,
        underlying_symbol,
        contract.option_type,
        RISK_FREE_RATE_DEFAULT,
        DIVIDEND_YIELD_DEFAULT,
        baseline.implied_volatility,
        SENSITIVITY_ALTERNATE_RISK_FREE_RATE,
        rate_alternate.implied_volatility,
        rate_sensitivity,
        SENSITIVITY_ALTERNATE_DIVIDEND_YIELD,
        dividend_alternate.implied_volatility,
        dividend_sensitivity,
    )


# ==========================================================================
# Defined-Risk Options Structure Economics and Research Integration v1
# addition (milestone section 4): deterministic stratified bounded chain
# sampling, replacing the flat first-N contract truncation the previous
# milestone's genuine capture found concentrates dense chains into their
# nearest few expirations (e.g. SPY's 200-contract cap covering only 4
# expirations spanning 3-6 DTE out of the declared 0-60 DTE / 6-expiration
# policy -- see ``options-quote-validation-report``'s retained
# ``DENSE_CHAIN_CONTRACT_CAP_CONCENTRATES_NEAR_TERM`` limitation note).
#
# This is a selection-policy fix only: ``OptionChainCaptureBounds.
# max_contracts_per_underlying`` is never widened -- the same final count is
# retained -- only *which* contracts fill that count changes, from
# provider-response order to a deterministic round-robin across
# (expiration bucket, call/put, moneyness) strata, nearest-the-money first
# within each stratum. Pure and provider-neutral: the caller (the Alpaca
# adapter) supplies already-OCC-parsed candidates; nothing here performs a
# network request or knows about any specific provider's response shape.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class ChainSampleCandidate:
    """One contract eligible for stratified selection, reduced to exactly
    the fields the selector needs -- deliberately not a full
    ``OptionContract``, since that identity is only worth constructing for
    contracts actually selected."""

    contract_symbol: str
    expiration: str
    option_type: OptionType
    strike: Decimal


@dataclass(frozen=True, slots=True)
class ChainCoverageCount:
    key: str
    contracts_available: int
    contracts_selected: int


@dataclass(frozen=True, slots=True)
class StratifiedChainSelection:
    """Bounded, deterministic selection evidence (milestone section 4).
    Never persisted as its own immutable row -- like
    ``CohortQuoteQualityReport``/``CrossSectionalIntelligenceSummary``
    above, it carries no ``canonical_hash`` because it is fully and
    deterministically reproducible from its declared inputs; it exists to
    be surfaced in the capture command's reviewable output, not to grow the
    persisted schema for a computed selection summary."""

    underlying_price: Decimal
    captured_at: datetime
    max_contracts: int
    contracts_available: int
    contracts_selected: int
    contracts_excluded_by_cap: int
    coverage_by_expiration_bucket: tuple[ChainCoverageCount, ...]
    coverage_by_expiration_date: tuple[ChainCoverageCount, ...]
    coverage_by_moneyness: tuple[ChainCoverageCount, ...]
    coverage_by_option_type: tuple[ChainCoverageCount, ...]
    selected_contract_symbols: tuple[str, ...]
    methodology: str

    @property
    def expiration_count_available(self) -> int:
        """Distinct exact expiration dates present anywhere in the eligible
        (pre-cap) candidate pool -- milestone section 5's "expiration count
        available", deliberately exact-date, not the coarser
        ``coverage_by_expiration_bucket``."""
        return len(self.coverage_by_expiration_date)

    @property
    def expiration_count_selected(self) -> int:
        """Distinct exact expiration dates with at least one contract
        actually retained after the cap -- milestone section 5's
        "expiration count selected". A dense underlying whose entire budget
        is consumed by the nearest one or two expirations reports a small
        number here even when ``expiration_count_available`` is large."""
        return sum(1 for item in self.coverage_by_expiration_date if item.contracts_selected > 0)


_STRATIFIED_SELECTION_METHODOLOGY = (
    "DETERMINISTIC_ROUND_ROBIN_BY_EXPIRATION_BUCKET_CALL_PUT_MONEYNESS_V1: "
    "candidates are grouped into (expiration bucket, call/put, moneyness "
    "classification) strata; each stratum is internally sorted nearest-the-"
    "money first, then ties broken by contract symbol; one contract is "
    "drawn from each non-exhausted stratum in deterministic stratum order, "
    "repeating until max_contracts is reached or every stratum is "
    "exhausted. Never widens max_contracts_per_underlying -- the same "
    "final count is retained, only which contracts fill it changes."
)


def _stratum_key(
    candidate: ChainSampleCandidate, *, underlying_price: Decimal, as_of: date
) -> tuple[ExpirationBucket, OptionType, MoneynessClassification]:
    dte = max(0, (date.fromisoformat(candidate.expiration) - as_of).days)
    bucket = _expiration_bucket(dte)
    moneyness = classify_moneyness(
        underlying_price=underlying_price,
        strike=candidate.strike,
        option_type=candidate.option_type,
    )
    return bucket, candidate.option_type, moneyness


def _coverage_counts(
    available_by_key: dict[str, int], selected_by_key: dict[str, int]
) -> tuple[ChainCoverageCount, ...]:
    keys = sorted(set(available_by_key) | set(selected_by_key))
    return tuple(
        ChainCoverageCount(key, available_by_key.get(key, 0), selected_by_key.get(key, 0))
        for key in keys
    )


def select_contracts_stratified(
    candidates: tuple[ChainSampleCandidate, ...],
    *,
    underlying_price: Decimal,
    captured_at: datetime,
    max_contracts: int,
) -> StratifiedChainSelection:
    """Deterministically select at most ``max_contracts`` of ``candidates``,
    preserving coverage across expiration/call-put/moneyness strata instead
    of truncating to however many happened to arrive first. Fewer
    candidates than ``max_contracts`` retains all of them unchanged."""

    if max_contracts < 1:
        raise OptionsQuoteIntelligenceError("STRATIFIED_SELECTION_MAX_CONTRACTS_INVALID")
    as_of = captured_at.date()
    deduplicated = {item.contract_symbol: item for item in candidates}
    ordered_all = tuple(sorted(deduplicated.values(), key=lambda item: item.contract_symbol))
    available = len(ordered_all)

    groups: dict[
        tuple[ExpirationBucket, OptionType, MoneynessClassification], list[ChainSampleCandidate]
    ] = {}
    for candidate in ordered_all:
        key = _stratum_key(candidate, underlying_price=underlying_price, as_of=as_of)
        groups.setdefault(key, []).append(candidate)
    for members in groups.values():
        members.sort(key=lambda item: (abs(item.strike - underlying_price), item.contract_symbol))

    if available <= max_contracts:
        selected = ordered_all
    else:
        strata_order = sorted(groups, key=lambda key: (key[0].value, key[1].value, key[2].value))
        cursors = dict.fromkeys(strata_order, 0)
        picked: list[ChainSampleCandidate] = []
        progressed = True
        while len(picked) < max_contracts and progressed:
            progressed = False
            for key in strata_order:
                if len(picked) >= max_contracts:
                    break
                pool = groups[key]
                cursor = cursors[key]
                if cursor < len(pool):
                    picked.append(pool[cursor])
                    cursors[key] = cursor + 1
                    progressed = True
        selected = tuple(sorted(picked, key=lambda item: item.contract_symbol))

    selected_symbols = frozenset(item.contract_symbol for item in selected)
    available_by_expiration: dict[str, int] = {}
    selected_by_expiration: dict[str, int] = {}
    available_by_expiration_date: dict[str, int] = {}
    selected_by_expiration_date: dict[str, int] = {}
    available_by_moneyness: dict[str, int] = {}
    selected_by_moneyness: dict[str, int] = {}
    available_by_type: dict[str, int] = {}
    selected_by_type: dict[str, int] = {}
    for candidate in ordered_all:
        bucket, option_type, moneyness = _stratum_key(
            candidate, underlying_price=underlying_price, as_of=as_of
        )
        is_selected = candidate.contract_symbol in selected_symbols
        available_by_expiration[bucket.value] = available_by_expiration.get(bucket.value, 0) + 1
        available_by_expiration_date[candidate.expiration] = (
            available_by_expiration_date.get(candidate.expiration, 0) + 1
        )
        available_by_moneyness[moneyness.value] = available_by_moneyness.get(moneyness.value, 0) + 1
        available_by_type[option_type.value] = available_by_type.get(option_type.value, 0) + 1
        if is_selected:
            selected_by_expiration[bucket.value] = selected_by_expiration.get(bucket.value, 0) + 1
            selected_by_expiration_date[candidate.expiration] = (
                selected_by_expiration_date.get(candidate.expiration, 0) + 1
            )
            selected_by_moneyness[moneyness.value] = (
                selected_by_moneyness.get(moneyness.value, 0) + 1
            )
            selected_by_type[option_type.value] = selected_by_type.get(option_type.value, 0) + 1

    return StratifiedChainSelection(
        underlying_price,
        captured_at,
        max_contracts,
        available,
        len(selected),
        available - len(selected),
        _coverage_counts(available_by_expiration, selected_by_expiration),
        _coverage_counts(available_by_expiration_date, selected_by_expiration_date),
        _coverage_counts(available_by_moneyness, selected_by_moneyness),
        _coverage_counts(available_by_type, selected_by_type),
        tuple(item.contract_symbol for item in selected),
        _STRATIFIED_SELECTION_METHODOLOGY,
    )
