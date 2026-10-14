"""Pure Coinbase-compatible internal Crypto Spot PAPER rules.

This module owns no provider client, credential, network, LIVE, custody, or
transfer capability.  Every monetary value is hypothetical PAPER state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from apex.domain.crypto_universe import CryptoUniverseSnapshot, PublicCryptoProduct
from apex.domain.shared_opportunity import (
    EligibilityDecision as SharedEligibilityDecision,
)
from apex.domain.shared_opportunity import (
    EligibilityState as SharedEligibilityState,
)
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import DecimalInput, content_digest, parse_decimal

CRYPTO_PAPER_PROVIDER_CONTRACT_V1 = "COINBASE_ADVANCED_TRADE_SPOT_PAPER_CONTRACT_V1"
CRYPTO_PAPER_ORDER_ELIGIBILITY_POLICY_V1 = "CRYPTO_PAPER_ORDER_ELIGIBILITY_POLICY_V1"
CRYPTO_PAPER_EXECUTION_POLICY_V1 = "CRYPTO_PAPER_EXECUTION_POLICY_V1"
CRYPTO_PAPER_ACCOUNTING_POLICY_V1 = "CRYPTO_PAPER_DOUBLE_ENTRY_ACCOUNTING_V1"
CRYPTO_PAPER_WARNING = "CRYPTO SPOT PAPER — SIMULATED CAPITAL ONLY"
MAX_ORDER_NOTIONAL = Decimal("25")
MAX_OPEN_NOTIONAL = Decimal("100")
MAX_OPEN_ORDERS_PER_PRODUCT = 1
DEFAULT_PREVIEW_SECONDS = 300


class CryptoPaperError(ValueError):
    """A Crypto PAPER invariant failed closed."""


class ProviderCapabilityState(StrEnum):
    PUBLIC_MARKET_DATA_AVAILABLE = "PUBLIC_MARKET_DATA_AVAILABLE"
    PRIVATE_CREDENTIALS_ABSENT = "PRIVATE_CREDENTIALS_ABSENT"
    PRIVATE_VIEW_AVAILABLE = "PRIVATE_VIEW_AVAILABLE"
    ORDER_PREVIEW_AVAILABLE = "ORDER_PREVIEW_AVAILABLE"
    LIVE_TRADE_PERMISSION_AVAILABLE = "LIVE_TRADE_PERMISSION_AVAILABLE"
    PAPER_ONLY = "PAPER_ONLY"
    UNAVAILABLE = "UNAVAILABLE"


class PaperAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"


class PaperSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PaperOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class PaperOrderState(StrEnum):
    PREVIEWED = "PREVIEWED"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class PaperReservationState(StrEnum):
    HELD = "HELD"
    PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


class PaperEligibilityState(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"


class PaperReconciliationStatus(StrEnum):
    CLEAN = "CLEAN"
    DISCREPANCY = "DISCREPANCY"


class PaperPricingSource(StrEnum):
    GENUINE_TOP_OF_BOOK = "GENUINE_TOP_OF_BOOK"
    RETAINED_DAILY_OPEN_FALLBACK = "RETAINED_DAILY_OPEN_FALLBACK"
    FIXTURE_RETAINED_OPEN = "FIXTURE_RETAINED_OPEN"


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CryptoPaperError(reason)
    return value.astimezone(UTC)


def _id(value: str, reason: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise CryptoPaperError(reason)
    return value.strip()


def _decimal(
    value: DecimalInput,
    reason: str,
    *,
    positive: bool = False,
    signed: bool = False,
) -> Decimal:
    try:
        return parse_decimal(
            value,
            allow_negative=signed,
            allow_zero=not positive,
        )
    except ValueError as error:
        raise CryptoPaperError(reason) from error


def _floor(value: Decimal, increment: Decimal) -> Decimal:
    steps = (value / increment).to_integral_value(rounding=ROUND_FLOOR)
    return _decimal(steps * increment, "CRYPTO_PAPER_ROUNDED_VALUE_INVALID")


def _ceil(value: Decimal, increment: Decimal) -> Decimal:
    steps = (value / increment).to_integral_value(rounding=ROUND_CEILING)
    return _decimal(steps * increment, "CRYPTO_PAPER_ROUNDED_VALUE_INVALID")


@dataclass(frozen=True, slots=True)
class CryptoPaperProviderContract:
    contract_id: str
    product_id: str
    provider: str
    base_asset: str
    quote_asset: str
    trading_status: str
    product_status: str
    base_increment: Decimal
    quote_increment: Decimal
    price_increment: Decimal
    minimum_base_size: Decimal
    minimum_quote_notional: Decimal
    permitted_order_configurations: tuple[str, ...]
    fee_evidence_status: str
    account_capability_status: ProviderCapabilityState
    preview_capability: ProviderCapabilityState
    submission_capability: ProviderCapabilityState
    provider_contract_version: str
    source_capture_id: str
    source_product_hash: str
    evidence_class: str
    known_limitations: tuple[str, ...]
    canonical_hash: str
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"


def build_provider_contract(
    *,
    contract_id: str,
    product: PublicCryptoProduct,
    source_capture_id: str,
) -> CryptoPaperProviderContract:
    reasons = tuple(product.restriction_reasons)
    if (
        product.market_type != "SPOT"
        or product.quote_asset != "USD"
        or product.trading_status != "ACTIVE"
        or product.product_status != "ONLINE"
        or product.price_increment <= 0
        or product.quantity_increment <= 0
        or product.minimum_quantity is None
        or product.minimum_notional is None
        or reasons
    ):
        capability = ProviderCapabilityState.UNAVAILABLE
    else:
        capability = ProviderCapabilityState.PAPER_ONLY
    values = (
        _id(contract_id, "CRYPTO_PAPER_CONTRACT_ID_INVALID"),
        product.product_id,
        product.provider,
        product.base_asset,
        product.quote_asset,
        product.trading_status,
        product.product_status,
        product.quantity_increment,
        product.price_increment,
        product.price_increment,
        product.minimum_quantity or Decimal(0),
        product.minimum_notional or Decimal(0),
        ("MARKET_BUY_QUOTE", "MARKET_SELL_BASE", "LIMIT_BUY_BASE", "LIMIT_SELL_BASE"),
        "MODELED_FROM_RETAINED_PUBLIC_EVIDENCE",
        ProviderCapabilityState.PRIVATE_CREDENTIALS_ABSENT,
        ProviderCapabilityState.PRIVATE_CREDENTIALS_ABSENT,
        capability,
        CRYPTO_PAPER_PROVIDER_CONTRACT_V1,
        _id(source_capture_id, "CRYPTO_PAPER_CAPTURE_ID_INVALID"),
        product.canonical_hash,
        product.evidence_class.value,
        tuple(
            sorted(
                {
                    "NO_COINBASE_ACCOUNT_BALANCE",
                    "NO_AUTHENTICATED_COINBASE_PREVIEW",
                    "NO_COINBASE_ORDER_SUBMISSION_OR_CANCELLATION",
                    "PUBLIC_TOP_OF_BOOK_UNAVAILABLE_UNLESS_SEPARATELY_RETAINED",
                    *reasons,
                }
            )
        ),
        "PAPER_ONLY",
        "NONE",
    )
    return CryptoPaperProviderContract(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class CryptoPaperCapability:
    capability_id: str
    contract_id: str | None
    primary_state: ProviderCapabilityState
    states: tuple[ProviderCapabilityState, ...]
    credentials_requested: bool
    provider_requests: int
    inspected_at: datetime
    limitations: tuple[str, ...]
    canonical_hash: str
    paper_authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"


def inspect_paper_capability(
    *, contract: CryptoPaperProviderContract | None, inspected_at: datetime
) -> CryptoPaperCapability:
    primary = (
        ProviderCapabilityState.PAPER_ONLY
        if contract is not None
        and contract.submission_capability is ProviderCapabilityState.PAPER_ONLY
        else ProviderCapabilityState.UNAVAILABLE
    )
    states = (
        (
            ProviderCapabilityState.PUBLIC_MARKET_DATA_AVAILABLE,
            ProviderCapabilityState.PRIVATE_CREDENTIALS_ABSENT,
            ProviderCapabilityState.PAPER_ONLY,
        )
        if primary is ProviderCapabilityState.PAPER_ONLY
        else (ProviderCapabilityState.UNAVAILABLE,)
    )
    limitations = (
        contract.known_limitations
        if contract is not None
        else ("NO_RETAINED_COMPATIBLE_SPOT_PRODUCT_CONTRACT",)
    )
    values = (
        f"{contract.contract_id if contract else 'crypto-paper'}-capability",
        None if contract is None else contract.contract_id,
        primary,
        states,
        False,
        0,
        _time(inspected_at, "CRYPTO_PAPER_CAPABILITY_TIME_INVALID"),
        limitations,
        "PAPER_ONLY",
        "NONE",
    )
    return CryptoPaperCapability(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class PaperAssetBalance:
    asset: str
    total: Decimal
    reserved: Decimal

    @property
    def available(self) -> Decimal:
        return self.total - self.reserved


@dataclass(frozen=True, slots=True)
class CryptoPaperAccount:
    account_id: str
    venue: str
    base_currency: str
    opening_cash: Decimal
    created_at: datetime
    status: PaperAccountStatus
    account_type: str
    canonical_hash: str
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        opening_cash: DecimalInput,
        created_at: datetime,
    ) -> CryptoPaperAccount:
        values = (
            _id(account_id, "CRYPTO_PAPER_ACCOUNT_ID_INVALID"),
            "APEX_INTERNAL_CRYPTO_SPOT_PAPER",
            "USD",
            _decimal(opening_cash, "CRYPTO_PAPER_OPENING_CASH_INVALID", positive=True),
            _time(created_at, "CRYPTO_PAPER_ACCOUNT_TIME_INVALID"),
            PaperAccountStatus.ACTIVE,
            "PAPER",
            "PAPER_ONLY",
            "NONE",
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class CryptoPaperAccountState:
    account_id: str
    version: int
    cash: Decimal
    reserved_cash: Decimal
    balances: tuple[PaperAssetBalance, ...]
    realized_paper_pnl: Decimal
    modeled_fees: Decimal
    updated_at: datetime
    last_event_id: str
    canonical_hash: str
    authority: str = "PAPER_ONLY"

    @property
    def available_cash(self) -> Decimal:
        return self.cash - self.reserved_cash

    def balance(self, asset: str) -> PaperAssetBalance:
        return next(
            (item for item in self.balances if item.asset == asset),
            PaperAssetBalance(asset, Decimal(0), Decimal(0)),
        )

    @classmethod
    def opening(cls, *, account: CryptoPaperAccount, event_id: str) -> CryptoPaperAccountState:
        values = (
            account.account_id,
            1,
            account.opening_cash,
            Decimal(0),
            (),
            Decimal(0),
            Decimal(0),
            account.created_at,
            _id(event_id, "CRYPTO_PAPER_ACCOUNT_EVENT_ID_INVALID"),
            "PAPER_ONLY",
        )
        return cls(*values[:-1], content_digest(values), *values[-1:])


@dataclass(frozen=True, slots=True)
class CryptoPaperEligibility:
    decision_id: str
    opportunity_set_id: str
    projection_id: str
    policy_id: str
    evaluated_at: datetime
    state: PaperEligibilityState
    reasons: tuple[str, ...]
    product_contract_id: str | None
    account_id: str | None
    canonical_hash: str
    paper_authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"


def evaluate_paper_eligibility(
    *,
    decision_id: str,
    opportunity_set_id: str,
    projection: SharedOpportunityProjection,
    shared_decision: SharedEligibilityDecision,
    universe: CryptoUniverseSnapshot,
    contract: CryptoPaperProviderContract | None,
    account: CryptoPaperAccount | None,
    account_state: CryptoPaperAccountState | None,
    operating_cycle_enabled: bool,
    evaluated_at: datetime,
) -> CryptoPaperEligibility:
    now = _time(evaluated_at, "CRYPTO_PAPER_ELIGIBILITY_TIME_INVALID")
    reasons: list[str] = []
    if projection.venue is not SharedVenue.CRYPTO:
        reasons.append("SOURCE_VENUE_NOT_CRYPTO")
    if projection.valid_until is None or now >= projection.valid_until:
        reasons.append("SOURCE_OPPORTUNITY_EXPIRED")
    if shared_decision.state is not SharedEligibilityState.ELIGIBLE:
        reasons.append(f"SHARED_OPPORTUNITY_{shared_decision.state.value}")
    if not projection.paper_eligible:
        reasons.append("SOURCE_OPPORTUNITY_DOES_NOT_PERMIT_PAPER_CONSIDERATION")
    if projection.product_or_market_id not in universe.eligible_product_ids:
        reasons.append("PRODUCT_NOT_IN_CURRENT_ELIGIBLE_CRYPTO_UNIVERSE")
    if contract is None:
        reasons.append("PRODUCT_CONTRACT_UNAVAILABLE")
    elif contract.product_id != projection.product_or_market_id:
        reasons.append("PRODUCT_CONTRACT_MISMATCH")
    elif contract.submission_capability is not ProviderCapabilityState.PAPER_ONLY:
        reasons.append("PRODUCT_ROUTE_NOT_PAPER_COMPATIBLE")
    if account is None or account_state is None:
        reasons.append("PAPER_ACCOUNT_UNAVAILABLE")
    elif account.status is not PaperAccountStatus.ACTIVE:
        reasons.append("PAPER_ACCOUNT_NOT_ACTIVE")
    elif account_state.available_cash <= 0:
        reasons.append("PAPER_CASH_UNAVAILABLE")
    if not projection.costs_retained:
        reasons.append("ESTIMATED_COSTS_NOT_RETAINED")
    if not operating_cycle_enabled:
        reasons.append("OPERATING_CYCLE_NOT_ENABLED")
    state = PaperEligibilityState.ELIGIBLE if not reasons else PaperEligibilityState.REJECTED
    if state is PaperEligibilityState.ELIGIBLE:
        reasons.append("ELIGIBLE_FOR_EXACT_OPERATOR_AUTHORIZED_PAPER_CONSIDERATION")
    values = (
        _id(decision_id, "CRYPTO_PAPER_DECISION_ID_INVALID"),
        _id(opportunity_set_id, "CRYPTO_PAPER_SET_ID_INVALID"),
        projection.projection_id,
        CRYPTO_PAPER_ORDER_ELIGIBILITY_POLICY_V1,
        now,
        state,
        tuple(sorted(set(reasons))),
        None if contract is None else contract.contract_id,
        None if account is None else account.account_id,
        "PAPER_ONLY",
        "NONE",
    )
    return CryptoPaperEligibility(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class CryptoPaperEligibilityScan:
    scan_id: str
    opportunity_set_id: str
    evaluated_at: datetime
    decision_ids: tuple[str, ...]
    crypto_source_count: int
    eligible_count: int
    authorization_count: int
    order_count: int
    provider_requests: int
    canonical_hash: str
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        scan_id: str,
        opportunity_set_id: str,
        decisions: tuple[CryptoPaperEligibility, ...],
        evaluated_at: datetime,
    ) -> CryptoPaperEligibilityScan:
        values = (
            _id(scan_id, "CRYPTO_PAPER_SCAN_ID_INVALID"),
            _id(opportunity_set_id, "CRYPTO_PAPER_SCAN_SET_ID_INVALID"),
            _time(evaluated_at, "CRYPTO_PAPER_SCAN_TIME_INVALID"),
            tuple(item.decision_id for item in decisions),
            len(decisions),
            sum(item.state is PaperEligibilityState.ELIGIBLE for item in decisions),
            0,
            0,
            0,
            "PAPER_ONLY",
            "NONE",
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class CryptoPaperAuthorization:
    authorization_id: str
    account_id: str
    opportunity_set_id: str
    projection_id: str
    source_projection_hash: str
    eligibility_decision_id: str
    eligibility_decision_hash: str
    strategy_or_discovery_id: str
    product_id: str
    maximum_quote_notional: Decimal
    maximum_base_size: Decimal | None
    allowed_side: PaperSide
    allowed_order_type: PaperOrderType
    expires_at: datetime
    actor: str
    reason: str
    idempotency_key: str
    created_at: datetime
    canonical_hash: str
    paper_authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        authorization_id: str,
        account_id: str,
        eligibility: CryptoPaperEligibility,
        projection: SharedOpportunityProjection,
        maximum_quote_notional: DecimalInput,
        maximum_base_size: DecimalInput | None,
        allowed_side: PaperSide,
        allowed_order_type: PaperOrderType,
        expires_at: datetime,
        actor: str,
        reason: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> CryptoPaperAuthorization:
        created = _time(created_at, "CRYPTO_PAPER_AUTHORIZATION_TIME_INVALID")
        expiry = _time(expires_at, "CRYPTO_PAPER_AUTHORIZATION_EXPIRY_INVALID")
        maximum = _decimal(
            maximum_quote_notional,
            "CRYPTO_PAPER_AUTHORIZATION_NOTIONAL_INVALID",
            positive=True,
        )
        if eligibility.state is not PaperEligibilityState.ELIGIBLE:
            raise CryptoPaperError("CRYPTO_PAPER_OPPORTUNITY_NOT_ELIGIBLE")
        if (
            eligibility.projection_id != projection.projection_id
            or eligibility.opportunity_set_id == ""
            or eligibility.account_id != account_id
        ):
            raise CryptoPaperError("CRYPTO_PAPER_AUTHORIZATION_LINEAGE_MISMATCH")
        if expiry <= created or maximum > MAX_ORDER_NOTIONAL:
            raise CryptoPaperError("CRYPTO_PAPER_AUTHORIZATION_BOUND_INVALID")
        base_max = (
            None
            if maximum_base_size is None
            else _decimal(
                maximum_base_size,
                "CRYPTO_PAPER_AUTHORIZATION_BASE_INVALID",
                positive=True,
            )
        )
        values = (
            _id(authorization_id, "CRYPTO_PAPER_AUTHORIZATION_ID_INVALID"),
            _id(account_id, "CRYPTO_PAPER_ACCOUNT_ID_INVALID"),
            eligibility.opportunity_set_id,
            projection.projection_id,
            projection.canonical_hash,
            eligibility.decision_id,
            eligibility.canonical_hash,
            projection.strategy_or_method_id,
            projection.product_or_market_id,
            maximum,
            base_max,
            allowed_side,
            allowed_order_type,
            expiry,
            _id(actor, "CRYPTO_PAPER_AUTHORIZATION_ACTOR_INVALID"),
            _id(reason, "CRYPTO_PAPER_AUTHORIZATION_REASON_INVALID"),
            _id(idempotency_key, "CRYPTO_PAPER_IDEMPOTENCY_KEY_INVALID"),
            created,
            "PAPER_ONLY",
            "NONE",
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class CoinbaseOrderProjection:
    client_order_id: str
    product_id: str
    side: PaperSide
    order_configuration: str
    quote_size: str | None
    base_size: str | None
    limit_price: str | None
    canonical_hash: str
    sendable: bool = False


@dataclass(frozen=True, slots=True)
class CryptoPaperOrderIntent:
    intent_id: str
    account_id: str
    authorization_id: str
    opportunity_set_id: str
    projection_id: str
    contract_id: str
    product_id: str
    side: PaperSide
    order_type: PaperOrderType
    quote_size: Decimal | None
    base_size: Decimal | None
    limit_price: Decimal | None
    estimated_notional: Decimal
    validation_results: tuple[str, ...]
    provider_request: CoinbaseOrderProjection
    created_at: datetime
    canonical_hash: str
    paper_authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"


def construct_order_intent(
    *,
    intent_id: str,
    authorization: CryptoPaperAuthorization,
    contract: CryptoPaperProviderContract,
    account_state: CryptoPaperAccountState,
    side: PaperSide,
    order_type: PaperOrderType,
    quote_size: DecimalInput | None,
    base_size: DecimalInput | None,
    limit_price: DecimalInput | None,
    created_at: datetime,
) -> CryptoPaperOrderIntent:
    now = _time(created_at, "CRYPTO_PAPER_INTENT_TIME_INVALID")
    if now >= authorization.expires_at:
        raise CryptoPaperError("CRYPTO_PAPER_AUTHORIZATION_EXPIRED")
    if side is not authorization.allowed_side or order_type is not authorization.allowed_order_type:
        raise CryptoPaperError("CRYPTO_PAPER_AUTHORIZATION_SCOPE_MISMATCH")
    quote = (
        None
        if quote_size is None
        else _decimal(quote_size, "CRYPTO_PAPER_QUOTE_SIZE_INVALID", positive=True)
    )
    base = (
        None
        if base_size is None
        else _decimal(base_size, "CRYPTO_PAPER_BASE_SIZE_INVALID", positive=True)
    )
    price = (
        None
        if limit_price is None
        else _decimal(limit_price, "CRYPTO_PAPER_LIMIT_PRICE_INVALID", positive=True)
    )
    if order_type is PaperOrderType.MARKET and side is PaperSide.BUY:
        if quote is None or base is not None or price is not None:
            raise CryptoPaperError("CRYPTO_PAPER_MARKET_BUY_CONFIGURATION_INVALID")
        if quote % contract.quote_increment != 0:
            raise CryptoPaperError("CRYPTO_PAPER_QUOTE_INCREMENT_INVALID")
        if quote < contract.minimum_quote_notional:
            raise CryptoPaperError("CRYPTO_PAPER_MINIMUM_NOTIONAL_NOT_MET")
        estimated = quote
        configuration = "market_market_ioc"
    elif order_type is PaperOrderType.MARKET and side is PaperSide.SELL:
        if base is None or quote is not None or price is not None:
            raise CryptoPaperError("CRYPTO_PAPER_MARKET_SELL_CONFIGURATION_INVALID")
        if base % contract.base_increment != 0:
            raise CryptoPaperError("CRYPTO_PAPER_BASE_INCREMENT_INVALID")
        if base < contract.minimum_base_size:
            raise CryptoPaperError("CRYPTO_PAPER_MINIMUM_BASE_NOT_MET")
        if account_state.balance(contract.base_asset).available < base:
            raise CryptoPaperError("CRYPTO_PAPER_ASSET_BALANCE_INSUFFICIENT")
        estimated = Decimal(0)
        configuration = "market_market_ioc"
    else:
        if base is None or price is None or quote is not None:
            raise CryptoPaperError("CRYPTO_PAPER_LIMIT_CONFIGURATION_INVALID")
        if base % contract.base_increment != 0:
            raise CryptoPaperError("CRYPTO_PAPER_BASE_INCREMENT_INVALID")
        if price % contract.price_increment != 0:
            raise CryptoPaperError("CRYPTO_PAPER_PRICE_INCREMENT_INVALID")
        if base < contract.minimum_base_size:
            raise CryptoPaperError("CRYPTO_PAPER_MINIMUM_BASE_NOT_MET")
        estimated = _decimal(base * price, "CRYPTO_PAPER_NOTIONAL_INVALID", positive=True)
        if estimated < contract.minimum_quote_notional:
            raise CryptoPaperError("CRYPTO_PAPER_MINIMUM_NOTIONAL_NOT_MET")
        if side is PaperSide.SELL and account_state.balance(contract.base_asset).available < base:
            raise CryptoPaperError("CRYPTO_PAPER_ASSET_BALANCE_INSUFFICIENT")
        configuration = "limit_limit_gtc"
    if estimated > authorization.maximum_quote_notional or estimated > MAX_ORDER_NOTIONAL:
        raise CryptoPaperError("CRYPTO_PAPER_ORDER_NOTIONAL_CAP_EXCEEDED")
    if (
        base is not None
        and authorization.maximum_base_size is not None
        and base > authorization.maximum_base_size
    ):
        raise CryptoPaperError("CRYPTO_PAPER_ORDER_BASE_CAP_EXCEEDED")
    if side is PaperSide.BUY and account_state.available_cash < estimated:
        raise CryptoPaperError("CRYPTO_PAPER_CASH_INSUFFICIENT")
    request_values = (
        _id(intent_id, "CRYPTO_PAPER_INTENT_ID_INVALID"),
        contract.product_id,
        side,
        configuration,
        None if quote is None else format(quote, "f"),
        None if base is None else format(base, "f"),
        None if price is None else format(price, "f"),
        False,
    )
    provider = CoinbaseOrderProjection(*request_values[:-1], content_digest(request_values), False)
    values = (
        intent_id,
        authorization.account_id,
        authorization.authorization_id,
        authorization.opportunity_set_id,
        authorization.projection_id,
        contract.contract_id,
        contract.product_id,
        side,
        order_type,
        quote,
        base,
        price,
        estimated,
        ("VALID", "PROVIDER_REQUEST_PROJECTION_NOT_SENT"),
        provider,
        now,
        "PAPER_ONLY",
        "NONE",
    )
    return CryptoPaperOrderIntent(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class RetainedPaperPriceEvidence:
    evidence_id: str
    product_id: str
    observed_at: datetime
    ingested_at: datetime
    price: Decimal
    available_base_quantity: Decimal | None
    source: PaperPricingSource
    evidence_class: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        evidence_id: str,
        product_id: str,
        observed_at: datetime,
        ingested_at: datetime,
        price: DecimalInput,
        available_base_quantity: DecimalInput | None,
        source: PaperPricingSource,
        evidence_class: str,
    ) -> RetainedPaperPriceEvidence:
        observed = _time(observed_at, "CRYPTO_PAPER_EVIDENCE_TIME_INVALID")
        ingested = _time(ingested_at, "CRYPTO_PAPER_INGESTION_TIME_INVALID")
        if ingested < observed:
            raise CryptoPaperError("CRYPTO_PAPER_EVIDENCE_INGESTION_INVALID")
        available = (
            None
            if available_base_quantity is None
            else _decimal(
                available_base_quantity,
                "CRYPTO_PAPER_EVIDENCE_QUANTITY_INVALID",
                positive=True,
            )
        )
        values = (
            _id(evidence_id, "CRYPTO_PAPER_EVIDENCE_ID_INVALID"),
            _id(product_id, "CRYPTO_PAPER_PRODUCT_ID_INVALID"),
            observed,
            ingested,
            _decimal(price, "CRYPTO_PAPER_EVIDENCE_PRICE_INVALID", positive=True),
            available,
            source,
            _id(evidence_class, "CRYPTO_PAPER_EVIDENCE_CLASS_INVALID"),
        )
        return cls(*values, content_digest(values))


@dataclass(frozen=True, slots=True)
class CryptoPaperPreview:
    preview_id: str
    intent_id: str
    account_id: str
    product_id: str
    account_state_hash: str
    estimated_execution_price: Decimal
    estimated_base_quantity: Decimal
    estimated_quote_quantity: Decimal
    modeled_spread: Decimal
    modeled_slippage: Decimal
    modeled_fee: Decimal
    estimated_total_cost: Decimal
    estimated_remaining_cash: Decimal
    estimated_remaining_asset: Decimal
    rejection_reasons: tuple[str, ...]
    provider_request: CoinbaseOrderProjection
    evidence_ids: tuple[str, ...]
    pricing_source: PaperPricingSource
    created_at: datetime
    expires_at: datetime
    canonical_hash: str
    provider_requests: int = 0
    genuine_coinbase_preview: bool = False
    paper_authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"


def build_paper_preview(
    *,
    preview_id: str,
    intent: CryptoPaperOrderIntent,
    authorization: CryptoPaperAuthorization,
    contract: CryptoPaperProviderContract,
    account_state: CryptoPaperAccountState,
    evidence: RetainedPaperPriceEvidence,
    fee_bps: DecimalInput,
    spread_bps: DecimalInput,
    slippage_bps: DecimalInput,
    created_at: datetime,
    expires_in_seconds: int = DEFAULT_PREVIEW_SECONDS,
) -> CryptoPaperPreview:
    now = _time(created_at, "CRYPTO_PAPER_PREVIEW_TIME_INVALID")
    if evidence.product_id != intent.product_id or evidence.ingested_at > now:
        raise CryptoPaperError("CRYPTO_PAPER_PREVIEW_EVIDENCE_INVALID")
    if authorization.authorization_id != intent.authorization_id:
        raise CryptoPaperError("CRYPTO_PAPER_AUTHORIZATION_SCOPE_MISMATCH")
    if not 1 <= expires_in_seconds <= 3600:
        raise CryptoPaperError("CRYPTO_PAPER_PREVIEW_EXPIRY_INVALID")
    fee_rate = _decimal(fee_bps, "CRYPTO_PAPER_FEE_INVALID") / Decimal(10000)
    spread_rate = _decimal(spread_bps, "CRYPTO_PAPER_SPREAD_INVALID") / Decimal(20000)
    slippage_rate = _decimal(slippage_bps, "CRYPTO_PAPER_SLIPPAGE_INVALID") / Decimal(10000)
    market_adjustment = spread_rate + slippage_rate
    if intent.order_type is PaperOrderType.LIMIT:
        price = intent.limit_price or evidence.price
        spread = Decimal(0)
        slippage = Decimal(0)
    elif intent.side is PaperSide.BUY:
        price = _ceil(
            evidence.price * (Decimal(1) + market_adjustment),
            contract.price_increment,
        )
        spread = evidence.price * spread_rate
        slippage = evidence.price * slippage_rate
    else:
        price = _floor(
            evidence.price * (Decimal(1) - market_adjustment),
            contract.price_increment,
        )
        spread = evidence.price * spread_rate
        slippage = evidence.price * slippage_rate
    if intent.quote_size is not None:
        base = _floor(
            intent.quote_size / (price * (Decimal(1) + fee_rate)),
            contract.base_increment,
        )
    else:
        base = intent.base_size or Decimal(0)
    quote = _floor(base * price, contract.quote_increment)
    fee = _floor(quote * fee_rate, contract.quote_increment)
    total = quote + fee if intent.side is PaperSide.BUY else quote - fee
    rejections: list[str] = []
    if now >= authorization.expires_at:
        rejections.append("PAPER_AUTHORIZATION_EXPIRED")
    if quote > authorization.maximum_quote_notional:
        rejections.append("PAPER_AUTHORIZATION_NOTIONAL_CAP_EXCEEDED")
    if authorization.maximum_base_size is not None and base > authorization.maximum_base_size:
        rejections.append("PAPER_AUTHORIZATION_BASE_CAP_EXCEEDED")
    if base < contract.minimum_base_size:
        rejections.append("MINIMUM_BASE_SIZE_NOT_MET")
    if quote < contract.minimum_quote_notional:
        rejections.append("MINIMUM_QUOTE_NOTIONAL_NOT_MET")
    if intent.side is PaperSide.BUY and total > account_state.available_cash:
        rejections.append("PAPER_CASH_INSUFFICIENT")
    if (
        intent.side is PaperSide.SELL
        and account_state.balance(contract.base_asset).available < base
    ):
        rejections.append("PAPER_ASSET_BALANCE_INSUFFICIENT")
    remaining_cash = (
        account_state.available_cash - total
        if intent.side is PaperSide.BUY
        else account_state.available_cash + total
    )
    remaining_asset = (
        account_state.balance(contract.base_asset).available + base
        if intent.side is PaperSide.BUY
        else account_state.balance(contract.base_asset).available - base
    )
    values = (
        _id(preview_id, "CRYPTO_PAPER_PREVIEW_ID_INVALID"),
        intent.intent_id,
        intent.account_id,
        intent.product_id,
        account_state.canonical_hash,
        price,
        base,
        quote,
        spread,
        slippage,
        fee,
        total,
        remaining_cash,
        remaining_asset,
        tuple(sorted(rejections)),
        intent.provider_request,
        (evidence.evidence_id,),
        evidence.source,
        now,
        now + timedelta(seconds=expires_in_seconds),
        0,
        False,
        "PAPER_ONLY",
        "NONE",
    )
    return CryptoPaperPreview(
        *values[:-4],
        content_digest(values),
        *values[-4:],
    )


@dataclass(frozen=True, slots=True)
class CryptoPaperOrder:
    order_id: str
    account_id: str
    intent_id: str
    preview_id: str
    authorization_id: str
    product_id: str
    base_asset: str
    quote_asset: str
    side: PaperSide
    order_type: PaperOrderType
    target_base_quantity: Decimal
    limit_price: Decimal | None
    filled_base_quantity: Decimal
    filled_quote_quantity: Decimal
    cumulative_fees: Decimal
    state: PaperOrderState
    submitted_at: datetime
    valid_until: datetime
    last_sequence: int
    canonical_hash: str
    paper_authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"

    @property
    def remaining_base_quantity(self) -> Decimal:
        return self.target_base_quantity - self.filled_base_quantity


@dataclass(frozen=True, slots=True)
class CryptoPaperReservation:
    reservation_id: str
    account_id: str
    order_id: str
    asset: str
    original_amount: Decimal
    remaining_amount: Decimal
    consumed_amount: Decimal
    state: PaperReservationState
    version: int
    canonical_hash: str
    authority: str = "PAPER_ONLY"


@dataclass(frozen=True, slots=True)
class CryptoPaperOrderEvent:
    event_id: str
    order_id: str
    sequence: int
    prior_state: PaperOrderState | None
    next_state: PaperOrderState
    actor: str
    occurred_at: datetime
    evidence_ids: tuple[str, ...]
    base_quantity: Decimal
    quote_quantity: Decimal
    cash_effect: Decimal
    fee_effect: Decimal
    reason: str
    canonical_hash: str
    authority: str = "PAPER_ONLY"


@dataclass(frozen=True, slots=True)
class CryptoPaperSubmission:
    order: CryptoPaperOrder
    reservation: CryptoPaperReservation
    account_state: CryptoPaperAccountState
    events: tuple[CryptoPaperOrderEvent, ...]


def submit_paper_order(
    *,
    order_id: str,
    reservation_id: str,
    preview: CryptoPaperPreview,
    intent: CryptoPaperOrderIntent,
    authorization: CryptoPaperAuthorization,
    contract: CryptoPaperProviderContract,
    account_state: CryptoPaperAccountState,
    open_orders: tuple[CryptoPaperOrder, ...],
    submitted_at: datetime,
    valid_until: datetime,
    actor: str,
) -> CryptoPaperSubmission:
    now = _time(submitted_at, "CRYPTO_PAPER_SUBMISSION_TIME_INVALID")
    expiry = _time(valid_until, "CRYPTO_PAPER_ORDER_EXPIRY_INVALID")
    if now >= preview.expires_at or preview.rejection_reasons:
        raise CryptoPaperError("CRYPTO_PAPER_PREVIEW_NOT_SUBMITTABLE")
    if authorization.authorization_id != intent.authorization_id or now >= authorization.expires_at:
        raise CryptoPaperError("CRYPTO_PAPER_AUTHORIZATION_EXPIRED_OR_MISMATCHED")
    if preview.account_state_hash != account_state.canonical_hash:
        raise CryptoPaperError("CRYPTO_PAPER_PREVIEW_BALANCE_CHANGED")
    if expiry <= now:
        raise CryptoPaperError("CRYPTO_PAPER_ORDER_VALIDITY_INVALID")
    active = tuple(
        item
        for item in open_orders
        if item.state
        in {
            PaperOrderState.SUBMITTED,
            PaperOrderState.OPEN,
            PaperOrderState.PARTIALLY_FILLED,
        }
    )
    if sum(item.product_id == intent.product_id for item in active) >= MAX_OPEN_ORDERS_PER_PRODUCT:
        raise CryptoPaperError("CRYPTO_PAPER_OPEN_ORDER_PRODUCT_CAP")
    if (
        sum(
            item.remaining_base_quantity * (item.limit_price or preview.estimated_execution_price)
            for item in active
        )
        + preview.estimated_quote_quantity
        > MAX_OPEN_NOTIONAL
    ):
        raise CryptoPaperError("CRYPTO_PAPER_AGGREGATE_OPEN_NOTIONAL_CAP")
    reserved_asset = "USD" if intent.side is PaperSide.BUY else contract.base_asset
    reserved_amount = (
        preview.estimated_total_cost
        if intent.side is PaperSide.BUY
        else preview.estimated_base_quantity
    )
    if intent.side is PaperSide.BUY and account_state.available_cash < reserved_amount:
        raise CryptoPaperError("CRYPTO_PAPER_CASH_INSUFFICIENT")
    if (
        intent.side is PaperSide.SELL
        and account_state.balance(contract.base_asset).available < reserved_amount
    ):
        raise CryptoPaperError("CRYPTO_PAPER_ASSET_BALANCE_INSUFFICIENT")
    order_values = (
        _id(order_id, "CRYPTO_PAPER_ORDER_ID_INVALID"),
        intent.account_id,
        intent.intent_id,
        preview.preview_id,
        intent.authorization_id,
        intent.product_id,
        contract.base_asset,
        contract.quote_asset,
        intent.side,
        intent.order_type,
        preview.estimated_base_quantity,
        intent.limit_price,
        Decimal(0),
        Decimal(0),
        Decimal(0),
        PaperOrderState.OPEN,
        now,
        expiry,
        2,
        "PAPER_ONLY",
        "NONE",
    )
    order = CryptoPaperOrder(*order_values[:-2], content_digest(order_values), *order_values[-2:])
    reservation_values = (
        _id(reservation_id, "CRYPTO_PAPER_RESERVATION_ID_INVALID"),
        intent.account_id,
        order.order_id,
        reserved_asset,
        reserved_amount,
        reserved_amount,
        Decimal(0),
        PaperReservationState.HELD,
        1,
        "PAPER_ONLY",
    )
    reservation = CryptoPaperReservation(
        *reservation_values[:-1], content_digest(reservation_values), *reservation_values[-1:]
    )
    balances = list(account_state.balances)
    if reserved_asset == "USD":
        next_reserved_cash = account_state.reserved_cash + reserved_amount
    else:
        next_reserved_cash = account_state.reserved_cash
        existing = account_state.balance(reserved_asset)
        balances = [item for item in balances if item.asset != reserved_asset]
        balances.append(
            PaperAssetBalance(existing.asset, existing.total, existing.reserved + reserved_amount)
        )
    submitted_event = _order_event(
        order_id=order.order_id,
        sequence=1,
        prior=None,
        next_state=PaperOrderState.SUBMITTED,
        actor=actor,
        occurred_at=now,
        evidence_ids=(preview.preview_id,),
        reason="INTERNAL_PAPER_SUBMISSION_ZERO_PROVIDER_REQUESTS",
    )
    open_event = _order_event(
        order_id=order.order_id,
        sequence=2,
        prior=PaperOrderState.SUBMITTED,
        next_state=PaperOrderState.OPEN,
        actor=actor,
        occurred_at=now,
        evidence_ids=(reservation.reservation_id,),
        reason="PAPER_RESERVATION_HELD",
    )
    next_state = _account_state(
        prior=account_state,
        cash=account_state.cash,
        reserved_cash=next_reserved_cash,
        balances=tuple(sorted(balances, key=lambda item: item.asset)),
        realized_pnl=account_state.realized_paper_pnl,
        fees=account_state.modeled_fees,
        updated_at=now,
        event_id=open_event.event_id,
    )
    return CryptoPaperSubmission(order, reservation, next_state, (submitted_event, open_event))


def _order_event(
    *,
    order_id: str,
    sequence: int,
    prior: PaperOrderState | None,
    next_state: PaperOrderState,
    actor: str,
    occurred_at: datetime,
    evidence_ids: tuple[str, ...],
    reason: str,
    base_quantity: Decimal = Decimal(0),
    quote_quantity: Decimal = Decimal(0),
    cash_effect: Decimal = Decimal(0),
    fee_effect: Decimal = Decimal(0),
) -> CryptoPaperOrderEvent:
    values = (
        f"{order_id}-event-{sequence}",
        order_id,
        sequence,
        prior,
        next_state,
        _id(actor, "CRYPTO_PAPER_EVENT_ACTOR_INVALID"),
        _time(occurred_at, "CRYPTO_PAPER_EVENT_TIME_INVALID"),
        tuple(sorted(set(evidence_ids))),
        base_quantity,
        quote_quantity,
        cash_effect,
        fee_effect,
        _id(reason, "CRYPTO_PAPER_EVENT_REASON_INVALID"),
        "PAPER_ONLY",
    )
    return CryptoPaperOrderEvent(*values[:-1], content_digest(values), *values[-1:])


def _account_state(
    *,
    prior: CryptoPaperAccountState,
    cash: Decimal,
    reserved_cash: Decimal,
    balances: tuple[PaperAssetBalance, ...],
    realized_pnl: Decimal,
    fees: Decimal,
    updated_at: datetime,
    event_id: str,
) -> CryptoPaperAccountState:
    if cash < 0 or reserved_cash < 0 or reserved_cash > cash:
        raise CryptoPaperError("CRYPTO_PAPER_ACCOUNT_CASH_INVARIANT")
    if any(item.total < 0 or item.reserved < 0 or item.reserved > item.total for item in balances):
        raise CryptoPaperError("CRYPTO_PAPER_ACCOUNT_ASSET_INVARIANT")
    values = (
        prior.account_id,
        prior.version + 1,
        cash,
        reserved_cash,
        balances,
        realized_pnl,
        fees,
        _time(updated_at, "CRYPTO_PAPER_ACCOUNT_UPDATE_TIME_INVALID"),
        event_id,
        "PAPER_ONLY",
    )
    return CryptoPaperAccountState(*values[:-1], content_digest(values), *values[-1:])


@dataclass(frozen=True, slots=True)
class CryptoPaperFill:
    fill_id: str
    order_id: str
    product_id: str
    side: PaperSide
    price: Decimal
    base_quantity: Decimal
    quote_quantity: Decimal
    fee: Decimal
    slippage: Decimal
    evidence_id: str
    event_time: datetime
    ingestion_time: datetime
    canonical_hash: str
    authority: str = "PAPER_ONLY"


@dataclass(frozen=True, slots=True)
class CryptoPaperPosition:
    position_id: str
    account_id: str
    product_id: str
    base_asset: str
    base_quantity: Decimal
    average_cost: Decimal
    cumulative_cost: Decimal
    cumulative_sale_proceeds: Decimal
    realized_paper_pnl: Decimal
    modeled_fees: Decimal
    state: str
    version: int
    canonical_hash: str
    authority: str = "PAPER_ONLY"


@dataclass(frozen=True, slots=True)
class CryptoPaperPosting:
    account_code: str
    direction: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class CryptoPaperJournal:
    journal_id: str
    source_fill_id: str
    account_id: str
    currency: str
    postings: tuple[CryptoPaperPosting, ...]
    occurred_at: datetime
    canonical_hash: str
    authority: str = "PAPER_ONLY"


# ---------------------------------------------------------------------------
# Ledger contract (Crypto PAPER Ledger Close Accounting Integrity v1)
# ---------------------------------------------------------------------------
#
# The balanced PAPER journal follows the same contract as Apex's core ledger
# and the Securities PAPER ledger (docs/ARCHITECTURE.md "Ledger authority"):
#
# * PAPER_CASH -- cash movement (BUY credits quote + fee; SELL debits
#   quote - fee).
# * PAPER_CRYPTO_ASSET_COST -- fee-EXCLUSIVE consideration of the open
#   position: a BUY debits its quote quantity; a SELL credits the
#   consideration it relieves (proportional, rounded up to the satoshi, on a
#   partial SELL; the exact remainder on the SELL that flattens the product).
# * PAPER_MODELED_FEE_EXPENSE -- every BUY and SELL fee, each exactly once,
#   when the fill that incurs it posts.
# * PAPER_REALIZED_PNL -- GROSS trading P&L: sale consideration minus the
#   consideration it relieves. Net economic income is realized P&L minus fee
#   expense.
#
# The position/account model deliberately differs: ``CryptoPaperPosition.
# cumulative_cost`` capitalizes the BUY fee into a fee-inclusive basis and
# ``realized_paper_pnl`` is net of both fees. The two views reconcile by the
# identity (enforced by ``ledger_contract_discrepancies``):
#
#   ledger ASSET_COST(product) == fee-exclusive consideration held (from fills)
#   ledger PNL - ledger FEES   == account.realized_paper_pnl
#                                 - (sum(position.cumulative_cost)
#                                    - sum(consideration held))
#
# i.e. ledger net income equals account realized P&L minus the BUY fees still
# capitalized in open positions (zero once every position is flat).
#
# Before this milestone a SELL credited ASSET_COST with the position's
# fee-INCLUSIVE basis, so every BUY fee was recognized twice in ledger P&L
# (once as fee expense, once inside the relieved basis) and a flat position
# left a negative ASSET_COST residue.

LEDGER_CASH = "PAPER_CASH"
LEDGER_ASSET_COST = "PAPER_CRYPTO_ASSET_COST"
LEDGER_FEE_EXPENSE = "PAPER_MODELED_FEE_EXPENSE"
LEDGER_REALIZED_PNL = "PAPER_REALIZED_PNL"
LEDGER_ACCOUNT_CODES = frozenset(
    {LEDGER_CASH, LEDGER_ASSET_COST, LEDGER_FEE_EXPENSE, LEDGER_REALIZED_PNL}
)
_SATOSHI = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class CryptoPaperLedgerConsideration:
    """Fee-exclusive consideration of one product's position, derived only
    from its retained fills (never from journals, so a defective journal can
    never propagate into later relief)."""

    product_id: str
    base_quantity: Decimal
    consideration: Decimal


def ledger_consideration_relief(
    *, consideration: Decimal, base_quantity: Decimal, sold_base_quantity: Decimal
) -> Decimal:
    """Fee-exclusive consideration a SELL of ``sold_base_quantity`` relieves:
    the exact remainder when it flattens the position, otherwise the
    proportional share rounded up to the satoshi (the module's existing
    conservative direction for relieved cost). Never exceeds ``consideration``."""

    if sold_base_quantity <= 0 or sold_base_quantity > base_quantity:
        raise CryptoPaperError("CRYPTO_PAPER_POSITION_INSUFFICIENT")
    if sold_base_quantity == base_quantity:
        return consideration
    return _ceil(consideration * sold_base_quantity / base_quantity, _SATOSHI)


def product_ledger_consideration(
    *, product_id: str, fills: tuple[CryptoPaperFill, ...]
) -> CryptoPaperLedgerConsideration:
    """Replay ``fills`` (in retained, application order) for one product."""

    quantity = Decimal(0)
    consideration = Decimal(0)
    for fill in fills:
        if fill.product_id != product_id:
            continue
        if fill.side is PaperSide.BUY:
            quantity += fill.base_quantity
            consideration += fill.quote_quantity
        else:
            relief = ledger_consideration_relief(
                consideration=consideration,
                base_quantity=quantity,
                sold_base_quantity=fill.base_quantity,
            )
            quantity -= fill.base_quantity
            consideration -= relief
    return CryptoPaperLedgerConsideration(product_id, quantity, consideration)


def primary_fill_journal_id(fill_id: str) -> str:
    return f"{fill_id}-journal"


def _journal(
    *,
    fill: CryptoPaperFill,
    account_id: str,
    asset_cost_relief: Decimal,
) -> CryptoPaperJournal:
    postings: tuple[CryptoPaperPosting, ...]
    if fill.side is PaperSide.BUY:
        postings = (
            CryptoPaperPosting(LEDGER_ASSET_COST, "DEBIT", fill.quote_quantity),
            CryptoPaperPosting(LEDGER_FEE_EXPENSE, "DEBIT", fill.fee),
            CryptoPaperPosting(LEDGER_CASH, "CREDIT", fill.quote_quantity + fill.fee),
        )
    else:
        gross_result = fill.quote_quantity - asset_cost_relief
        postings_list = [
            CryptoPaperPosting(LEDGER_CASH, "DEBIT", fill.quote_quantity - fill.fee),
            CryptoPaperPosting(LEDGER_FEE_EXPENSE, "DEBIT", fill.fee),
            CryptoPaperPosting(LEDGER_ASSET_COST, "CREDIT", asset_cost_relief),
        ]
        if gross_result >= 0:
            postings_list.append(CryptoPaperPosting(LEDGER_REALIZED_PNL, "CREDIT", gross_result))
        else:
            postings_list.append(CryptoPaperPosting(LEDGER_REALIZED_PNL, "DEBIT", -gross_result))
        postings = tuple(postings_list)
    return _sealed_journal(
        journal_id=primary_fill_journal_id(fill.fill_id),
        source_fill_id=fill.fill_id,
        account_id=account_id,
        postings=postings,
        occurred_at=fill.event_time,
    )


def _sealed_journal(
    *,
    journal_id: str,
    source_fill_id: str,
    account_id: str,
    postings: tuple[CryptoPaperPosting, ...],
    occurred_at: datetime,
) -> CryptoPaperJournal:
    debits = sum(item.amount for item in postings if item.direction == "DEBIT")
    credits = sum(item.amount for item in postings if item.direction == "CREDIT")
    if debits != credits:
        raise CryptoPaperError("CRYPTO_PAPER_JOURNAL_UNBALANCED")
    values = (
        journal_id,
        source_fill_id,
        account_id,
        "USD",
        postings,
        occurred_at,
        "PAPER_ONLY",
    )
    return CryptoPaperJournal(*values[:-1], content_digest(values), *values[-1:])


@dataclass(frozen=True, slots=True)
class CryptoPaperFillApplication:
    order: CryptoPaperOrder
    reservation: CryptoPaperReservation
    account_state: CryptoPaperAccountState
    position: CryptoPaperPosition
    fill: CryptoPaperFill
    event: CryptoPaperOrderEvent
    journal: CryptoPaperJournal


def apply_retained_evidence(
    *,
    fill_id: str,
    order: CryptoPaperOrder,
    contract: CryptoPaperProviderContract,
    reservation: CryptoPaperReservation,
    account_state: CryptoPaperAccountState,
    position: CryptoPaperPosition | None,
    evidence: RetainedPaperPriceEvidence,
    fee_bps: DecimalInput,
    slippage_bps: DecimalInput,
    actor: str,
    prior_fills: tuple[CryptoPaperFill, ...],
) -> CryptoPaperFillApplication | None:
    """``prior_fills`` is the account's full retained fill history (in
    application order) before this fill; the SELL journal relieves ledger
    consideration derived from it (see "Ledger contract" above)."""

    if order.state not in {PaperOrderState.OPEN, PaperOrderState.PARTIALLY_FILLED}:
        raise CryptoPaperError("CRYPTO_PAPER_ORDER_NOT_FILLABLE")
    if any(item.fill_id == fill_id for item in prior_fills):
        raise CryptoPaperError("CRYPTO_PAPER_DUPLICATE_FILL")
    held = product_ledger_consideration(product_id=order.product_id, fills=prior_fills)
    if held.base_quantity != (Decimal(0) if position is None else position.base_quantity):
        raise CryptoPaperError("CRYPTO_PAPER_POSITION_FILL_HISTORY_MISMATCH")
    if evidence.product_id != order.product_id:
        raise CryptoPaperError("CRYPTO_PAPER_FILL_EVIDENCE_PRODUCT_MISMATCH")
    if contract.product_id != order.product_id:
        raise CryptoPaperError("CRYPTO_PAPER_FILL_CONTRACT_MISMATCH")
    if evidence.ingested_at <= order.submitted_at or evidence.observed_at <= order.submitted_at:
        raise CryptoPaperError("CRYPTO_PAPER_NEXT_EVIDENCE_REQUIRED")
    if evidence.observed_at >= order.valid_until:
        raise CryptoPaperError("CRYPTO_PAPER_ORDER_VALIDITY_ELAPSED")
    if order.order_type is PaperOrderType.LIMIT:
        if order.side is PaperSide.BUY and evidence.price > (order.limit_price or Decimal(0)):
            return None
        if order.side is PaperSide.SELL and evidence.price < (order.limit_price or Decimal(0)):
            return None
        execution_price = order.limit_price or evidence.price
        slippage = Decimal(0)
    else:
        rate = _decimal(slippage_bps, "CRYPTO_PAPER_SLIPPAGE_INVALID") / Decimal(10000)
        multiplier = Decimal(1) + rate if order.side is PaperSide.BUY else Decimal(1) - rate
        execution_price = (
            _ceil(evidence.price * multiplier, contract.price_increment)
            if order.side is PaperSide.BUY
            else _floor(evidence.price * multiplier, contract.price_increment)
        )
        slippage = abs(execution_price - evidence.price)
    base = order.remaining_base_quantity
    if evidence.available_base_quantity is not None:
        base = min(
            base,
            _floor(evidence.available_base_quantity, contract.base_increment),
        )
    if base <= 0:
        return None
    quote = _floor(base * execution_price, contract.quote_increment)
    fee_rate = _decimal(fee_bps, "CRYPTO_PAPER_FEE_INVALID") / Decimal(10000)
    fee = _floor(quote * fee_rate, contract.quote_increment)
    fill_values = (
        _id(fill_id, "CRYPTO_PAPER_FILL_ID_INVALID"),
        order.order_id,
        order.product_id,
        order.side,
        execution_price,
        base,
        quote,
        fee,
        slippage,
        evidence.evidence_id,
        evidence.observed_at,
        evidence.ingested_at,
        "PAPER_ONLY",
    )
    fill = CryptoPaperFill(*fill_values[:-1], content_digest(fill_values), *fill_values[-1:])
    filled_base = order.filled_base_quantity + base
    terminal = filled_base == order.target_base_quantity
    state = PaperOrderState.FILLED if terminal else PaperOrderState.PARTIALLY_FILLED
    next_order_values = (
        order.order_id,
        order.account_id,
        order.intent_id,
        order.preview_id,
        order.authorization_id,
        order.product_id,
        order.base_asset,
        order.quote_asset,
        order.side,
        order.order_type,
        order.target_base_quantity,
        order.limit_price,
        filled_base,
        order.filled_quote_quantity + quote,
        order.cumulative_fees + fee,
        state,
        order.submitted_at,
        order.valid_until,
        order.last_sequence + 1,
        "PAPER_ONLY",
        "NONE",
    )
    next_order = CryptoPaperOrder(
        *next_order_values[:-2], content_digest(next_order_values), *next_order_values[-2:]
    )
    consumed = quote + fee if order.side is PaperSide.BUY else base
    remaining = reservation.remaining_amount - consumed
    if remaining < 0 and abs(remaining) <= Decimal("0.00000001"):
        remaining = Decimal(0)
    if remaining < 0:
        raise CryptoPaperError("CRYPTO_PAPER_RESERVATION_OVERCONSUMED")
    if terminal:
        released = remaining
        remaining = Decimal(0)
        reservation_state = (
            PaperReservationState.CONSUMED if released == 0 else PaperReservationState.RELEASED
        )
    else:
        reservation_state = PaperReservationState.PARTIALLY_CONSUMED
        released = Decimal(0)
    next_reservation_values = (
        reservation.reservation_id,
        reservation.account_id,
        reservation.order_id,
        reservation.asset,
        reservation.original_amount,
        remaining,
        reservation.consumed_amount + consumed,
        reservation_state,
        reservation.version + 1,
        "PAPER_ONLY",
    )
    next_reservation = CryptoPaperReservation(
        *next_reservation_values[:-1],
        content_digest(next_reservation_values),
        *next_reservation_values[-1:],
    )
    balances = list(account_state.balances)
    balance = account_state.balance(order.base_asset)
    balances = [item for item in balances if item.asset != order.base_asset]
    if order.side is PaperSide.BUY:
        next_cash = account_state.cash - quote - fee
        next_reserved_cash = account_state.reserved_cash - consumed - released
        next_balance = PaperAssetBalance(order.base_asset, balance.total + base, balance.reserved)
        prior_cost = Decimal(0) if position is None else position.cumulative_cost
        prior_qty = Decimal(0) if position is None else position.base_quantity
        cost = quote + fee
        next_qty = prior_qty + base
        cumulative_cost = prior_cost + cost
        average_cost = _floor(cumulative_cost / next_qty, Decimal("0.00000001"))
        position_values = (
            f"{order.account_id}-{order.product_id}-position",
            order.account_id,
            order.product_id,
            order.base_asset,
            next_qty,
            average_cost,
            cumulative_cost,
            Decimal(0) if position is None else position.cumulative_sale_proceeds,
            Decimal(0) if position is None else position.realized_paper_pnl,
            (Decimal(0) if position is None else position.modeled_fees) + fee,
            "OPEN",
            1 if position is None else position.version + 1,
            "PAPER_ONLY",
        )
        next_position = CryptoPaperPosition(
            *position_values[:-1], content_digest(position_values), *position_values[-1:]
        )
        realized = Decimal(0)
        next_realized = account_state.realized_paper_pnl
        asset_cost_relief = Decimal(0)
    else:
        if position is None or position.base_quantity < base:
            raise CryptoPaperError("CRYPTO_PAPER_POSITION_INSUFFICIENT")
        # Repeatable Strategy PAPER Evidence v1: position.average_cost is
        # itself already floor-quantized to 8 places (below, "cumulative_
        # cost / next_qty"), so multiplying it by `base` (also up to 8
        # places -- domain.crypto_universe's own base_increment) is a
        # genuine Decimal scale-additive operation that can produce up to
        # 16 decimal places -- silently exceeding domain.values.MAX_SCALE
        # (8) the moment this value (or anything derived from it: realized,
        # cumulative_cost - cost_basis, the journal) is later canonically
        # hashed. No production SELL had ever exercised this branch before
        # this milestone (domain.crypto_paper's own SELL support was fully
        # implemented but never actually called -- see docs/REPEATABLE_
        # STRATEGY_PAPER_EVIDENCE_V1.md), so this pre-existing precision gap
        # was latent, not previously reachable. _ceil (this module's own
        # existing helper, already used for BUY-side execution pricing)
        # rounds the cost basis being removed UP -- the same "unfavorable
        # to the trader" conservative direction build_paper_preview already
        # applies to BUY/SELL execution prices -- rather than a fresh,
        # separately-invented rounding convention.
        #
        # Crypto PAPER Ledger Close Accounting Integrity v1: the SELL that
        # flattens the position relieves the exact remaining cumulative cost,
        # so rounded partial reliefs can never leave a basis
        # residue on a CLOSED position or leak that dust into
        # realized P&L. Partial SELLs are unchanged.
        cost_basis = (
            position.cumulative_cost
            if base == position.base_quantity
            else _ceil(position.average_cost * base, Decimal("0.00000001"))
        )
        net_proceeds = quote - fee
        realized = net_proceeds - cost_basis
        asset_cost_relief = ledger_consideration_relief(
            consideration=held.consideration,
            base_quantity=held.base_quantity,
            sold_base_quantity=base,
        )
        next_cash = account_state.cash + net_proceeds
        next_reserved_cash = account_state.reserved_cash
        next_balance = PaperAssetBalance(
            order.base_asset,
            balance.total - base,
            balance.reserved - consumed - released,
        )
        next_qty = position.base_quantity - base
        position_values = (
            position.position_id,
            position.account_id,
            position.product_id,
            position.base_asset,
            next_qty,
            Decimal(0) if next_qty == 0 else position.average_cost,
            position.cumulative_cost - cost_basis,
            position.cumulative_sale_proceeds + net_proceeds,
            position.realized_paper_pnl + realized,
            position.modeled_fees + fee,
            "CLOSED" if next_qty == 0 else "OPEN",
            position.version + 1,
            "PAPER_ONLY",
        )
        next_position = CryptoPaperPosition(
            *position_values[:-1], content_digest(position_values), *position_values[-1:]
        )
        next_realized = account_state.realized_paper_pnl + realized
    balances.append(next_balance)
    event = _order_event(
        order_id=order.order_id,
        sequence=next_order.last_sequence,
        prior=order.state,
        next_state=state,
        actor=actor,
        occurred_at=evidence.ingested_at,
        evidence_ids=(fill.fill_id, evidence.evidence_id),
        reason="RETAINED_POST_SUBMISSION_EVIDENCE_APPLIED",
        base_quantity=base,
        quote_quantity=quote,
        cash_effect=(-quote - fee if order.side is PaperSide.BUY else quote - fee),
        fee_effect=fee,
    )
    next_account = _account_state(
        prior=account_state,
        cash=next_cash,
        reserved_cash=next_reserved_cash,
        balances=tuple(sorted(balances, key=lambda item: item.asset)),
        realized_pnl=next_realized,
        fees=account_state.modeled_fees + fee,
        updated_at=evidence.ingested_at,
        event_id=event.event_id,
    )
    journal = _journal(
        fill=fill,
        account_id=order.account_id,
        asset_cost_relief=asset_cost_relief,
    )
    return CryptoPaperFillApplication(
        next_order,
        next_reservation,
        next_account,
        next_position,
        fill,
        event,
        journal,
    )


@dataclass(frozen=True, slots=True)
class CryptoPaperPositionMark:
    """A read-only, unpersisted valuation of one open Position at a single
    point-in-time mark price.

    PAPER Operations & Observability v1: never written back to
    ``CryptoPaperState``/``CryptoPaperPosition``, never posted to the
    balanced double-entry journal (``_journal``) -- purely a derived
    read-model quantity for an operator surface. ``authority`` is always
    ``NONE``: this creates no PAPER accounting fact, it only interprets an
    already-retained Position against one additional, already-point-in-
    time-bounded market observation the caller supplies.
    """

    position_id: str
    product_id: str
    base_quantity: Decimal
    mark_price: Decimal
    marked_at: datetime
    market_value: Decimal
    unrealized_paper_pnl: Decimal
    canonical_hash: str
    authority: str = "NONE"


def estimate_open_position_mark(
    *,
    position: CryptoPaperPosition,
    mark_price: DecimalInput,
    marked_at: datetime,
) -> CryptoPaperPositionMark:
    """Pure mark-to-market of one currently OPEN Position.

    Deliberately mirrors ``domain.securities_paper.mark_position``'s exact
    formula (``market_value - average_cost * quantity``), but unlike that
    function never mutates or replaces the Position: crypto_paper's own
    balanced double-entry journal is the sole source of persisted PAPER
    accounting truth, and this function must never become a second one --
    it exists only to answer "what would this open Position be worth right
    now", never to record that anywhere.

    The caller is responsible for supplying a ``mark_price`` already
    obtained through a point-in-time-bounded lookup (Live Observation
    Point-in-Time Race Fix v1 -- ``application.live_market_observation.
    ObtainContemporaneousMarketObservation``, ``as_of``-bounded, never an
    unbounded "latest" query) and for never calling this on a STALE/ABSENT
    ``ContemporaneousObservationDisposition``; this function does not
    re-check staleness itself, only that ``marked_at`` is a valid aware
    timestamp and that ``position`` is genuinely still OPEN.
    """

    if position.state != "OPEN":
        raise CryptoPaperError("CRYPTO_PAPER_POSITION_NOT_OPEN")
    when = _time(marked_at, "CRYPTO_PAPER_MARK_TIME_INVALID")
    price = _decimal(mark_price, "CRYPTO_PAPER_MARK_PRICE_INVALID", positive=True)
    # A raw Decimal product's scale is the SUM of its operands' scales (an
    # 8-decimal price times an 8-decimal quantity can reach 16), exceeding
    # apex.domain.values.MAX_SCALE (8) -- the same overflow this file's own
    # apply_retained_evidence already avoids for average_cost via _floor(...,
    # Decimal("0.00000001")). Quantized here identically, before content_
    # digest (which re-validates every field through that same MAX_SCALE
    # bound and would otherwise raise ValueValidationError on genuine
    # production price/quantity precision -- caught by this milestone's own
    # runtime acceptance against real retained state, not a synthetic test.
    market_value = _floor(price * position.base_quantity, Decimal("0.00000001"))
    cost_basis = _floor(position.average_cost * position.base_quantity, Decimal("0.00000001"))
    unrealized = market_value - cost_basis
    values = (
        position.position_id,
        position.product_id,
        position.base_quantity,
        price,
        when,
        market_value,
        unrealized,
        "NONE",
    )
    return CryptoPaperPositionMark(*values[:-1], content_digest(values), *values[-1:])


_TERMINAL_DEAD_ORDER_STATES = frozenset(
    {PaperOrderState.REJECTED, PaperOrderState.CANCELED, PaperOrderState.EXPIRED}
)


def open_trial_orders_by_strategy(
    *,
    positions: Sequence[CryptoPaperPosition],
    orders: Sequence[CryptoPaperOrder],
    authorizations: Sequence[CryptoPaperAuthorization],
) -> dict[str, CryptoPaperOrder]:
    """Pure read-model projection: ``strategy_or_discovery_id -> the Order
    that currently justifies calling that strategy's trial "open"``.

    PAPER Operations & Observability v1. "Has an open trial" means an OPEN
    Position exists for that strategy's instrument, never that its
    originating Order is itself still unfilled -- a market order
    transitions OPEN -> FILLED within one cycle by design, but the
    Position it created can (and, for STRATEGY_PAPER_TRIAL_EXECUTION's
    buy-and-hold-style benchmarks, typically does) stay open for a long
    time afterward. Filtering by ``order.state in {OPEN, PARTIALLY_
    FILLED}`` alone (an earlier version of this function did) silently
    drops every trial the moment its order fills -- caught only by this
    milestone's own runtime acceptance against real retained production
    state, not a synthetic test (every hand-built fixture used a
    still-OPEN order). A REJECTED/CANCELED/EXPIRED order is never
    returned even if its ``product_id`` happens to coincide with an
    unrelated open Position on the same instrument.
    """

    open_position_product_ids = {
        position.product_id for position in positions if position.state == "OPEN"
    }
    authorization_strategy_by_id = {
        authorization.authorization_id: authorization.strategy_or_discovery_id
        for authorization in authorizations
    }
    result: dict[str, CryptoPaperOrder] = {}
    for order in orders:
        if order.product_id not in open_position_product_ids:
            continue
        if order.state in _TERMINAL_DEAD_ORDER_STATES:
            continue
        strategy_id = authorization_strategy_by_id.get(order.authorization_id)
        if strategy_id is not None:
            result[strategy_id] = order
    return result


@dataclass(frozen=True, slots=True)
class CryptoPaperCancellation:
    order: CryptoPaperOrder
    reservation: CryptoPaperReservation
    account_state: CryptoPaperAccountState
    events: tuple[CryptoPaperOrderEvent, ...]


def cancel_paper_order(
    *,
    order: CryptoPaperOrder,
    reservation: CryptoPaperReservation,
    account_state: CryptoPaperAccountState,
    canceled_at: datetime,
    actor: str,
    reason: str,
) -> CryptoPaperCancellation:
    when = _time(canceled_at, "CRYPTO_PAPER_CANCEL_TIME_INVALID")
    if order.state not in {PaperOrderState.OPEN, PaperOrderState.PARTIALLY_FILLED}:
        raise CryptoPaperError("CRYPTO_PAPER_ORDER_NOT_CANCELABLE")
    request_event = _order_event(
        order_id=order.order_id,
        sequence=order.last_sequence + 1,
        prior=order.state,
        next_state=PaperOrderState.CANCEL_REQUESTED,
        actor=actor,
        occurred_at=when,
        evidence_ids=(reservation.reservation_id,),
        reason="INTERNAL_PAPER_CANCEL_ZERO_PROVIDER_REQUESTS",
    )
    canceled_event = _order_event(
        order_id=order.order_id,
        sequence=order.last_sequence + 2,
        prior=PaperOrderState.CANCEL_REQUESTED,
        next_state=PaperOrderState.CANCELED,
        actor=actor,
        occurred_at=when,
        evidence_ids=(reservation.reservation_id,),
        reason=reason,
    )
    order_values = (
        order.order_id,
        order.account_id,
        order.intent_id,
        order.preview_id,
        order.authorization_id,
        order.product_id,
        order.base_asset,
        order.quote_asset,
        order.side,
        order.order_type,
        order.target_base_quantity,
        order.limit_price,
        order.filled_base_quantity,
        order.filled_quote_quantity,
        order.cumulative_fees,
        PaperOrderState.CANCELED,
        order.submitted_at,
        order.valid_until,
        canceled_event.sequence,
        "PAPER_ONLY",
        "NONE",
    )
    next_order = CryptoPaperOrder(
        *order_values[:-2], content_digest(order_values), *order_values[-2:]
    )
    released = reservation.remaining_amount
    reservation_values = (
        reservation.reservation_id,
        reservation.account_id,
        reservation.order_id,
        reservation.asset,
        reservation.original_amount,
        Decimal(0),
        reservation.consumed_amount,
        PaperReservationState.RELEASED,
        reservation.version + 1,
        "PAPER_ONLY",
    )
    next_reservation = CryptoPaperReservation(
        *reservation_values[:-1], content_digest(reservation_values), *reservation_values[-1:]
    )
    balances = list(account_state.balances)
    if reservation.asset == "USD":
        reserved_cash = account_state.reserved_cash - released
    else:
        reserved_cash = account_state.reserved_cash
        balance = account_state.balance(reservation.asset)
        balances = [item for item in balances if item.asset != reservation.asset]
        balances.append(
            PaperAssetBalance(
                balance.asset,
                balance.total,
                balance.reserved - released,
            )
        )
    next_account = _account_state(
        prior=account_state,
        cash=account_state.cash,
        reserved_cash=reserved_cash,
        balances=tuple(sorted(balances, key=lambda item: item.asset)),
        realized_pnl=account_state.realized_paper_pnl,
        fees=account_state.modeled_fees,
        updated_at=when,
        event_id=canceled_event.event_id,
    )
    return CryptoPaperCancellation(
        next_order,
        next_reservation,
        next_account,
        (request_event, canceled_event),
    )


def expire_paper_order(
    *,
    order: CryptoPaperOrder,
    reservation: CryptoPaperReservation,
    account_state: CryptoPaperAccountState,
    expired_at: datetime,
    actor: str,
) -> CryptoPaperCancellation:
    when = _time(expired_at, "CRYPTO_PAPER_EXPIRY_TIME_INVALID")
    if order.state not in {PaperOrderState.OPEN, PaperOrderState.PARTIALLY_FILLED}:
        raise CryptoPaperError("CRYPTO_PAPER_ORDER_NOT_EXPIRABLE")
    if when < order.valid_until:
        raise CryptoPaperError("CRYPTO_PAPER_ORDER_NOT_YET_EXPIRED")
    event = _order_event(
        order_id=order.order_id,
        sequence=order.last_sequence + 1,
        prior=order.state,
        next_state=PaperOrderState.EXPIRED,
        actor=actor,
        occurred_at=when,
        evidence_ids=(reservation.reservation_id,),
        reason="INTERNAL_PAPER_ORDER_VALIDITY_ELAPSED",
    )
    order_values = (
        order.order_id,
        order.account_id,
        order.intent_id,
        order.preview_id,
        order.authorization_id,
        order.product_id,
        order.base_asset,
        order.quote_asset,
        order.side,
        order.order_type,
        order.target_base_quantity,
        order.limit_price,
        order.filled_base_quantity,
        order.filled_quote_quantity,
        order.cumulative_fees,
        PaperOrderState.EXPIRED,
        order.submitted_at,
        order.valid_until,
        event.sequence,
        "PAPER_ONLY",
        "NONE",
    )
    next_order = CryptoPaperOrder(
        *order_values[:-2], content_digest(order_values), *order_values[-2:]
    )
    released = reservation.remaining_amount
    reservation_values = (
        reservation.reservation_id,
        reservation.account_id,
        reservation.order_id,
        reservation.asset,
        reservation.original_amount,
        Decimal(0),
        reservation.consumed_amount,
        PaperReservationState.RELEASED,
        reservation.version + 1,
        "PAPER_ONLY",
    )
    next_reservation = CryptoPaperReservation(
        *reservation_values[:-1],
        content_digest(reservation_values),
        *reservation_values[-1:],
    )
    balances = list(account_state.balances)
    if reservation.asset == "USD":
        reserved_cash = account_state.reserved_cash - released
    else:
        reserved_cash = account_state.reserved_cash
        balance = account_state.balance(reservation.asset)
        balances = [item for item in balances if item.asset != reservation.asset]
        balances.append(
            PaperAssetBalance(
                balance.asset,
                balance.total,
                balance.reserved - released,
            )
        )
    next_account = _account_state(
        prior=account_state,
        cash=account_state.cash,
        reserved_cash=reserved_cash,
        balances=tuple(sorted(balances, key=lambda item: item.asset)),
        realized_pnl=account_state.realized_paper_pnl,
        fees=account_state.modeled_fees,
        updated_at=when,
        event_id=event.event_id,
    )
    return CryptoPaperCancellation(
        next_order,
        next_reservation,
        next_account,
        (event,),
    )


@dataclass(frozen=True, slots=True)
class CryptoPaperReconciliation:
    reconciliation_id: str
    account_id: str
    order_id: str | None
    evaluated_at: datetime
    status: PaperReconciliationStatus
    severity: str
    repairability: str
    discrepancies: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    canonical_hash: str
    authority: str = "PAPER_ONLY"


def reconcile_paper_state(
    *,
    reconciliation_id: str,
    account: CryptoPaperAccount,
    account_state: CryptoPaperAccountState,
    orders: tuple[CryptoPaperOrder, ...],
    reservations: tuple[CryptoPaperReservation, ...],
    fills: tuple[CryptoPaperFill, ...],
    positions: tuple[CryptoPaperPosition, ...],
    journals: tuple[CryptoPaperJournal, ...],
    evaluated_at: datetime,
    order_id: str | None = None,
) -> CryptoPaperReconciliation:
    discrepancies: list[str] = []
    if account_state.cash < 0 or account_state.reserved_cash > account_state.cash:
        discrepancies.append("ACCOUNT_CASH_INVARIANT")
    active_reservations = tuple(
        item
        for item in reservations
        if item.state in {PaperReservationState.HELD, PaperReservationState.PARTIALLY_CONSUMED}
    )
    expected_reserved_cash = sum(
        item.remaining_amount for item in active_reservations if item.asset == "USD"
    )
    if expected_reserved_cash != account_state.reserved_cash:
        discrepancies.append("RESERVED_CASH_MISMATCH")
    for balance in account_state.balances:
        expected = sum(
            item.remaining_amount for item in active_reservations if item.asset == balance.asset
        )
        if expected != balance.reserved:
            discrepancies.append(f"RESERVED_ASSET_MISMATCH:{balance.asset}")
    for order in orders:
        order_fills = tuple(item for item in fills if item.order_id == order.order_id)
        order_reservations = tuple(item for item in reservations if item.order_id == order.order_id)
        if len(order_reservations) != 1:
            discrepancies.append(f"ORDER_RESERVATION_COUNT_MISMATCH:{order.order_id}")
        else:
            reservation = order_reservations[0]
            expected_consumed = sum(
                (
                    item.quote_quantity + item.fee
                    if order.side is PaperSide.BUY
                    else item.base_quantity
                )
                for item in order_fills
            )
            if reservation.consumed_amount != expected_consumed:
                discrepancies.append(f"ORDER_RESERVATION_CONSUMPTION_MISMATCH:{order.order_id}")
            if order.state in {
                PaperOrderState.OPEN,
                PaperOrderState.PARTIALLY_FILLED,
            }:
                if reservation.state not in {
                    PaperReservationState.HELD,
                    PaperReservationState.PARTIALLY_CONSUMED,
                }:
                    discrepancies.append(f"OPEN_ORDER_RESERVATION_STATE_MISMATCH:{order.order_id}")
                if (
                    reservation.original_amount
                    != reservation.consumed_amount + reservation.remaining_amount
                ):
                    discrepancies.append(f"OPEN_ORDER_RESERVATION_AMOUNT_MISMATCH:{order.order_id}")
            elif reservation.remaining_amount != 0:
                discrepancies.append(f"TERMINAL_ORDER_RESERVATION_NOT_RELEASED:{order.order_id}")
        if order.filled_base_quantity > order.target_base_quantity:
            discrepancies.append(f"ORDER_OVERFILLED:{order.order_id}")
        if (
            order.state is PaperOrderState.FILLED
            and order.filled_base_quantity != order.target_base_quantity
        ):
            discrepancies.append(f"ORDER_TERMINAL_QUANTITY_MISMATCH:{order.order_id}")
        if sum(item.base_quantity for item in order_fills) != order.filled_base_quantity:
            discrepancies.append(f"ORDER_FILL_BASE_MISMATCH:{order.order_id}")
        if sum(item.quote_quantity for item in order_fills) != order.filled_quote_quantity:
            discrepancies.append(f"ORDER_FILL_QUOTE_MISMATCH:{order.order_id}")
        if sum(item.fee for item in order_fills) != order.cumulative_fees:
            discrepancies.append(f"ORDER_FILL_FEE_MISMATCH:{order.order_id}")
    for journal in journals:
        debits = sum(item.amount for item in journal.postings if item.direction == "DEBIT")
        credits = sum(item.amount for item in journal.postings if item.direction == "CREDIT")
        if debits != credits:
            discrepancies.append(f"UNBALANCED_JOURNAL:{journal.journal_id}")
    for fill in fills:
        primary_id = primary_fill_journal_id(fill.fill_id)
        if sum(item.journal_id == primary_id for item in journals) != 1:
            discrepancies.append(f"FILL_JOURNAL_COUNT_MISMATCH:{fill.fill_id}")
    if sum(item.fee for item in fills) != account_state.modeled_fees:
        discrepancies.append("ACCOUNT_FEE_MISMATCH")
    if sum(item.realized_paper_pnl for item in positions) != (account_state.realized_paper_pnl):
        discrepancies.append("ACCOUNT_REALIZED_PNL_MISMATCH")
    cash_from_journals = account.opening_cash
    for journal in journals:
        cash_from_journals += sum(
            item.amount
            for item in journal.postings
            if item.account_code == "PAPER_CASH" and item.direction == "DEBIT"
        )
        cash_from_journals -= sum(
            item.amount
            for item in journal.postings
            if item.account_code == "PAPER_CASH" and item.direction == "CREDIT"
        )
    if cash_from_journals != account_state.cash:
        discrepancies.append("ACCOUNT_CASH_JOURNAL_MISMATCH")
    for asset in {item.base_asset for item in positions} | {
        item.asset for item in account_state.balances
    }:
        position_quantity = sum(
            item.base_quantity for item in positions if item.base_asset == asset
        )
        if position_quantity != account_state.balance(asset).total:
            discrepancies.append(f"POSITION_BALANCE_MISMATCH:{asset}")
    discrepancies.extend(
        ledger_contract_discrepancies(
            account_state=account_state,
            fills=fills,
            positions=positions,
            journals=journals,
        )
    )
    status = (
        PaperReconciliationStatus.CLEAN
        if not discrepancies
        else PaperReconciliationStatus.DISCREPANCY
    )
    evidence_ids = tuple(
        sorted(
            {
                account_state.last_event_id,
                *(item.order_id for item in orders),
                *(item.fill_id for item in fills),
                *(item.journal_id for item in journals),
            }
        )
    )
    values = (
        _id(reconciliation_id, "CRYPTO_PAPER_RECONCILIATION_ID_INVALID"),
        account.account_id,
        order_id,
        _time(evaluated_at, "CRYPTO_PAPER_RECONCILIATION_TIME_INVALID"),
        status,
        "NONE" if status is PaperReconciliationStatus.CLEAN else "ERROR",
        "NO_SILENT_REPAIR",
        tuple(sorted(set(discrepancies))),
        evidence_ids,
        "PAPER_ONLY",
    )
    return CryptoPaperReconciliation(*values[:-1], content_digest(values), *values[-1:])


def ledger_account_balances(journals: tuple[CryptoPaperJournal, ...]) -> dict[str, Decimal]:
    """Signed ledger balances by account code, debit-positive."""

    result: dict[str, Decimal] = {}
    for journal in journals:
        for posting in journal.postings:
            signed = posting.amount if posting.direction == "DEBIT" else -posting.amount
            result[posting.account_code] = result.get(posting.account_code, Decimal(0)) + signed
    return result


def ledger_contract_discrepancies(
    *,
    account_state: CryptoPaperAccountState,
    fills: tuple[CryptoPaperFill, ...],
    positions: tuple[CryptoPaperPosition, ...],
    journals: tuple[CryptoPaperJournal, ...],
) -> tuple[str, ...]:
    """Compares ledger economics with fill/position/account economics (see
    "Ledger contract"). Uses only retained domain rows -- never a console- or
    read-model-derived value."""

    discrepancies: list[str] = []
    fills_by_id = {item.fill_id: item for item in fills}
    asset_cost_by_product: dict[str, Decimal] = {}
    seen_journal_ids: set[str] = set()
    for journal in journals:
        if journal.journal_id in seen_journal_ids:
            discrepancies.append(f"DUPLICATE_JOURNAL:{journal.journal_id}")
            continue
        seen_journal_ids.add(journal.journal_id)
        source = fills_by_id.get(journal.source_fill_id)
        if source is None:
            discrepancies.append(f"JOURNAL_SOURCE_FILL_MISSING:{journal.journal_id}")
            continue
        recognized = journal.journal_id == primary_fill_journal_id(source.fill_id) or (
            source.side is PaperSide.SELL
            and journal.journal_id == close_accounting_correction_journal_id(source.fill_id)
        )
        if not recognized:
            discrepancies.append(f"UNRECOGNIZED_JOURNAL:{journal.journal_id}")
            continue
        for posting in journal.postings:
            if posting.account_code not in LEDGER_ACCOUNT_CODES:
                discrepancies.append(f"UNRECOGNIZED_LEDGER_ACCOUNT:{posting.account_code}")
            elif posting.account_code == LEDGER_ASSET_COST:
                signed = posting.amount if posting.direction == "DEBIT" else -posting.amount
                asset_cost_by_product[source.product_id] = (
                    asset_cost_by_product.get(source.product_id, Decimal(0)) + signed
                )
    consideration_held = Decimal(0)
    products = (
        {item.product_id for item in fills}
        | {item.product_id for item in positions}
        | set(asset_cost_by_product)
    )
    for product_id in sorted(products):
        try:
            held = product_ledger_consideration(product_id=product_id, fills=fills)
        except CryptoPaperError:
            discrepancies.append(f"POSITION_FILL_HISTORY_INVALID:{product_id}")
            continue
        position_quantity = sum(
            (item.base_quantity for item in positions if item.product_id == product_id),
            Decimal(0),
        )
        if held.base_quantity != position_quantity:
            discrepancies.append(f"POSITION_FILL_QUANTITY_MISMATCH:{product_id}")
        ledger_asset_cost = asset_cost_by_product.get(product_id, Decimal(0))
        if ledger_asset_cost != held.consideration:
            discrepancies.append(f"LEDGER_ASSET_COST_MISMATCH:{product_id}")
        if position_quantity == 0 and ledger_asset_cost != 0:
            discrepancies.append(f"FLAT_POSITION_LEDGER_ASSET_COST_RESIDUAL:{product_id}")
        if position_quantity == 0 and any(
            item.cumulative_cost != 0 for item in positions if item.product_id == product_id
        ):
            discrepancies.append(f"FLAT_POSITION_COST_BASIS_RESIDUAL:{product_id}")
        consideration_held += held.consideration
    ledger = ledger_account_balances(journals)
    fee_expense = ledger.get(LEDGER_FEE_EXPENSE, Decimal(0))
    ledger_net_income = -ledger.get(LEDGER_REALIZED_PNL, Decimal(0)) - fee_expense
    capitalized_fees = (
        sum((item.cumulative_cost for item in positions), Decimal(0)) - consideration_held
    )
    if ledger_net_income != account_state.realized_paper_pnl - capitalized_fees:
        discrepancies.append("LEDGER_NET_INCOME_MISMATCH")
    if fee_expense != account_state.modeled_fees:
        discrepancies.append("LEDGER_FEE_EXPENSE_MISMATCH")
    return tuple(discrepancies)


# ---------------------------------------------------------------------------
# Append-only close-accounting correction
# ---------------------------------------------------------------------------
#
# A SELL journal booked before this milestone relieved PAPER_CRYPTO_ASSET_COST
# at the position's fee-inclusive basis. Retained journals are immutable, so
# the only permitted repair is ONE additional, deterministic, balanced journal
# per such SELL fill that reclassifies the over-relieved amount between
# PAPER_CRYPTO_ASSET_COST and PAPER_REALIZED_PNL. It moves no cash and no fee
# and touches no account/position/order/fill row. It is deliberately narrow:
# it can only be built for a SELL fill whose retained primary journal is
# byte-identical to the pre-fix SELL formula, and only for the exact
# difference between that journal's relief and the contract relief.

CLOSE_ACCOUNTING_CORRECTION_POLICY_ID = "CRYPTO_PAPER_CLOSE_ACCOUNTING_CORRECTION_V1"


def close_accounting_correction_journal_id(fill_id: str) -> str:
    return f"{primary_fill_journal_id(fill_id)}-close-accounting-correction-v1"


@dataclass(frozen=True, slots=True)
class CryptoPaperCloseAccountingCorrection:
    policy_id: str
    journal: CryptoPaperJournal
    source_fill_id: str
    source_journal_id: str
    source_journal_hash: str
    booked_asset_cost_relief: Decimal
    contract_asset_cost_relief: Decimal
    restatement: Decimal
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"


def _pre_fix_sell_journal(
    *, fill: CryptoPaperFill, account_id: str, booked_relief: Decimal
) -> CryptoPaperJournal:
    """Frozen replica of the pre-fix SELL journal formula, used only to prove
    a retained journal was produced by it."""

    gross_result = fill.quote_quantity - booked_relief
    postings = [
        CryptoPaperPosting(LEDGER_CASH, "DEBIT", fill.quote_quantity - fill.fee),
        CryptoPaperPosting(LEDGER_FEE_EXPENSE, "DEBIT", fill.fee),
        CryptoPaperPosting(LEDGER_ASSET_COST, "CREDIT", booked_relief),
    ]
    if gross_result >= 0:
        postings.append(CryptoPaperPosting(LEDGER_REALIZED_PNL, "CREDIT", gross_result))
    else:
        postings.append(CryptoPaperPosting(LEDGER_REALIZED_PNL, "DEBIT", -gross_result))
    return _sealed_journal(
        journal_id=primary_fill_journal_id(fill.fill_id),
        source_fill_id=fill.fill_id,
        account_id=account_id,
        postings=tuple(postings),
        occurred_at=fill.event_time,
    )


def build_close_accounting_correction(
    *,
    account_id: str,
    fills: tuple[CryptoPaperFill, ...],
    journals: tuple[CryptoPaperJournal, ...],
    sell_fill_id: str,
) -> CryptoPaperCloseAccountingCorrection:
    fill = next((item for item in fills if item.fill_id == sell_fill_id), None)
    if fill is None:
        raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_SOURCE_FILL_NOT_FOUND")
    if fill.side is not PaperSide.SELL:
        raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_SOURCE_NOT_SELL")
    correction_id = close_accounting_correction_journal_id(fill.fill_id)
    if any(item.journal_id == correction_id for item in journals):
        raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_ALREADY_RETAINED")
    primaries = tuple(
        item for item in journals if item.journal_id == primary_fill_journal_id(fill.fill_id)
    )
    if len(primaries) != 1 or primaries[0].source_fill_id != fill.fill_id:
        raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_SOURCE_JOURNAL_NOT_FOUND")
    source = primaries[0]
    if source.account_id != account_id:
        raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_ACCOUNT_MISMATCH")
    asset_postings = tuple(
        item for item in source.postings if item.account_code == LEDGER_ASSET_COST
    )
    if len(asset_postings) != 1 or asset_postings[0].direction != "CREDIT":
        raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_SOURCE_JOURNAL_SHAPE_INVALID")
    booked = asset_postings[0].amount
    replica = _pre_fix_sell_journal(fill=fill, account_id=account_id, booked_relief=booked)
    # Full-value equality (ids, postings, time, stored hash), not just the
    # stored hash field: the retained journal must be exactly what the
    # pre-fix formula produces for this fill.
    if replica != source:
        raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_SOURCE_JOURNAL_NOT_PRE_FIX_SELL")
    prior = fills[: fills.index(fill)]
    held = product_ledger_consideration(product_id=fill.product_id, fills=prior)
    contract = ledger_consideration_relief(
        consideration=held.consideration,
        base_quantity=held.base_quantity,
        sold_base_quantity=fill.base_quantity,
    )
    restatement = booked - contract
    if restatement == 0:
        raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_NOT_REQUIRED")
    postings = (
        (
            CryptoPaperPosting(LEDGER_ASSET_COST, "DEBIT", restatement),
            CryptoPaperPosting(LEDGER_REALIZED_PNL, "CREDIT", restatement),
        )
        if restatement > 0
        else (
            CryptoPaperPosting(LEDGER_REALIZED_PNL, "DEBIT", -restatement),
            CryptoPaperPosting(LEDGER_ASSET_COST, "CREDIT", -restatement),
        )
    )
    journal = _sealed_journal(
        journal_id=correction_id,
        source_fill_id=fill.fill_id,
        account_id=account_id,
        postings=postings,
        occurred_at=source.occurred_at,
    )
    return CryptoPaperCloseAccountingCorrection(
        CLOSE_ACCOUNTING_CORRECTION_POLICY_ID,
        journal,
        fill.fill_id,
        source.journal_id,
        source.canonical_hash,
        booked,
        contract,
        restatement,
    )


@dataclass(frozen=True, slots=True)
class CryptoPaperOperation:
    idempotency_key: str
    operation_type: str
    result_id: str
    result_hash: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        idempotency_key: str,
        operation_type: str,
        result_id: str,
        result_hash: str,
    ) -> CryptoPaperOperation:
        values = (
            _id(idempotency_key, "CRYPTO_PAPER_IDEMPOTENCY_KEY_INVALID"),
            _id(operation_type, "CRYPTO_PAPER_OPERATION_TYPE_INVALID"),
            _id(result_id, "CRYPTO_PAPER_OPERATION_RESULT_ID_INVALID"),
            _id(result_hash, "CRYPTO_PAPER_OPERATION_RESULT_HASH_INVALID"),
        )
        return cls(*values, content_digest(values))


@dataclass(frozen=True, slots=True)
class CryptoPaperState:
    account: CryptoPaperAccount
    account_state: CryptoPaperAccountState
    contracts: tuple[CryptoPaperProviderContract, ...] = ()
    capabilities: tuple[CryptoPaperCapability, ...] = ()
    eligibilities: tuple[CryptoPaperEligibility, ...] = ()
    scans: tuple[CryptoPaperEligibilityScan, ...] = ()
    authorizations: tuple[CryptoPaperAuthorization, ...] = ()
    intents: tuple[CryptoPaperOrderIntent, ...] = ()
    previews: tuple[CryptoPaperPreview, ...] = ()
    evidence: tuple[RetainedPaperPriceEvidence, ...] = ()
    orders: tuple[CryptoPaperOrder, ...] = ()
    reservations: tuple[CryptoPaperReservation, ...] = ()
    events: tuple[CryptoPaperOrderEvent, ...] = ()
    fills: tuple[CryptoPaperFill, ...] = ()
    positions: tuple[CryptoPaperPosition, ...] = ()
    journals: tuple[CryptoPaperJournal, ...] = ()
    reconciliations: tuple[CryptoPaperReconciliation, ...] = ()
    operations: tuple[CryptoPaperOperation, ...] = ()
    canonical_hash: str = ""
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"

    def sealed(self) -> CryptoPaperState:
        values = replace(self, canonical_hash="")
        return replace(self, canonical_hash=content_digest(values))
