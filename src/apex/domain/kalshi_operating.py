"""Kalshi operating evidence and isolated internal PAPER rules.

Public refresh inputs are references to the existing retained Kalshi discovery
and binary-complement records.  This module owns no provider client, credential,
broker, scheduler, or LIVE capability.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from apex.domain.values import DecimalInput, content_digest, parse_decimal

KALSHI_CAPABILITY_CONTRACT_V1 = "KALSHI_CAPABILITY_CONTRACT_V1"
KALSHI_CURRENT_MARKET_POLICY_V1 = "KALSHI_CURRENT_MARKET_POLICY_V1"
KALSHI_PAPER_ORDER_ELIGIBILITY_POLICY_V1 = "KALSHI_PAPER_ORDER_ELIGIBILITY_POLICY_V1"
KALSHI_PAPER_ACCOUNTING_POLICY_V1 = "KALSHI_PAPER_DOUBLE_ENTRY_ACCOUNTING_V1"
KALSHI_PAPER_WARNING = "KALSHI PAPER — SIMULATED CAPITAL ONLY"
MAX_PAPER_ORDER_RISK = Decimal("25")
MAX_PAPER_OPEN_RISK = Decimal("100")
PAPER_PRICE_INCREMENT = Decimal("0.01")


class KalshiOperatingError(ValueError):
    """A Kalshi operating or PAPER invariant failed closed."""


class KalshiCapabilityState(StrEnum):
    PUBLIC_DISCOVERY_AVAILABLE = "PUBLIC_DISCOVERY_AVAILABLE"
    PUBLIC_ORDERBOOK_AVAILABLE = "PUBLIC_ORDERBOOK_AVAILABLE"
    PUBLIC_RULES_AVAILABLE = "PUBLIC_RULES_AVAILABLE"
    PRIVATE_CREDENTIALS_ABSENT = "PRIVATE_CREDENTIALS_ABSENT"
    PRIVATE_VIEW_AVAILABLE = "PRIVATE_VIEW_AVAILABLE"
    LIVE_TRADE_PERMISSION_AVAILABLE = "LIVE_TRADE_PERMISSION_AVAILABLE"
    PAPER_ONLY = "PAPER_ONLY"
    UNAVAILABLE = "UNAVAILABLE"


class KalshiEvidenceClass(StrEnum):
    FIXTURE = "FIXTURE"
    GENUINE_PUBLIC = "GENUINE_PUBLIC"


class KalshiMarketEligibilityState(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"


class KalshiPaperAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"


class KalshiSide(StrEnum):
    YES = "YES"
    NO = "NO"


class KalshiPaperOrderType(StrEnum):
    LIMIT = "LIMIT"


class KalshiPaperOrderState(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    SETTLED = "SETTLED"


class KalshiReservationState(StrEnum):
    HELD = "HELD"
    PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


class KalshiPositionState(StrEnum):
    OPEN = "OPEN"
    SETTLED = "SETTLED"


class KalshiReconciliationStatus(StrEnum):
    CLEAN = "CLEAN"
    DISCREPANCY = "DISCREPANCY"


def _id(value: str, reason: str, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise KalshiOperatingError(reason)
    return value.strip()


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise KalshiOperatingError(reason)
    return value.astimezone(UTC)


def _decimal(
    value: DecimalInput,
    reason: str,
    *,
    positive: bool = False,
    signed: bool = False,
) -> Decimal:
    try:
        return parse_decimal(value, allow_zero=not positive, allow_negative=signed)
    except ValueError as error:
        raise KalshiOperatingError(reason) from error


def _fee(price: Decimal, contracts: int) -> Decimal:
    """Conservative Kalshi quadratic taker-fee model, rounded to cents."""

    exact = Decimal("0.07") * Decimal(contracts) * price * (Decimal(1) - price)
    return (exact / PAPER_PRICE_INCREMENT).to_integral_value(
        rounding=ROUND_CEILING
    ) * PAPER_PRICE_INCREMENT


@dataclass(frozen=True, slots=True)
class KalshiCapability:
    capability_id: str
    inspected_at: datetime
    primary_state: KalshiCapabilityState
    public_market_discovery: KalshiCapabilityState
    public_market_inspection: KalshiCapabilityState
    public_orderbook_access: KalshiCapabilityState
    market_rules: KalshiCapabilityState
    close_settlement_metadata: KalshiCapabilityState
    authenticated_account_visibility: KalshiCapabilityState
    authenticated_order_preview: KalshiCapabilityState
    authenticated_trade_permission: KalshiCapabilityState
    paper_capability: KalshiCapabilityState
    live_capability: KalshiCapabilityState
    states: tuple[KalshiCapabilityState, ...]
    limitations: tuple[str, ...]
    credentials_requested: bool
    provider_requests: int
    canonical_hash: str
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"

    @classmethod
    def inspect(
        cls,
        *,
        capability_id: str,
        inspected_at: datetime,
        public_discovery: bool,
        public_orderbook: bool,
        public_rules: bool,
    ) -> KalshiCapability:
        public_states = tuple(
            state
            for available, state in (
                (public_discovery, KalshiCapabilityState.PUBLIC_DISCOVERY_AVAILABLE),
                (public_orderbook, KalshiCapabilityState.PUBLIC_ORDERBOOK_AVAILABLE),
                (public_rules, KalshiCapabilityState.PUBLIC_RULES_AVAILABLE),
            )
            if available
        )
        ready = len(public_states) == 3
        states = (
            (
                *public_states,
                KalshiCapabilityState.PRIVATE_CREDENTIALS_ABSENT,
                KalshiCapabilityState.PAPER_ONLY,
            )
            if ready
            else (KalshiCapabilityState.UNAVAILABLE,)
        )
        limitations = tuple(
            sorted(
                {
                    "NO_KALSHI_CREDENTIALS_REQUESTED",
                    "NO_AUTHENTICATED_ACCOUNT_OR_PREVIEW",
                    "NO_GENUINE_ORDER_SUBMISSION_OR_CANCELLATION",
                    "NO_LIVE_AUTHORITY",
                    *(() if ready else ("REQUIRED_PUBLIC_READ_CAPABILITY_UNAVAILABLE",)),
                }
            )
        )
        values = (
            _id(capability_id, "KALSHI_CAPABILITY_ID_INVALID"),
            _time(inspected_at, "KALSHI_CAPABILITY_TIME_INVALID"),
            KalshiCapabilityState.PAPER_ONLY if ready else KalshiCapabilityState.UNAVAILABLE,
            (
                KalshiCapabilityState.PUBLIC_DISCOVERY_AVAILABLE
                if public_discovery
                else KalshiCapabilityState.UNAVAILABLE
            ),
            (
                KalshiCapabilityState.PUBLIC_DISCOVERY_AVAILABLE
                if public_discovery
                else KalshiCapabilityState.UNAVAILABLE
            ),
            (
                KalshiCapabilityState.PUBLIC_ORDERBOOK_AVAILABLE
                if public_orderbook
                else KalshiCapabilityState.UNAVAILABLE
            ),
            (
                KalshiCapabilityState.PUBLIC_RULES_AVAILABLE
                if public_rules
                else KalshiCapabilityState.UNAVAILABLE
            ),
            (
                KalshiCapabilityState.PUBLIC_RULES_AVAILABLE
                if public_rules
                else KalshiCapabilityState.UNAVAILABLE
            ),
            KalshiCapabilityState.PRIVATE_CREDENTIALS_ABSENT,
            KalshiCapabilityState.PRIVATE_CREDENTIALS_ABSENT,
            KalshiCapabilityState.PRIVATE_CREDENTIALS_ABSENT,
            (KalshiCapabilityState.PAPER_ONLY if ready else KalshiCapabilityState.UNAVAILABLE),
            KalshiCapabilityState.UNAVAILABLE,
            states,
            limitations,
            False,
            0,
            "PAPER_ONLY",
            "NONE",
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])

    @property
    def public_refresh_available(self) -> bool:
        required = {
            KalshiCapabilityState.PUBLIC_DISCOVERY_AVAILABLE,
            KalshiCapabilityState.PUBLIC_ORDERBOOK_AVAILABLE,
            KalshiCapabilityState.PUBLIC_RULES_AVAILABLE,
        }
        return required.issubset(self.states)


@dataclass(frozen=True, slots=True)
class KalshiCurrentOpportunity:
    opportunity_id: str
    refresh_id: str
    evaluation_id: str
    evidence_class: KalshiEvidenceClass
    strategy_id: str
    market_ticker: str
    event_ticker: str
    rule_hash: str
    side: KalshiSide
    price: Decimal
    expected_gross_payoff: Decimal
    modeled_fee: Decimal
    maximum_loss: Decimal
    expected_net_value: Decimal | None
    visible_liquidity: int
    required_hypothetical_capital: Decimal
    maximum_sensible_allocation: Decimal
    close_at: datetime
    settlement_at: datetime
    expires_at: datetime
    evidence_observed_at: datetime
    supporting_evidence_ids: tuple[str, ...]
    eligibility: KalshiMarketEligibilityState
    rejection_reasons: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"
    paper_eligible: bool = False
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        opportunity_id: str,
        refresh_id: str,
        evaluation_id: str,
        evidence_class: KalshiEvidenceClass,
        market_ticker: str,
        event_ticker: str,
        rule_hash: str,
        side: KalshiSide,
        price: DecimalInput,
        visible_liquidity: int,
        close_at: datetime,
        settlement_at: datetime,
        evidence_observed_at: datetime,
        as_of: datetime,
        supporting_evidence_ids: tuple[str, ...],
        market_open: bool = True,
        book_complete: bool = True,
        fee_retained: bool = True,
        maximum_evidence_age_seconds: int = 300,
        expected_net_value: DecimalInput | None = None,
        disqualifying_reasons: tuple[str, ...] = (),
    ) -> KalshiCurrentOpportunity:
        now = _time(as_of, "KALSHI_OPPORTUNITY_AS_OF_INVALID")
        close = _time(close_at, "KALSHI_OPPORTUNITY_CLOSE_INVALID")
        settlement = _time(settlement_at, "KALSHI_OPPORTUNITY_SETTLEMENT_TIME_INVALID")
        observed = _time(evidence_observed_at, "KALSHI_OPPORTUNITY_EVIDENCE_TIME_INVALID")
        normalized_price = _decimal(price, "KALSHI_OPPORTUNITY_PRICE_INVALID")
        reasons: list[str] = []
        if normalized_price <= 0 or normalized_price >= 1:
            reasons.append("PRICE_OUTSIDE_SUPPORTED_BUY_RANGE")
        if not market_open:
            reasons.append("MARKET_NOT_OPEN")
        if now >= close:
            reasons.append("MARKET_EXPIRED")
        if settlement < close:
            reasons.append("SETTLEMENT_BEFORE_CLOSE")
        normalized_rule_hash = rule_hash.strip() or "UNAVAILABLE"
        if not rule_hash.strip():
            reasons.append("RULE_EVIDENCE_MISSING")
        if not book_complete:
            reasons.append("ORDERBOOK_MISSING_SIDE")
        if not fee_retained:
            reasons.append("FEE_EVIDENCE_MISSING")
        if visible_liquidity <= 0:
            reasons.append("VISIBLE_LIQUIDITY_INSUFFICIENT")
        age = (now - observed).total_seconds()
        if age < 0 or age > maximum_evidence_age_seconds:
            reasons.append("EVIDENCE_STALE_OR_UNAVAILABLE")
        evidence_ids = tuple(sorted(set(supporting_evidence_ids)))
        if not evidence_ids:
            reasons.append("SUPPORTING_EVIDENCE_MISSING")
        reasons.extend(disqualifying_reasons)
        eligibility = (
            KalshiMarketEligibilityState.ELIGIBLE
            if not reasons
            else KalshiMarketEligibilityState.REJECTED
        )
        modeled_fee = _fee(normalized_price, 1)
        maximum_loss = normalized_price
        net = (
            None
            if expected_net_value is None
            else _decimal(
                expected_net_value,
                "KALSHI_OPPORTUNITY_NET_VALUE_INVALID",
                signed=True,
            )
        )
        values = (
            _id(opportunity_id, "KALSHI_OPPORTUNITY_ID_INVALID"),
            _id(refresh_id, "KALSHI_REFRESH_ID_INVALID"),
            _id(evaluation_id, "KALSHI_EVALUATION_ID_INVALID"),
            evidence_class,
            "KALSHI_BINARY_COMPLEMENT_CURRENT_V1",
            _id(market_ticker, "KALSHI_MARKET_ID_INVALID"),
            _id(event_ticker, "KALSHI_EVENT_ID_INVALID"),
            _id(normalized_rule_hash, "KALSHI_RULE_HASH_INVALID"),
            side,
            normalized_price,
            Decimal(1),
            modeled_fee,
            maximum_loss,
            net,
            visible_liquidity,
            maximum_loss + modeled_fee,
            min(MAX_PAPER_ORDER_RISK, maximum_loss + modeled_fee),
            close,
            settlement,
            close,
            observed,
            evidence_ids,
            eligibility,
            tuple(sorted(set(reasons))),
            "NONE",
            False,
            "NONE",
        )
        return cls(*values[:-3], content_digest(values), *values[-3:])


@dataclass(frozen=True, slots=True)
class KalshiRefreshRecord:
    refresh_id: str
    as_of: datetime
    evidence_class: KalshiEvidenceClass
    market_discovery_run_id: str
    market_configuration_hash: str
    complement_configuration_hash: str
    page_count: int
    market_count: int
    markets_evaluated: int
    eligible_market_count: int
    opportunity_ids: tuple[str, ...]
    evaluation_ids: tuple[str, ...]
    provider_requests: int
    provider_writes: int
    request_bounds: tuple[tuple[str, int], ...]
    checkpoints: tuple[str, ...]
    warnings: tuple[str, ...]
    completed_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        refresh_id: str,
        as_of: datetime,
        evidence_class: KalshiEvidenceClass,
        market_discovery_run_id: str,
        market_configuration_hash: str,
        complement_configuration_hash: str,
        page_count: int,
        market_count: int,
        markets_evaluated: int,
        opportunities: tuple[KalshiCurrentOpportunity, ...],
        evaluation_ids: tuple[str, ...],
        provider_requests: int,
        request_bounds: tuple[tuple[str, int], ...],
        checkpoints: tuple[str, ...],
        warnings: tuple[str, ...],
        completed_at: datetime,
    ) -> KalshiRefreshRecord:
        if min(page_count, market_count, markets_evaluated, provider_requests) < 0:
            raise KalshiOperatingError("KALSHI_REFRESH_COUNT_INVALID")
        if evidence_class is KalshiEvidenceClass.FIXTURE and provider_requests:
            raise KalshiOperatingError("KALSHI_FIXTURE_PROVIDER_REQUEST_FORBIDDEN")
        values = (
            _id(refresh_id, "KALSHI_REFRESH_ID_INVALID"),
            _time(as_of, "KALSHI_REFRESH_AS_OF_INVALID"),
            evidence_class,
            _id(market_discovery_run_id, "KALSHI_MARKET_RUN_ID_INVALID"),
            _id(market_configuration_hash, "KALSHI_MARKET_CONFIG_HASH_INVALID"),
            _id(complement_configuration_hash, "KALSHI_COMPLEMENT_CONFIG_HASH_INVALID"),
            page_count,
            market_count,
            markets_evaluated,
            sum(
                item.eligibility is KalshiMarketEligibilityState.ELIGIBLE for item in opportunities
            ),
            tuple(item.opportunity_id for item in opportunities),
            tuple(sorted(set(evaluation_ids))),
            provider_requests,
            0,
            tuple(sorted(request_bounds)),
            tuple(checkpoints),
            tuple(sorted(set(warnings))),
            _time(completed_at, "KALSHI_REFRESH_COMPLETED_TIME_INVALID"),
            "NONE",
            "NONE",
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class KalshiPaperAccount:
    account_id: str
    opening_cash: Decimal
    created_at: datetime
    status: KalshiPaperAccountStatus
    canonical_hash: str
    venue: str = "KALSHI"
    account_type: str = "INTERNAL_PAPER"
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls, *, account_id: str, opening_cash: DecimalInput, created_at: datetime
    ) -> KalshiPaperAccount:
        values = (
            _id(account_id, "KALSHI_PAPER_ACCOUNT_ID_INVALID"),
            _decimal(opening_cash, "KALSHI_PAPER_OPENING_CASH_INVALID", positive=True),
            _time(created_at, "KALSHI_PAPER_ACCOUNT_TIME_INVALID"),
            KalshiPaperAccountStatus.ACTIVE,
            "KALSHI",
            "INTERNAL_PAPER",
            "PAPER_ONLY",
            "NONE",
        )
        return cls(*values[:4], content_digest(values), *values[4:])


@dataclass(frozen=True, slots=True)
class KalshiPaperEligibility:
    decision_id: str
    opportunity_id: str
    source_opportunity_hash: str
    account_id: str | None
    evaluated_at: datetime
    eligible: bool
    reasons: tuple[str, ...]
    canonical_hash: str
    policy_id: str = KALSHI_PAPER_ORDER_ELIGIBILITY_POLICY_V1
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"

    @classmethod
    def evaluate(
        cls,
        *,
        decision_id: str,
        opportunity: KalshiCurrentOpportunity,
        account: KalshiPaperAccount | None,
        available_cash: DecimalInput,
        shared_eligible: bool,
        operating_cycle_enabled: bool,
        evaluated_at: datetime,
    ) -> KalshiPaperEligibility:
        now = _time(evaluated_at, "KALSHI_PAPER_ELIGIBILITY_TIME_INVALID")
        reasons: list[str] = []
        if opportunity.eligibility is not KalshiMarketEligibilityState.ELIGIBLE:
            reasons.append("SOURCE_MARKET_NOT_ELIGIBLE")
        if now >= opportunity.expires_at:
            reasons.append("SOURCE_OPPORTUNITY_EXPIRED")
        if not shared_eligible:
            reasons.append("SHARED_OPPORTUNITY_NOT_ELIGIBLE")
        if account is None:
            reasons.append("PAPER_ACCOUNT_UNAVAILABLE")
        elif account.status is not KalshiPaperAccountStatus.ACTIVE:
            reasons.append("PAPER_ACCOUNT_NOT_ACTIVE")
        cash = _decimal(
            available_cash,
            "KALSHI_PAPER_AVAILABLE_CASH_INVALID",
            signed=True,
        )
        if cash < opportunity.required_hypothetical_capital:
            reasons.append("PAPER_CASH_UNAVAILABLE")
        if not operating_cycle_enabled:
            reasons.append("OPERATING_CYCLE_NOT_ENABLED")
        eligible = not reasons
        if eligible:
            reasons.append("ELIGIBLE_FOR_EXPLICIT_OPERATOR_PAPER_AUTHORIZATION")
        values = (
            _id(decision_id, "KALSHI_PAPER_DECISION_ID_INVALID"),
            opportunity.opportunity_id,
            opportunity.canonical_hash,
            None if account is None else _id(account.account_id, "KALSHI_PAPER_ACCOUNT_ID_INVALID"),
            now,
            eligible,
            tuple(sorted(set(reasons))),
            KALSHI_PAPER_ORDER_ELIGIBILITY_POLICY_V1,
            "PAPER_ONLY",
            "NONE",
        )
        return cls(*values[:7], content_digest(values), *values[7:])


@dataclass(frozen=True, slots=True)
class KalshiPaperEligibilityScan:
    scan_id: str
    refresh_id: str
    evaluated_at: datetime
    source_count: int
    eligible_count: int
    decision_ids: tuple[str, ...]
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
        refresh_id: str,
        decisions: tuple[KalshiPaperEligibility, ...],
        evaluated_at: datetime,
    ) -> KalshiPaperEligibilityScan:
        values = (
            _id(scan_id, "KALSHI_PAPER_SCAN_ID_INVALID"),
            _id(refresh_id, "KALSHI_PAPER_SCAN_REFRESH_ID_INVALID"),
            _time(evaluated_at, "KALSHI_PAPER_SCAN_TIME_INVALID"),
            len(decisions),
            sum(item.eligible for item in decisions),
            tuple(item.decision_id for item in decisions),
            0,
            0,
            0,
            "PAPER_ONLY",
            "NONE",
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class KalshiPaperAuthorization:
    authorization_id: str
    account_id: str
    opportunity_id: str
    source_opportunity_hash: str
    market_ticker: str
    side: KalshiSide
    maximum_contracts: int
    maximum_dollar_risk: Decimal
    allowed_order_type: KalshiPaperOrderType
    expires_at: datetime
    actor: str
    reason: str
    idempotency_key: str
    created_at: datetime
    canonical_hash: str
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        authorization_id: str,
        account_id: str,
        opportunity: KalshiCurrentOpportunity,
        eligibility: KalshiPaperEligibility,
        maximum_contracts: int,
        maximum_dollar_risk: DecimalInput,
        expires_at: datetime,
        actor: str,
        reason: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> KalshiPaperAuthorization:
        created = _time(created_at, "KALSHI_PAPER_AUTHORIZATION_TIME_INVALID")
        expiry = _time(expires_at, "KALSHI_PAPER_AUTHORIZATION_EXPIRY_INVALID")
        risk = _decimal(
            maximum_dollar_risk,
            "KALSHI_PAPER_AUTHORIZATION_RISK_INVALID",
            positive=True,
        )
        if (
            not eligibility.eligible
            or eligibility.opportunity_id != opportunity.opportunity_id
            or eligibility.source_opportunity_hash != opportunity.canonical_hash
            or eligibility.account_id != account_id
        ):
            raise KalshiOperatingError("KALSHI_PAPER_OPPORTUNITY_NOT_ELIGIBLE")
        if not 1 <= maximum_contracts <= 10_000:
            raise KalshiOperatingError("KALSHI_PAPER_CONTRACT_LIMIT_INVALID")
        if risk > MAX_PAPER_ORDER_RISK:
            raise KalshiOperatingError("KALSHI_PAPER_ORDER_RISK_LIMIT_EXCEEDED")
        if expiry <= created or expiry > opportunity.expires_at:
            raise KalshiOperatingError("KALSHI_PAPER_AUTHORIZATION_EXPIRY_INVALID")
        values = (
            _id(authorization_id, "KALSHI_PAPER_AUTHORIZATION_ID_INVALID"),
            _id(account_id, "KALSHI_PAPER_ACCOUNT_ID_INVALID"),
            opportunity.opportunity_id,
            opportunity.canonical_hash,
            opportunity.market_ticker,
            opportunity.side,
            maximum_contracts,
            risk,
            KalshiPaperOrderType.LIMIT,
            expiry,
            _id(actor, "KALSHI_PAPER_ACTOR_INVALID"),
            _id(reason, "KALSHI_PAPER_AUTHORIZATION_REASON_INVALID"),
            _id(idempotency_key, "KALSHI_PAPER_IDEMPOTENCY_KEY_INVALID", 160),
            created,
            "PAPER_ONLY",
            "NONE",
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class KalshiPaperPreview:
    preview_id: str
    authorization_id: str
    opportunity_id: str
    market_ticker: str
    event_ticker: str
    rule_hash: str
    side: KalshiSide
    limit_price: Decimal
    contracts: int
    gross_cost: Decimal
    modeled_fees: Decimal
    maximum_loss: Decimal
    maximum_payoff: Decimal
    available_liquidity: int
    paper_cash_before: Decimal
    paper_cash_after: Decimal
    rejection_reasons: tuple[str, ...]
    evidence_id: str
    evidence_observed_at: datetime
    expires_at: datetime
    created_at: datetime
    canonical_hash: str
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"

    @property
    def accepted(self) -> bool:
        return not self.rejection_reasons

    @property
    def reserved_risk(self) -> Decimal:
        return self.maximum_loss + self.modeled_fees


def preview_paper_order(
    *,
    preview_id: str,
    authorization: KalshiPaperAuthorization,
    opportunity: KalshiCurrentOpportunity,
    limit_price: DecimalInput,
    contracts: int,
    available_cash: DecimalInput,
    available_liquidity: int,
    evidence_id: str,
    evidence_observed_at: datetime,
    created_at: datetime,
) -> KalshiPaperPreview:
    now = _time(created_at, "KALSHI_PAPER_PREVIEW_TIME_INVALID")
    observed = _time(evidence_observed_at, "KALSHI_PAPER_PREVIEW_EVIDENCE_TIME_INVALID")
    price = _decimal(limit_price, "KALSHI_PAPER_LIMIT_PRICE_INVALID", positive=True)
    cash = _decimal(available_cash, "KALSHI_PAPER_AVAILABLE_CASH_INVALID", signed=True)
    reasons: list[str] = []
    if authorization.opportunity_id != opportunity.opportunity_id:
        reasons.append("AUTHORIZATION_OPPORTUNITY_MISMATCH")
    if authorization.source_opportunity_hash != opportunity.canonical_hash:
        reasons.append("AUTHORIZATION_OPPORTUNITY_HASH_MISMATCH")
    if authorization.market_ticker != opportunity.market_ticker:
        reasons.append("AUTHORIZATION_MARKET_MISMATCH")
    if authorization.side is not opportunity.side:
        reasons.append("AUTHORIZATION_SIDE_MISMATCH")
    if now >= authorization.expires_at or now >= opportunity.expires_at:
        reasons.append("AUTHORIZATION_OR_OPPORTUNITY_EXPIRED")
    if price >= 1 or price % PAPER_PRICE_INCREMENT:
        reasons.append("LIMIT_PRICE_INVALID")
    if contracts < 1 or contracts > authorization.maximum_contracts:
        reasons.append("CONTRACT_QUANTITY_EXCEEDS_AUTHORIZATION")
    if available_liquidity < contracts:
        reasons.append("VISIBLE_LIQUIDITY_INSUFFICIENT")
    if observed > now:
        reasons.append("PREVIEW_EVIDENCE_UNAVAILABLE")
    gross = price * max(0, contracts)
    fees = _fee(price, max(0, contracts))
    maximum_loss = gross
    reserved = maximum_loss + fees
    if reserved > authorization.maximum_dollar_risk:
        reasons.append("MAXIMUM_RISK_EXCEEDS_AUTHORIZATION")
    if reserved > cash:
        reasons.append("PAPER_CASH_INSUFFICIENT")
    values = (
        _id(preview_id, "KALSHI_PAPER_PREVIEW_ID_INVALID"),
        authorization.authorization_id,
        opportunity.opportunity_id,
        opportunity.market_ticker,
        opportunity.event_ticker,
        opportunity.rule_hash,
        opportunity.side,
        price,
        contracts,
        gross,
        fees,
        maximum_loss,
        Decimal(contracts),
        available_liquidity,
        cash,
        cash - reserved,
        tuple(sorted(set(reasons))),
        _id(evidence_id, "KALSHI_PAPER_EVIDENCE_ID_INVALID"),
        observed,
        authorization.expires_at,
        now,
        "PAPER_ONLY",
        "NONE",
    )
    return KalshiPaperPreview(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class KalshiPaperReservation:
    reservation_id: str
    order_id: str
    original_amount: Decimal
    current_amount: Decimal
    state: KalshiReservationState
    created_at: datetime
    released_at: datetime | None
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class KalshiPaperOrder:
    order_id: str
    preview_id: str
    authorization_id: str
    opportunity_id: str
    market_ticker: str
    event_ticker: str
    rule_hash: str
    side: KalshiSide
    limit_price: Decimal
    contracts: int
    filled_contracts: int
    state: KalshiPaperOrderState
    submitted_at: datetime
    expires_at: datetime
    idempotency_key: str
    canonical_hash: str
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"

    @property
    def remaining_contracts(self) -> int:
        return self.contracts - self.filled_contracts


@dataclass(frozen=True, slots=True)
class KalshiPaperFill:
    fill_id: str
    order_id: str
    market_ticker: str
    side: KalshiSide
    price: Decimal
    contracts: int
    fee: Decimal
    evidence_id: str
    evidence_observed_at: datetime
    ingested_at: datetime
    idempotency_key: str
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class KalshiPaperPosition:
    position_id: str
    market_ticker: str
    event_ticker: str
    rule_hash: str
    side: KalshiSide
    contracts: int
    cost_basis: Decimal
    fees: Decimal
    state: KalshiPositionState
    opened_at: datetime
    settled_at: datetime | None
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class KalshiResolutionFact:
    resolution_fact_id: str
    market_ticker: str
    event_ticker: str
    resolution: KalshiSide
    resolution_source: str
    rule_hash: str
    settled_at: datetime
    retained_at: datetime
    evidence_class: KalshiEvidenceClass
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        resolution_fact_id: str,
        market_ticker: str,
        event_ticker: str,
        resolution: KalshiSide,
        resolution_source: str,
        rule_hash: str,
        settled_at: datetime,
        retained_at: datetime,
        evidence_class: KalshiEvidenceClass,
    ) -> KalshiResolutionFact:
        settled = _time(settled_at, "KALSHI_RESOLUTION_SETTLED_TIME_INVALID")
        retained = _time(retained_at, "KALSHI_RESOLUTION_RETAINED_TIME_INVALID")
        if retained < settled:
            raise KalshiOperatingError("KALSHI_RESOLUTION_RETAINED_BEFORE_SETTLEMENT")
        values = (
            _id(resolution_fact_id, "KALSHI_RESOLUTION_FACT_ID_INVALID"),
            _id(market_ticker, "KALSHI_MARKET_ID_INVALID"),
            _id(event_ticker, "KALSHI_EVENT_ID_INVALID"),
            resolution,
            _id(resolution_source, "KALSHI_RESOLUTION_SOURCE_INVALID"),
            _id(rule_hash, "KALSHI_RULE_HASH_INVALID"),
            settled,
            retained,
            evidence_class,
        )
        return cls(*values, content_digest(values))


@dataclass(frozen=True, slots=True)
class KalshiPaperSettlement:
    settlement_id: str
    position_id: str
    resolution_fact_id: str
    gross_payout: Decimal
    fees: Decimal
    realized_paper_pnl: Decimal
    cash_effect: Decimal
    settled_at: datetime
    idempotency_key: str
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class KalshiPaperJournal:
    journal_id: str
    source_type: str
    source_id: str
    debit_account: str
    credit_account: str
    amount: Decimal
    occurred_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        journal_id: str,
        source_type: str,
        source_id: str,
        debit_account: str,
        credit_account: str,
        amount: DecimalInput,
        occurred_at: datetime,
    ) -> KalshiPaperJournal:
        values = (
            _id(journal_id, "KALSHI_PAPER_JOURNAL_ID_INVALID"),
            _id(source_type, "KALSHI_PAPER_JOURNAL_SOURCE_INVALID"),
            _id(source_id, "KALSHI_PAPER_JOURNAL_SOURCE_ID_INVALID"),
            _id(debit_account, "KALSHI_PAPER_DEBIT_ACCOUNT_INVALID"),
            _id(credit_account, "KALSHI_PAPER_CREDIT_ACCOUNT_INVALID"),
            _decimal(amount, "KALSHI_PAPER_JOURNAL_AMOUNT_INVALID", positive=True),
            _time(occurred_at, "KALSHI_PAPER_JOURNAL_TIME_INVALID"),
        )
        return cls(*values, content_digest(values))


@dataclass(frozen=True, slots=True)
class KalshiPaperOperation:
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
    ) -> KalshiPaperOperation:
        values = (
            _id(idempotency_key, "KALSHI_PAPER_IDEMPOTENCY_KEY_INVALID", 160),
            _id(operation_type, "KALSHI_PAPER_OPERATION_TYPE_INVALID"),
            _id(result_id, "KALSHI_PAPER_OPERATION_RESULT_ID_INVALID"),
            _id(result_hash, "KALSHI_PAPER_OPERATION_RESULT_HASH_INVALID"),
        )
        return cls(*values, content_digest(values))


@dataclass(frozen=True, slots=True)
class KalshiPaperReconciliation:
    reconciliation_id: str
    account_id: str
    reconciled_at: datetime
    status: KalshiReconciliationStatus
    issues: tuple[str, ...]
    calculated_cash: Decimal
    retained_cash: Decimal
    calculated_reserved_cash: Decimal
    retained_reserved_cash: Decimal
    open_exposure: Decimal
    modeled_fees: Decimal
    realized_paper_pnl: Decimal
    journal_debits: Decimal
    journal_credits: Decimal
    canonical_hash: str
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class KalshiPaperState:
    account: KalshiPaperAccount
    cash: Decimal
    reserved_cash: Decimal
    modeled_fees: Decimal = Decimal(0)
    realized_paper_pnl: Decimal = Decimal(0)
    authorizations: tuple[KalshiPaperAuthorization, ...] = ()
    previews: tuple[KalshiPaperPreview, ...] = ()
    orders: tuple[KalshiPaperOrder, ...] = ()
    reservations: tuple[KalshiPaperReservation, ...] = ()
    fills: tuple[KalshiPaperFill, ...] = ()
    positions: tuple[KalshiPaperPosition, ...] = ()
    resolutions: tuple[KalshiResolutionFact, ...] = ()
    settlements: tuple[KalshiPaperSettlement, ...] = ()
    journals: tuple[KalshiPaperJournal, ...] = ()
    reconciliations: tuple[KalshiPaperReconciliation, ...] = ()
    operations: tuple[KalshiPaperOperation, ...] = ()
    updated_at: datetime | None = None
    canonical_hash: str = ""
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"

    @classmethod
    def opening(cls, account: KalshiPaperAccount) -> KalshiPaperState:
        return cls(
            account=account,
            cash=account.opening_cash,
            reserved_cash=Decimal(0),
            updated_at=account.created_at,
        ).sealed()

    @property
    def available_cash(self) -> Decimal:
        return self.cash - self.reserved_cash

    @property
    def open_risk(self) -> Decimal:
        unfilled_order_risk = sum(
            (
                item.current_amount
                for item in self.reservations
                if item.state
                in {
                    KalshiReservationState.HELD,
                    KalshiReservationState.PARTIALLY_CONSUMED,
                }
            ),
            Decimal(0),
        )
        filled_position_risk = sum(
            (item.cost_basis for item in self.positions if item.state is KalshiPositionState.OPEN),
            Decimal(0),
        )
        return unfilled_order_risk + filled_position_risk

    def sealed(self) -> KalshiPaperState:
        value = replace(self, canonical_hash="")
        return replace(value, canonical_hash=content_digest(value))


def submit_paper_order(
    *,
    state: KalshiPaperState,
    order_id: str,
    preview: KalshiPaperPreview,
    idempotency_key: str,
    submitted_at: datetime,
) -> tuple[KalshiPaperOrder, KalshiPaperReservation, KalshiPaperState]:
    now = _time(submitted_at, "KALSHI_PAPER_SUBMISSION_TIME_INVALID")
    if not preview.accepted or now < preview.created_at or now >= preview.expires_at:
        raise KalshiOperatingError("KALSHI_PAPER_PREVIEW_NOT_SUBMITTABLE")
    if preview.paper_cash_before != state.available_cash:
        raise KalshiOperatingError("KALSHI_PAPER_PREVIEW_CASH_STALE")
    if preview.reserved_risk > state.available_cash:
        raise KalshiOperatingError("KALSHI_PAPER_CASH_INSUFFICIENT")
    if state.open_risk + preview.reserved_risk > MAX_PAPER_OPEN_RISK:
        raise KalshiOperatingError("KALSHI_PAPER_AGGREGATE_RISK_LIMIT_EXCEEDED")
    if any(
        item.market_ticker == preview.market_ticker
        and item.side is preview.side
        and item.state
        in {
            KalshiPaperOrderState.OPEN,
            KalshiPaperOrderState.PARTIALLY_FILLED,
            KalshiPaperOrderState.CANCEL_REQUESTED,
        }
        for item in state.orders
    ):
        raise KalshiOperatingError("KALSHI_PAPER_OPEN_MARKET_SIDE_LIMIT_EXCEEDED")
    order_values = (
        _id(order_id, "KALSHI_PAPER_ORDER_ID_INVALID"),
        preview.preview_id,
        preview.authorization_id,
        preview.opportunity_id,
        preview.market_ticker,
        preview.event_ticker,
        preview.rule_hash,
        preview.side,
        preview.limit_price,
        preview.contracts,
        0,
        KalshiPaperOrderState.OPEN,
        now,
        preview.expires_at,
        _id(idempotency_key, "KALSHI_PAPER_IDEMPOTENCY_KEY_INVALID", 160),
        "PAPER_ONLY",
        "NONE",
    )
    order = KalshiPaperOrder(*order_values[:-2], content_digest(order_values), *order_values[-2:])
    reservation_values = (
        f"{order.order_id}-reservation",
        order.order_id,
        preview.reserved_risk,
        preview.reserved_risk,
        KalshiReservationState.HELD,
        now,
        None,
    )
    reservation = KalshiPaperReservation(*reservation_values, content_digest(reservation_values))
    return (
        order,
        reservation,
        replace(
            state,
            reserved_cash=state.reserved_cash + reservation.current_amount,
            orders=(*state.orders, order),
            reservations=(*state.reservations, reservation),
            updated_at=now,
        ).sealed(),
    )


def apply_paper_fill(
    *,
    state: KalshiPaperState,
    order_id: str,
    fill_id: str,
    contracts: int,
    price: DecimalInput,
    evidence_id: str,
    evidence_observed_at: datetime,
    ingested_at: datetime,
    idempotency_key: str,
) -> tuple[KalshiPaperFill, KalshiPaperState]:
    order = next((item for item in state.orders if item.order_id == order_id), None)
    reservation = next((item for item in state.reservations if item.order_id == order_id), None)
    if order is None or reservation is None:
        raise KalshiOperatingError("KALSHI_PAPER_ORDER_OR_RESERVATION_NOT_FOUND")
    if order.state not in {
        KalshiPaperOrderState.OPEN,
        KalshiPaperOrderState.PARTIALLY_FILLED,
    }:
        raise KalshiOperatingError("KALSHI_PAPER_ORDER_NOT_FILLABLE")
    observed = _time(evidence_observed_at, "KALSHI_PAPER_FILL_EVIDENCE_TIME_INVALID")
    ingested = _time(ingested_at, "KALSHI_PAPER_FILL_INGESTION_TIME_INVALID")
    if observed <= order.submitted_at:
        raise KalshiOperatingError("KALSHI_PAPER_PRE_SUBMISSION_FILL_FORBIDDEN")
    if ingested < observed:
        raise KalshiOperatingError("KALSHI_PAPER_FILL_INGESTION_PRECEDES_EVIDENCE")
    fill_price = _decimal(price, "KALSHI_PAPER_FILL_PRICE_INVALID", positive=True)
    if fill_price > order.limit_price or fill_price >= 1:
        raise KalshiOperatingError("KALSHI_PAPER_FILL_EXCEEDS_LIMIT")
    if contracts < 1 or contracts > order.remaining_contracts:
        raise KalshiOperatingError("KALSHI_PAPER_FILL_QUANTITY_INVALID")
    fee = _fee(fill_price, contracts)
    cost = fill_price * contracts
    fill_values = (
        _id(fill_id, "KALSHI_PAPER_FILL_ID_INVALID"),
        order.order_id,
        order.market_ticker,
        order.side,
        fill_price,
        contracts,
        fee,
        _id(evidence_id, "KALSHI_PAPER_EVIDENCE_ID_INVALID"),
        observed,
        ingested,
        _id(idempotency_key, "KALSHI_PAPER_IDEMPOTENCY_KEY_INVALID", 160),
    )
    fill = KalshiPaperFill(*fill_values, content_digest(fill_values))
    filled_total = order.filled_contracts + contracts
    next_order = replace(
        order,
        filled_contracts=filled_total,
        state=(
            KalshiPaperOrderState.FILLED
            if filled_total == order.contracts
            else KalshiPaperOrderState.PARTIALLY_FILLED
        ),
        canonical_hash="",
    )
    next_order = replace(next_order, canonical_hash=content_digest(next_order))
    remaining = order.contracts - filled_total
    remaining_risk = (
        order.limit_price * remaining + _fee(order.limit_price, remaining)
        if remaining
        else Decimal(0)
    )
    released = reservation.current_amount - remaining_risk
    next_reservation = replace(
        reservation,
        current_amount=remaining_risk,
        state=(
            KalshiReservationState.CONSUMED
            if remaining == 0
            else KalshiReservationState.PARTIALLY_CONSUMED
        ),
        released_at=ingested if remaining == 0 else None,
        canonical_hash="",
    )
    next_reservation = replace(next_reservation, canonical_hash=content_digest(next_reservation))
    existing_position = next(
        (
            item
            for item in state.positions
            if item.market_ticker == order.market_ticker
            and item.side is order.side
            and item.state is KalshiPositionState.OPEN
        ),
        None,
    )
    if existing_position is None:
        position_values = (
            f"{order.order_id}-position",
            order.market_ticker,
            order.event_ticker,
            order.rule_hash,
            order.side,
            contracts,
            cost,
            fee,
            KalshiPositionState.OPEN,
            ingested,
            None,
        )
        position = KalshiPaperPosition(*position_values, content_digest(position_values))
        positions = (*state.positions, position)
    else:
        position = replace(
            existing_position,
            contracts=existing_position.contracts + contracts,
            cost_basis=existing_position.cost_basis + cost,
            fees=existing_position.fees + fee,
            canonical_hash="",
        )
        position = replace(position, canonical_hash=content_digest(position))
        positions = tuple(
            position if item.position_id == position.position_id else item
            for item in state.positions
        )
    purchase = KalshiPaperJournal.create(
        journal_id=f"{fill.fill_id}-purchase",
        source_type="FILL",
        source_id=fill.fill_id,
        debit_account="KALSHI_PAPER_POSITION_COST",
        credit_account="KALSHI_PAPER_CASH",
        amount=cost,
        occurred_at=ingested,
    )
    journals = (*state.journals, purchase)
    if fee:
        journals = (
            *journals,
            KalshiPaperJournal.create(
                journal_id=f"{fill.fill_id}-fee",
                source_type="FILL_FEE",
                source_id=fill.fill_id,
                debit_account="KALSHI_PAPER_FEE_EXPENSE",
                credit_account="KALSHI_PAPER_CASH",
                amount=fee,
                occurred_at=ingested,
            ),
        )
    orders = tuple(next_order if item.order_id == order.order_id else item for item in state.orders)
    reservations = tuple(
        next_reservation if item.order_id == order.order_id else item for item in state.reservations
    )
    next_state = replace(
        state,
        cash=state.cash - cost - fee,
        reserved_cash=state.reserved_cash - released,
        modeled_fees=state.modeled_fees + fee,
        orders=orders,
        reservations=reservations,
        fills=(*state.fills, fill),
        positions=positions,
        journals=journals,
        updated_at=ingested,
    ).sealed()
    if next_state.cash < 0 or next_state.reserved_cash < 0:
        raise KalshiOperatingError("KALSHI_PAPER_NEGATIVE_CASH_FORBIDDEN")
    return fill, next_state


def cancel_paper_order(
    *,
    state: KalshiPaperState,
    order_id: str,
    canceled_at: datetime,
) -> KalshiPaperState:
    now = _time(canceled_at, "KALSHI_PAPER_CANCELLATION_TIME_INVALID")
    order = next((item for item in state.orders if item.order_id == order_id), None)
    reservation = next((item for item in state.reservations if item.order_id == order_id), None)
    if order is None or reservation is None:
        raise KalshiOperatingError("KALSHI_PAPER_ORDER_OR_RESERVATION_NOT_FOUND")
    if order.state not in {
        KalshiPaperOrderState.OPEN,
        KalshiPaperOrderState.PARTIALLY_FILLED,
        KalshiPaperOrderState.CANCEL_REQUESTED,
    }:
        raise KalshiOperatingError("KALSHI_PAPER_ORDER_NOT_CANCELABLE")
    if now < order.submitted_at:
        raise KalshiOperatingError("KALSHI_PAPER_CANCELLATION_PRECEDES_SUBMISSION")
    next_order = replace(order, state=KalshiPaperOrderState.CANCELED, canonical_hash="")
    next_order = replace(next_order, canonical_hash=content_digest(next_order))
    next_reservation = replace(
        reservation,
        current_amount=Decimal(0),
        state=KalshiReservationState.RELEASED,
        released_at=now,
        canonical_hash="",
    )
    next_reservation = replace(next_reservation, canonical_hash=content_digest(next_reservation))
    return replace(
        state,
        reserved_cash=state.reserved_cash - reservation.current_amount,
        orders=tuple(next_order if item.order_id == order_id else item for item in state.orders),
        reservations=tuple(
            next_reservation if item.order_id == order_id else item for item in state.reservations
        ),
        updated_at=now,
    ).sealed()


def expire_paper_order(
    *,
    state: KalshiPaperState,
    order_id: str,
    expired_at: datetime,
) -> KalshiPaperState:
    now = _time(expired_at, "KALSHI_PAPER_EXPIRY_TIME_INVALID")
    order = next((item for item in state.orders if item.order_id == order_id), None)
    reservation = next((item for item in state.reservations if item.order_id == order_id), None)
    if order is None or reservation is None:
        raise KalshiOperatingError("KALSHI_PAPER_ORDER_OR_RESERVATION_NOT_FOUND")
    if order.state not in {
        KalshiPaperOrderState.OPEN,
        KalshiPaperOrderState.PARTIALLY_FILLED,
    }:
        raise KalshiOperatingError("KALSHI_PAPER_ORDER_NOT_EXPIRABLE")
    if now < order.expires_at:
        raise KalshiOperatingError("KALSHI_PAPER_ORDER_EXPIRY_NOT_REACHED")
    next_order = replace(order, state=KalshiPaperOrderState.EXPIRED, canonical_hash="")
    next_order = replace(next_order, canonical_hash=content_digest(next_order))
    next_reservation = replace(
        reservation,
        current_amount=Decimal(0),
        state=KalshiReservationState.RELEASED,
        released_at=now,
        canonical_hash="",
    )
    next_reservation = replace(next_reservation, canonical_hash=content_digest(next_reservation))
    return replace(
        state,
        reserved_cash=state.reserved_cash - reservation.current_amount,
        orders=tuple(next_order if item.order_id == order_id else item for item in state.orders),
        reservations=tuple(
            next_reservation if item.order_id == order_id else item for item in state.reservations
        ),
        updated_at=now,
    ).sealed()


def settle_paper_position(
    *,
    state: KalshiPaperState,
    position_id: str,
    fact: KalshiResolutionFact,
    settlement_id: str,
    idempotency_key: str,
) -> tuple[KalshiPaperSettlement, KalshiPaperState]:
    position = next((item for item in state.positions if item.position_id == position_id), None)
    if position is None or position.state is not KalshiPositionState.OPEN:
        raise KalshiOperatingError("KALSHI_PAPER_POSITION_NOT_SETTLEABLE")
    if (
        position.market_ticker != fact.market_ticker
        or position.event_ticker != fact.event_ticker
        or position.rule_hash != fact.rule_hash
    ):
        raise KalshiOperatingError("KALSHI_RESOLUTION_LINEAGE_MISMATCH")
    if fact.settled_at < position.opened_at:
        raise KalshiOperatingError("KALSHI_RESOLUTION_PRECEDES_POSITION")
    payout = Decimal(position.contracts) if position.side is fact.resolution else Decimal(0)
    pnl = payout - position.cost_basis - position.fees
    values = (
        _id(settlement_id, "KALSHI_PAPER_SETTLEMENT_ID_INVALID"),
        position.position_id,
        fact.resolution_fact_id,
        payout,
        Decimal(0),
        pnl,
        payout,
        fact.settled_at,
        _id(idempotency_key, "KALSHI_PAPER_IDEMPOTENCY_KEY_INVALID", 160),
    )
    settlement = KalshiPaperSettlement(*values, content_digest(values))
    next_position = replace(
        position,
        state=KalshiPositionState.SETTLED,
        settled_at=fact.settled_at,
        canonical_hash="",
    )
    next_position = replace(next_position, canonical_hash=content_digest(next_position))
    journals = state.journals
    if payout:
        journals = (
            *journals,
            KalshiPaperJournal.create(
                journal_id=f"{settlement.settlement_id}-payout",
                source_type="SETTLEMENT",
                source_id=settlement.settlement_id,
                debit_account="KALSHI_PAPER_CASH",
                credit_account="KALSHI_PAPER_SETTLEMENT_PROCEEDS",
                amount=payout,
                occurred_at=fact.settled_at,
            ),
        )
    orders = tuple(
        replace(item, state=KalshiPaperOrderState.SETTLED, canonical_hash="")
        if item.market_ticker == position.market_ticker
        and item.side is position.side
        and item.state is KalshiPaperOrderState.FILLED
        else item
        for item in state.orders
    )
    orders = tuple(
        replace(item, canonical_hash=content_digest(item)) if item.canonical_hash == "" else item
        for item in orders
    )
    return settlement, replace(
        state,
        cash=state.cash + payout,
        realized_paper_pnl=state.realized_paper_pnl + pnl,
        positions=tuple(
            next_position if item.position_id == position_id else item for item in state.positions
        ),
        orders=orders,
        resolutions=(*state.resolutions, fact),
        settlements=(*state.settlements, settlement),
        journals=journals,
        updated_at=fact.settled_at,
    ).sealed()


def reconcile_paper_state(
    *,
    state: KalshiPaperState,
    reconciliation_id: str,
    reconciled_at: datetime,
) -> KalshiPaperReconciliation:
    calculated_cash = state.account.opening_cash
    for fill in state.fills:
        calculated_cash -= fill.price * fill.contracts + fill.fee
    calculated_cash += sum((item.cash_effect for item in state.settlements), Decimal(0))
    calculated_reserved = sum(
        (
            item.current_amount
            for item in state.reservations
            if item.state
            in {
                KalshiReservationState.HELD,
                KalshiReservationState.PARTIALLY_CONSUMED,
            }
        ),
        Decimal(0),
    )
    open_exposure = sum(
        (item.cost_basis for item in state.positions if item.state is KalshiPositionState.OPEN),
        Decimal(0),
    )
    modeled_fees = sum((item.fee for item in state.fills), Decimal(0))
    realized = sum((item.realized_paper_pnl for item in state.settlements), Decimal(0))
    debits = sum((item.amount for item in state.journals), Decimal(0))
    credits = sum((item.amount for item in state.journals), Decimal(0))
    issues: list[str] = []
    if calculated_cash != state.cash:
        issues.append("CASH_MISMATCH")
    if calculated_reserved != state.reserved_cash:
        issues.append("RESERVED_CASH_MISMATCH")
    if modeled_fees != state.modeled_fees:
        issues.append("MODELED_FEES_MISMATCH")
    if realized != state.realized_paper_pnl:
        issues.append("REALIZED_PAPER_PNL_MISMATCH")
    if state.cash < 0 or state.available_cash < 0:
        issues.append("NEGATIVE_PAPER_CASH")
    if calculated_reserved > MAX_PAPER_OPEN_RISK:
        issues.append("AGGREGATE_RISK_LIMIT_EXCEEDED")
    if debits != credits:
        issues.append("JOURNAL_IMBALANCE")
    if len({item.fill_id for item in state.fills}) != len(state.fills):
        issues.append("DUPLICATE_FILL_ID")
    if len({item.journal_id for item in state.journals}) != len(state.journals):
        issues.append("DUPLICATE_JOURNAL_ID")
    previews = {item.preview_id: item for item in state.previews}
    reservations = {item.order_id: item for item in state.reservations}
    for order in state.orders:
        filled = sum(
            (item.contracts for item in state.fills if item.order_id == order.order_id),
            0,
        )
        if (
            filled != order.filled_contracts
            or not 0 <= order.remaining_contracts <= order.contracts
        ):
            issues.append(f"ORDER_FILL_QUANTITY_MISMATCH:{order.order_id}")
        preview = previews.get(order.preview_id)
        reservation = reservations.get(order.order_id)
        if preview is None or reservation is None:
            issues.append(f"ORDER_EVIDENCE_MISSING:{order.order_id}")
            continue
        if reservation.original_amount != preview.reserved_risk:
            issues.append(f"RESERVATION_ORIGINAL_RISK_MISMATCH:{order.order_id}")
        expected_reservation = (
            order.limit_price * order.remaining_contracts
            + _fee(order.limit_price, order.remaining_contracts)
            if order.state
            in {
                KalshiPaperOrderState.OPEN,
                KalshiPaperOrderState.PARTIALLY_FILLED,
                KalshiPaperOrderState.CANCEL_REQUESTED,
            }
            else Decimal(0)
        )
        if reservation.current_amount != expected_reservation:
            issues.append(f"RESERVATION_CURRENT_RISK_MISMATCH:{order.order_id}")
        if (
            (order.state is KalshiPaperOrderState.OPEN and order.filled_contracts != 0)
            or (
                order.state is KalshiPaperOrderState.PARTIALLY_FILLED
                and not 0 < order.filled_contracts < order.contracts
            )
            or (
                order.state
                in {
                    KalshiPaperOrderState.FILLED,
                    KalshiPaperOrderState.SETTLED,
                }
                and order.filled_contracts != order.contracts
            )
        ):
            issues.append(f"ORDER_STATE_QUANTITY_MISMATCH:{order.order_id}")
    settlements_by_position = {item.position_id for item in state.settlements}
    for position in state.positions:
        settled = position.position_id in settlements_by_position
        if (position.state is KalshiPositionState.SETTLED) != settled:
            issues.append(f"POSITION_SETTLEMENT_MISMATCH:{position.position_id}")
    if state.open_risk > MAX_PAPER_OPEN_RISK:
        issues.append("AGGREGATE_OPEN_EXPOSURE_LIMIT_EXCEEDED")
    values = (
        _id(reconciliation_id, "KALSHI_PAPER_RECONCILIATION_ID_INVALID"),
        state.account.account_id,
        _time(reconciled_at, "KALSHI_PAPER_RECONCILIATION_TIME_INVALID"),
        KalshiReconciliationStatus.CLEAN if not issues else KalshiReconciliationStatus.DISCREPANCY,
        tuple(sorted(set(issues))),
        calculated_cash,
        state.cash,
        calculated_reserved,
        state.reserved_cash,
        open_exposure,
        modeled_fees,
        realized,
        debits,
        credits,
        "PAPER_ONLY",
        "NONE",
    )
    return KalshiPaperReconciliation(*values[:-2], content_digest(values), *values[-2:])
