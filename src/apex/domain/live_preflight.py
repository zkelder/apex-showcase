"""Pure, immutable rules for non-executing LIVE-intent preflight evidence.

The types in this module are evidence only.  They do not own credentials,
provider transports, Orders, reservations, positions, journals, or authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from apex.domain.live_readiness import (
    ExecutionControlState,
    Provider,
    ReadinessRecommendation,
    assert_secret_free,
)
from apex.domain.values import canonical_decimal, content_digest, parse_decimal

LIVE_INTENT_CONTRACT_V1 = "LIVE_INTENT_CONTRACT_V1"
LIVE_INTENT_ELIGIBILITY_POLICY_V1 = "LIVE_INTENT_ELIGIBILITY_POLICY_V1"
LIVE_INTERNAL_PREFLIGHT_V1 = "LIVE_INTERNAL_PREFLIGHT_V1"
PROVIDER_PREVIEW_APPROVAL_V1 = "PROVIDER_PREVIEW_APPROVAL_V1"
PROVIDER_PREVIEW_CAPTURE_V1 = "PROVIDER_PREVIEW_CAPTURE_V1"
LIVE_PREFLIGHT_ASSESSMENT_V1 = "LIVE_PREFLIGHT_ASSESSMENT_V1"
LIVE_PREFLIGHT_WARNING = "LIVE PREFLIGHT IS EVIDENCE ONLY — NO LIVE AUTHORITY"
COINBASE_PREVIEW_HOST = "api.coinbase.com"
COINBASE_PREVIEW_PATH = "/api/v3/brokerage/orders/preview"
KALSHI_PREVIEW_LIMITATION = "KALSHI_NON_MUTATING_ORDER_PREVIEW_NOT_DOCUMENTED"


class LivePreflightError(ValueError):
    """Live-intent or preview evidence failed closed."""


class LiveIntentState(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class LiveOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class LiveIntentEligibilityState(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"


class InternalPreflightStatus(StrEnum):
    BLOCKED = "BLOCKED"
    READY_FOR_PROVIDER_PREVIEW = "READY_FOR_PROVIDER_PREVIEW"
    PROVIDER_PREVIEW_UNAVAILABLE = "PROVIDER_PREVIEW_UNAVAILABLE"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"


class ProviderPreviewStatus(StrEnum):
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    SUCCEEDED = "SUCCEEDED"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    MISMATCH = "MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"


class PreflightRecommendation(StrEnum):
    BLOCKED = "BLOCKED"
    REQUIRES_PRIVATE_CAPABILITY = "REQUIRES_PRIVATE_CAPABILITY"
    REQUIRES_PAPER_EVIDENCE = "REQUIRES_PAPER_EVIDENCE"
    REQUIRES_PROVIDER_PREVIEW = "REQUIRES_PROVIDER_PREVIEW"
    READY_FOR_LIVE_REVIEW = "READY_FOR_LIVE_REVIEW"


def _id(value: str, reason: str, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise LivePreflightError(reason)
    return value.strip()


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LivePreflightError(reason)
    return value.astimezone(UTC)


def _decimal(
    value: Decimal | str | int | None,
    reason: str,
    *,
    positive: bool = False,
) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = parse_decimal(value)
    except (TypeError, ValueError, ArithmeticError) as error:
        raise LivePreflightError(reason) from error
    if parsed < 0 or (positive and parsed <= 0):
        raise LivePreflightError(reason)
    return parsed


def _hash(value: str, reason: str) -> str:
    normalized = _id(value, reason, 64)
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise LivePreflightError(reason)
    return normalized


def _multiple(value: Decimal, increment: Decimal) -> bool:
    return increment > 0 and value % increment == 0


def _floor(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


@dataclass(frozen=True, slots=True)
class LiveIntentProposal:
    intent_id: str
    version: int
    source_opportunity_id: str
    source_opportunity_hash: str
    shared_projection_id: str
    shared_projection_hash: str
    allocation_decision_id: str
    allocation_decision_hash: str
    allocation_line_id: str
    allocation_amount: Decimal
    paper_trial_proposal_id: str
    paper_trial_proposal_hash: str
    provider: Provider
    account_scope_id: str
    product_or_market_id: str
    side: str
    order_type: LiveOrderType
    requested_quantity: Decimal | None
    requested_notional: Decimal | None
    limit_price: Decimal | None
    maximum_risk: Decimal
    strategy_or_method_version: str
    opportunity_observed_at: datetime
    allocation_decided_at: datetime
    capability_observed_at: datetime
    created_at: datetime
    expires_at: datetime
    state: LiveIntentState
    canonical_hash: str
    authority: str = "NONE"
    is_order: bool = False
    provider_order_id: str | None = None
    reservation_id: str | None = None
    capital_authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        intent_id: str,
        source_opportunity_id: str,
        source_opportunity_hash: str,
        shared_projection_id: str,
        shared_projection_hash: str,
        allocation_decision_id: str,
        allocation_decision_hash: str,
        allocation_line_id: str,
        allocation_amount: Decimal | str,
        paper_trial_proposal_id: str,
        paper_trial_proposal_hash: str,
        provider: Provider,
        account_scope_id: str,
        product_or_market_id: str,
        side: str,
        order_type: LiveOrderType,
        requested_quantity: Decimal | str | None,
        requested_notional: Decimal | str | None,
        limit_price: Decimal | str | None,
        maximum_risk: Decimal | str,
        strategy_or_method_version: str,
        opportunity_observed_at: datetime,
        allocation_decided_at: datetime,
        capability_observed_at: datetime,
        created_at: datetime,
        expires_at: datetime,
    ) -> LiveIntentProposal:
        quantity = _decimal(requested_quantity, "LIVE_INTENT_QUANTITY_INVALID", positive=True)
        notional = _decimal(requested_notional, "LIVE_INTENT_NOTIONAL_INVALID", positive=True)
        price = _decimal(limit_price, "LIVE_INTENT_LIMIT_PRICE_INVALID", positive=True)
        allocation = _decimal(allocation_amount, "LIVE_INTENT_ZERO_ALLOCATION", positive=True)
        risk = _decimal(maximum_risk, "LIVE_INTENT_MAXIMUM_RISK_INVALID", positive=True)
        assert allocation is not None and risk is not None
        if (quantity is None) == (notional is None):
            raise LivePreflightError("LIVE_INTENT_EXACTLY_ONE_SIZE_REQUIRED")
        if order_type is LiveOrderType.LIMIT and price is None:
            raise LivePreflightError("LIVE_INTENT_LIMIT_PRICE_REQUIRED")
        if order_type is LiveOrderType.MARKET and price is not None:
            raise LivePreflightError("LIVE_INTENT_MARKET_PRICE_PROHIBITED")
        created = _time(created_at, "LIVE_INTENT_CREATED_TIME_INVALID")
        expiry = _time(expires_at, "LIVE_INTENT_EXPIRATION_INVALID")
        evidence_times = (
            _time(opportunity_observed_at, "LIVE_INTENT_OPPORTUNITY_TIME_INVALID"),
            _time(allocation_decided_at, "LIVE_INTENT_ALLOCATION_TIME_INVALID"),
            _time(capability_observed_at, "LIVE_INTENT_CAPABILITY_TIME_INVALID"),
        )
        if expiry <= created or any(value > created for value in evidence_times):
            raise LivePreflightError("LIVE_INTENT_TIME_LINEAGE_INVALID")
        values = (
            _id(intent_id, "LIVE_INTENT_ID_INVALID"),
            1,
            _id(source_opportunity_id, "LIVE_INTENT_SOURCE_ID_INVALID"),
            _hash(source_opportunity_hash, "LIVE_INTENT_SOURCE_HASH_INVALID"),
            _id(shared_projection_id, "LIVE_INTENT_SHARED_ID_INVALID"),
            _hash(shared_projection_hash, "LIVE_INTENT_SHARED_HASH_INVALID"),
            _id(allocation_decision_id, "LIVE_INTENT_ALLOCATION_ID_INVALID"),
            _hash(allocation_decision_hash, "LIVE_INTENT_ALLOCATION_HASH_INVALID"),
            _id(allocation_line_id, "LIVE_INTENT_ALLOCATION_LINE_INVALID"),
            allocation,
            _id(paper_trial_proposal_id, "LIVE_INTENT_TRIAL_ID_INVALID"),
            _hash(paper_trial_proposal_hash, "LIVE_INTENT_TRIAL_HASH_INVALID"),
            provider,
            _id(account_scope_id, "LIVE_INTENT_ACCOUNT_SCOPE_INVALID"),
            _id(product_or_market_id, "LIVE_INTENT_PRODUCT_INVALID"),
            _id(side, "LIVE_INTENT_SIDE_INVALID", 20).upper(),
            order_type,
            quantity,
            notional,
            price,
            risk,
            _id(strategy_or_method_version, "LIVE_INTENT_METHOD_VERSION_INVALID"),
            *evidence_times,
            created,
            expiry,
            LiveIntentState.ACTIVE,
            "NONE",
            False,
            None,
            None,
            "NONE",
            "NONE",
        )
        assert_secret_free(values)
        return cls(
            intent_id=values[0],
            version=1,
            source_opportunity_id=values[2],
            source_opportunity_hash=values[3],
            shared_projection_id=values[4],
            shared_projection_hash=values[5],
            allocation_decision_id=values[6],
            allocation_decision_hash=values[7],
            allocation_line_id=values[8],
            allocation_amount=allocation,
            paper_trial_proposal_id=values[10],
            paper_trial_proposal_hash=values[11],
            provider=provider,
            account_scope_id=values[13],
            product_or_market_id=values[14],
            side=values[15],
            order_type=order_type,
            requested_quantity=quantity,
            requested_notional=notional,
            limit_price=price,
            maximum_risk=risk,
            strategy_or_method_version=values[21],
            opportunity_observed_at=evidence_times[0],
            allocation_decided_at=evidence_times[1],
            capability_observed_at=evidence_times[2],
            created_at=created,
            expires_at=expiry,
            state=LiveIntentState.ACTIVE,
            canonical_hash=content_digest((LIVE_INTENT_CONTRACT_V1, *values)),
        )

    def status_at(self, value: datetime) -> LiveIntentState:
        if self.state is LiveIntentState.INVALIDATED:
            return self.state
        return (
            LiveIntentState.EXPIRED
            if _time(value, "LIVE_INTENT_TIME_INVALID") >= self.expires_at
            else self.state
        )

    def invalidate(self, *, invalidated_at: datetime, reason: str) -> LiveIntentInvalidation:
        moment = _time(invalidated_at, "LIVE_INTENT_INVALIDATION_TIME_INVALID")
        if moment < self.created_at:
            raise LivePreflightError("LIVE_INTENT_INVALIDATION_TIME_INVALID")
        return LiveIntentInvalidation.create(intent=self, invalidated_at=moment, reason=reason)


@dataclass(frozen=True, slots=True)
class LiveIntentInvalidation:
    invalidation_id: str
    intent_id: str
    intent_hash: str
    reason: str
    invalidated_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls, *, intent: LiveIntentProposal, invalidated_at: datetime, reason: str
    ) -> LiveIntentInvalidation:
        values = (
            f"{intent.intent_id}-invalidation-{int(invalidated_at.timestamp())}",
            intent.intent_id,
            intent.canonical_hash,
            _id(reason, "LIVE_INTENT_INVALIDATION_REASON_INVALID"),
            invalidated_at,
            "NONE",
        )
        return cls(*values[:-1], content_digest(values), "NONE")


@dataclass(frozen=True, slots=True)
class LiveIntentEligibilityInputs:
    source_currently_eligible: bool
    allocation_nonzero: bool
    trial_proposal_lineage_valid: bool
    capability_current: bool
    account_constraints_available: bool
    product_constraints_available: bool
    fee_evidence_available: bool
    minimum_evidence_available: bool
    liquidity_sufficient: bool
    strategy_health_state: str
    paper_sample_count: int
    required_paper_sample_count: int
    reconciliation_quality: str
    operating_cycle_health: str
    backup_restore_verified: bool
    execution_control: ExecutionControlState
    safe_provider_permissions: bool
    live_risk_passed: bool
    explicit_operator_review: bool
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class LiveIntentEligibilityDecision:
    decision_id: str
    intent_id: str
    intent_hash: str
    policy_id: str
    state: LiveIntentEligibilityState
    blockers: tuple[str, ...]
    evaluated_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    live_authority: str = "NONE"


def evaluate_live_intent_eligibility(
    *, decision_id: str, intent: LiveIntentProposal, inputs: LiveIntentEligibilityInputs
) -> LiveIntentEligibilityDecision:
    now = _time(inputs.evaluated_at, "LIVE_INTENT_ELIGIBILITY_TIME_INVALID")
    blockers: list[str] = []
    checks = (
        (inputs.source_currently_eligible, "SOURCE_OPPORTUNITY_NOT_CURRENTLY_ELIGIBLE"),
        (inputs.allocation_nonzero, "NONZERO_ALLOCATION_REQUIRED"),
        (inputs.trial_proposal_lineage_valid, "PAPER_TRIAL_PROPOSAL_LINEAGE_INVALID"),
        (inputs.capability_current, "CURRENT_PRIVATE_CAPABILITY_MISSING"),
        (inputs.account_constraints_available, "ACCOUNT_CONSTRAINTS_MISSING"),
        (inputs.product_constraints_available, "PRODUCT_OR_MARKET_CONSTRAINTS_MISSING"),
        (inputs.fee_evidence_available, "FEE_EVIDENCE_MISSING"),
        (inputs.minimum_evidence_available, "MINIMUM_EVIDENCE_MISSING"),
        (inputs.liquidity_sufficient, "CURRENT_LIQUIDITY_INSUFFICIENT"),
        (inputs.strategy_health_state == "HEALTHY", "STRATEGY_HEALTH_NOT_HEALTHY"),
        (
            inputs.paper_sample_count >= inputs.required_paper_sample_count,
            "REQUIRED_PAPER_EVIDENCE_INSUFFICIENT",
        ),
        (inputs.reconciliation_quality == "CLEAN", "RECONCILIATION_NOT_CLEAN"),
        (inputs.operating_cycle_health == "HEALTHY", "OPERATING_CYCLE_NOT_HEALTHY"),
        (inputs.backup_restore_verified, "BACKUP_RESTORE_NOT_VERIFIED"),
        (
            inputs.execution_control is not ExecutionControlState.EMERGENCY_STOP,
            "EXECUTION_CONTROL_EMERGENCY_STOP",
        ),
        (inputs.safe_provider_permissions, "DANGEROUS_PROVIDER_PERMISSIONS"),
        (inputs.live_risk_passed, "LIVE_RISK_POLICY_BLOCKED"),
        (inputs.explicit_operator_review, "EXPLICIT_OPERATOR_REVIEW_REQUIRED"),
    )
    blockers.extend(reason for passed, reason in checks if not passed)
    if inputs.required_paper_sample_count <= 0 or inputs.paper_sample_count < 0:
        blockers.append("PAPER_EVIDENCE_COUNTS_INVALID")
    if intent.status_at(now) is LiveIntentState.EXPIRED:
        state = LiveIntentEligibilityState.EXPIRED
        blockers.append("LIVE_INTENT_EXPIRED")
    elif intent.state is LiveIntentState.INVALIDATED:
        state = LiveIntentEligibilityState.INVALID
        blockers.append("LIVE_INTENT_INVALIDATED")
    else:
        state = (
            LiveIntentEligibilityState.ELIGIBLE
            if not blockers
            else LiveIntentEligibilityState.BLOCKED
        )
    normalized = tuple(sorted(set(blockers)))
    values = (
        _id(decision_id, "LIVE_INTENT_ELIGIBILITY_ID_INVALID"),
        intent.intent_id,
        intent.canonical_hash,
        LIVE_INTENT_ELIGIBILITY_POLICY_V1,
        state,
        normalized,
        now,
        "NONE",
        "NONE",
    )
    return LiveIntentEligibilityDecision(*values[:7], content_digest(values), *values[7:])


@dataclass(frozen=True, slots=True)
class InternalPreflightInputs:
    reference_price: Decimal | str
    quantity_increment: Decimal | str
    price_increment: Decimal | str
    minimum_quantity: Decimal | str
    minimum_notional: Decimal | str
    fee_rate: Decimal | str
    slippage_rate: Decimal | str
    available_balance: Decimal | str | None
    displayed_liquidity_quantity: Decimal | str | None
    current_total_live_capital: Decimal | str
    maximum_total_live_capital: Decimal | str
    current_strategy_exposure: Decimal | str
    maximum_strategy_exposure: Decimal | str
    current_instrument_exposure: Decimal | str
    maximum_instrument_exposure: Decimal | str
    current_venue_exposure: Decimal | str
    maximum_venue_exposure: Decimal | str
    current_daily_loss: Decimal | str
    maximum_daily_loss: Decimal | str
    current_drawdown_ratio: Decimal | str
    maximum_drawdown_ratio: Decimal | str
    open_order_count: int
    maximum_open_orders: int
    open_position_count: int
    maximum_open_positions: int
    cash_before: Decimal | str
    minimum_retained_cash_ratio: Decimal | str
    provider_preview_supported: bool
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class InternalPreflight:
    preflight_id: str
    intent_id: str
    intent_hash: str
    eligibility_decision_id: str
    capability_snapshot_id: str
    capability_snapshot_hash: str
    readiness_assessment_id: str
    readiness_assessment_hash: str
    readiness_recommendation: ReadinessRecommendation
    risk_decision_id: str
    risk_decision_hash: str
    status: InternalPreflightStatus
    normalized_quantity: Decimal | None
    normalized_price: Decimal | None
    estimated_notional: Decimal
    estimated_fees: Decimal
    estimated_slippage: Decimal
    maximum_loss: Decimal
    available_balance: Decimal | None
    retained_cash_after: Decimal
    retained_cash_ratio_after: Decimal
    constraint_checks: tuple[tuple[str, str], ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    evaluated_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    provider_request_count: int = 0
    provider_write_count: int = 0
    order_created: bool = False
    reservation_created: bool = False
    live_authority: str = "NONE"


def run_internal_preflight(
    *,
    preflight_id: str,
    intent: LiveIntentProposal,
    eligibility: LiveIntentEligibilityDecision,
    capability_snapshot_id: str,
    capability_snapshot_hash: str,
    readiness_assessment_id: str,
    readiness_assessment_hash: str,
    readiness_recommendation: ReadinessRecommendation,
    risk_decision_id: str,
    risk_decision_hash: str,
    inputs: InternalPreflightInputs,
) -> InternalPreflight:
    now = _time(inputs.evaluated_at, "LIVE_PREFLIGHT_TIME_INVALID")
    decimals = {
        name: _decimal(value, f"LIVE_PREFLIGHT_{name.upper()}_INVALID")
        for name, value in (
            ("reference_price", inputs.reference_price),
            ("quantity_increment", inputs.quantity_increment),
            ("price_increment", inputs.price_increment),
            ("minimum_quantity", inputs.minimum_quantity),
            ("minimum_notional", inputs.minimum_notional),
            ("fee_rate", inputs.fee_rate),
            ("slippage_rate", inputs.slippage_rate),
            ("current_total_live_capital", inputs.current_total_live_capital),
            ("maximum_total_live_capital", inputs.maximum_total_live_capital),
            ("current_strategy_exposure", inputs.current_strategy_exposure),
            ("maximum_strategy_exposure", inputs.maximum_strategy_exposure),
            ("current_instrument_exposure", inputs.current_instrument_exposure),
            ("maximum_instrument_exposure", inputs.maximum_instrument_exposure),
            ("current_venue_exposure", inputs.current_venue_exposure),
            ("maximum_venue_exposure", inputs.maximum_venue_exposure),
            ("current_daily_loss", inputs.current_daily_loss),
            ("maximum_daily_loss", inputs.maximum_daily_loss),
            ("current_drawdown_ratio", inputs.current_drawdown_ratio),
            ("maximum_drawdown_ratio", inputs.maximum_drawdown_ratio),
            ("cash_before", inputs.cash_before),
            ("minimum_retained_cash_ratio", inputs.minimum_retained_cash_ratio),
        )
    }
    required_positive = ("reference_price", "quantity_increment", "price_increment")
    if any(value is None or value <= 0 for value in (decimals[name] for name in required_positive)):
        raise LivePreflightError("LIVE_PREFLIGHT_POSITIVE_CONSTRAINT_REQUIRED")
    available = _decimal(inputs.available_balance, "LIVE_PREFLIGHT_BALANCE_INVALID")
    liquidity = _decimal(inputs.displayed_liquidity_quantity, "LIVE_PREFLIGHT_LIQUIDITY_INVALID")
    price = intent.limit_price or decimals["reference_price"]
    assert price is not None
    quantity_increment = decimals["quantity_increment"]
    price_increment = decimals["price_increment"]
    assert quantity_increment is not None and price_increment is not None
    raw_quantity = (
        intent.requested_quantity
        if intent.requested_quantity is not None
        else (intent.requested_notional or Decimal(0)) / price
    )
    normalized_quantity = _floor(raw_quantity, quantity_increment)
    normalized_price = _floor(price, price_increment)
    blockers = list(eligibility.blockers)
    invalid: list[str] = []
    checks: list[tuple[str, str]] = []

    def check(name: str, passed: bool, reason: str, *, malformed: bool = False) -> None:
        checks.append((name, "PASS" if passed else "BLOCK"))
        if not passed:
            (invalid if malformed else blockers).append(reason)

    check(
        "QUANTITY_PRECISION",
        raw_quantity == normalized_quantity and normalized_quantity > 0,
        "QUANTITY_PRECISION_INVALID",
        malformed=True,
    )
    check(
        "PRICE_PRECISION",
        price == normalized_price,
        "PRICE_PRECISION_INVALID",
        malformed=True,
    )
    notional = normalized_quantity * normalized_price
    fee_rate = decimals["fee_rate"] or Decimal(0)
    slippage_rate = decimals["slippage_rate"] or Decimal(0)
    fees = notional * fee_rate
    slippage = notional * slippage_rate
    maximum_loss = notional + fees + slippage
    minimum_quantity = decimals["minimum_quantity"] or Decimal(0)
    minimum_notional = decimals["minimum_notional"] or Decimal(0)
    check("MINIMUM_QUANTITY", normalized_quantity >= minimum_quantity, "MINIMUM_QUANTITY_NOT_MET")
    check("MINIMUM_NOTIONAL", notional >= minimum_notional, "MINIMUM_NOTIONAL_NOT_MET")
    check("MAXIMUM_RISK", maximum_loss <= intent.maximum_risk, "INTENT_MAXIMUM_RISK_EXCEEDED")
    check("BALANCE", available is not None and available >= maximum_loss, "INSUFFICIENT_BALANCE")
    check(
        "LIQUIDITY",
        liquidity is not None and liquidity >= normalized_quantity,
        "LIQUIDITY_UNAVAILABLE",
    )
    exposure_checks = (
        (
            "TOTAL_LIVE_CAPITAL",
            decimals["current_total_live_capital"],
            decimals["maximum_total_live_capital"],
        ),
        (
            "STRATEGY_EXPOSURE",
            decimals["current_strategy_exposure"],
            decimals["maximum_strategy_exposure"],
        ),
        (
            "INSTRUMENT_EXPOSURE",
            decimals["current_instrument_exposure"],
            decimals["maximum_instrument_exposure"],
        ),
        ("VENUE_EXPOSURE", decimals["current_venue_exposure"], decimals["maximum_venue_exposure"]),
    )
    for name, current, maximum in exposure_checks:
        assert current is not None and maximum is not None
        check(name, current + maximum_loss <= maximum, f"{name}_LIMIT_EXCEEDED")
    assert decimals["current_daily_loss"] is not None and decimals["maximum_daily_loss"] is not None
    check(
        "DAILY_LOSS",
        decimals["current_daily_loss"] + maximum_loss <= decimals["maximum_daily_loss"],
        "DAILY_LOSS_LIMIT_EXCEEDED",
    )
    assert decimals["current_drawdown_ratio"] is not None
    assert decimals["maximum_drawdown_ratio"] is not None
    check(
        "DRAWDOWN",
        decimals["current_drawdown_ratio"] <= decimals["maximum_drawdown_ratio"],
        "DRAWDOWN_LIMIT_EXCEEDED",
    )
    check(
        "OPEN_ORDERS",
        inputs.open_order_count < inputs.maximum_open_orders,
        "OPEN_ORDER_LIMIT_EXCEEDED",
    )
    check(
        "OPEN_POSITIONS",
        inputs.open_position_count < inputs.maximum_open_positions,
        "OPEN_POSITION_LIMIT_EXCEEDED",
    )
    cash = decimals["cash_before"] or Decimal(0)
    retained = max(Decimal(0), cash - maximum_loss)
    retained_ratio = Decimal(0) if cash == 0 else retained / cash
    minimum_cash_ratio = decimals["minimum_retained_cash_ratio"] or Decimal(0)
    check("RETAINED_CASH", retained_ratio >= minimum_cash_ratio, "RETAINED_CASH_LIMIT_EXCEEDED")
    check(
        "READINESS",
        readiness_recommendation
        in {
            ReadinessRecommendation.LIVE_REVIEW_REQUIRED,
            ReadinessRecommendation.LIVE_PILOT_ELIGIBLE,
        },
        "LIVE_READINESS_NOT_SUFFICIENT",
    )
    if intent.status_at(now) is LiveIntentState.EXPIRED:
        status = InternalPreflightStatus.EXPIRED
        blockers.append("LIVE_INTENT_EXPIRED")
    elif invalid:
        status = InternalPreflightStatus.INVALID
        blockers.extend(invalid)
    elif blockers:
        status = InternalPreflightStatus.BLOCKED
    elif not inputs.provider_preview_supported:
        status = InternalPreflightStatus.PROVIDER_PREVIEW_UNAVAILABLE
    else:
        status = InternalPreflightStatus.READY_FOR_PROVIDER_PREVIEW
    warnings = (
        (KALSHI_PREVIEW_LIMITATION, "INTERNAL_PREFLIGHT_ONLY")
        if intent.provider is Provider.KALSHI
        else ()
    )
    values = (
        _id(preflight_id, "LIVE_PREFLIGHT_ID_INVALID"),
        intent.intent_id,
        intent.canonical_hash,
        eligibility.decision_id,
        _id(capability_snapshot_id, "LIVE_PREFLIGHT_CAPABILITY_ID_INVALID"),
        _hash(capability_snapshot_hash, "LIVE_PREFLIGHT_CAPABILITY_HASH_INVALID"),
        _id(readiness_assessment_id, "LIVE_PREFLIGHT_READINESS_ID_INVALID"),
        _hash(readiness_assessment_hash, "LIVE_PREFLIGHT_READINESS_HASH_INVALID"),
        readiness_recommendation,
        _id(risk_decision_id, "LIVE_PREFLIGHT_RISK_ID_INVALID"),
        _hash(risk_decision_hash, "LIVE_PREFLIGHT_RISK_HASH_INVALID"),
        status,
        normalized_quantity,
        normalized_price,
        notional,
        fees,
        slippage,
        maximum_loss,
        available,
        retained,
        retained_ratio,
        tuple(checks),
        tuple(sorted(set(blockers))),
        warnings,
        now,
        "NONE",
        0,
        0,
        False,
        False,
        "NONE",
    )
    return InternalPreflight(
        *values[:25],
        content_digest((LIVE_INTERNAL_PREFLIGHT_V1, *values)),
        *values[25:],
    )


@dataclass(frozen=True, slots=True)
class ProviderPreviewApproval:
    approval_id: str
    provider: Provider
    credential_reference_id: str
    intent_id: str
    intent_hash: str
    method: str
    host: str
    path: str
    maximum_request_count: int
    timeout_seconds: int
    approved_at: datetime
    expires_at: datetime
    actor: str
    reason: str
    idempotency_key: str
    canonical_hash: str
    authority: str = "NONE"
    preview_only: bool = True
    provider_write_count: int = 0
    order_authority: str = "NONE"
    cancellation_authority: str = "NONE"
    transfer_authority: str = "NONE"
    withdrawal_authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create_coinbase(
        cls,
        *,
        approval_id: str,
        credential_reference_id: str,
        intent: LiveIntentProposal,
        maximum_request_count: int,
        timeout_seconds: int,
        approved_at: datetime,
        expires_at: datetime,
        actor: str,
        reason: str,
        idempotency_key: str,
    ) -> ProviderPreviewApproval:
        if intent.provider is not Provider.COINBASE:
            raise LivePreflightError("PROVIDER_PREVIEW_COINBASE_INTENT_REQUIRED")
        start = _time(approved_at, "PROVIDER_PREVIEW_APPROVAL_TIME_INVALID")
        end = _time(expires_at, "PROVIDER_PREVIEW_APPROVAL_EXPIRATION_INVALID")
        if (
            not 1 <= maximum_request_count <= 3
            or not 1 <= timeout_seconds <= 30
            or end <= start
            or intent.status_at(start) is not LiveIntentState.ACTIVE
            or end > intent.expires_at
        ):
            raise LivePreflightError("PROVIDER_PREVIEW_APPROVAL_SCOPE_INVALID")
        values = (
            _id(approval_id, "PROVIDER_PREVIEW_APPROVAL_ID_INVALID"),
            Provider.COINBASE,
            _id(credential_reference_id, "PROVIDER_PREVIEW_CREDENTIAL_INVALID"),
            intent.intent_id,
            intent.canonical_hash,
            "POST",
            COINBASE_PREVIEW_HOST,
            COINBASE_PREVIEW_PATH,
            maximum_request_count,
            timeout_seconds,
            start,
            end,
            _id(actor, "PROVIDER_PREVIEW_ACTOR_INVALID"),
            _id(reason, "PROVIDER_PREVIEW_REASON_INVALID"),
            _id(idempotency_key, "PROVIDER_PREVIEW_IDEMPOTENCY_INVALID"),
            "NONE",
            True,
            0,
            "NONE",
            "NONE",
            "NONE",
            "NONE",
            "NONE",
        )
        assert_secret_free(values)
        return cls(
            *values[:15],
            content_digest((PROVIDER_PREVIEW_APPROVAL_V1, *values)),
            *values[15:],
        )

    def active_at(self, value: datetime) -> bool:
        moment = _time(value, "PROVIDER_PREVIEW_APPROVAL_TIME_INVALID")
        return self.approved_at <= moment < self.expires_at


_FORBIDDEN_PREVIEW_FIELDS = frozenset(
    {
        "client_order_id",
        "order_id",
        "preview_id",
        "leverage",
        "margin_type",
        "retail_portfolio_id",
        "attached_order_configuration",
        "transfer",
        "withdrawal",
        "cancel",
        "replace",
        "batch",
    }
)


def coinbase_preview_body(intent: LiveIntentProposal) -> dict[str, object]:
    size_key = "base_size" if intent.requested_quantity is not None else "quote_size"
    size_value = intent.requested_quantity or intent.requested_notional
    assert size_value is not None
    configuration: dict[str, object]
    if intent.order_type is LiveOrderType.MARKET:
        configuration = {"market_market_ioc": {size_key: canonical_decimal(size_value)}}
    else:
        assert intent.limit_price is not None
        configuration = {
            "limit_limit_gtc": {
                size_key: canonical_decimal(size_value),
                "limit_price": canonical_decimal(intent.limit_price),
                "post_only": False,
            }
        }
    return {
        "product_id": intent.product_or_market_id,
        "side": intent.side,
        "order_configuration": configuration,
    }


def validate_provider_preview_request(
    *,
    approval: ProviderPreviewApproval,
    intent: LiveIntentProposal,
    method: str,
    host: str,
    path: str,
    body: Mapping[str, object],
    requested_at: datetime,
    prior_request_count: int,
) -> None:
    """Exact pre-I/O firewall for the one supported non-mutating preview call."""

    if not approval.active_at(requested_at):
        raise LivePreflightError("PROVIDER_PREVIEW_APPROVAL_EXPIRED")
    if intent.status_at(requested_at) is not LiveIntentState.ACTIVE:
        raise LivePreflightError("PROVIDER_PREVIEW_INTENT_NOT_ACTIVE")
    if (
        approval.provider is not Provider.COINBASE
        or intent.provider is not Provider.COINBASE
        or approval.intent_id != intent.intent_id
        or approval.intent_hash != intent.canonical_hash
        or method != approval.method
        or host != approval.host
        or path != approval.path
        or prior_request_count < 0
        or prior_request_count >= approval.maximum_request_count
    ):
        raise LivePreflightError("PROVIDER_PREVIEW_REQUEST_SCOPE_MISMATCH")
    if body != coinbase_preview_body(intent):
        raise LivePreflightError("PROVIDER_PREVIEW_BODY_MISMATCH")

    def keys(value: object) -> set[str]:
        if isinstance(value, Mapping):
            return {str(key) for key in value} | {
                nested for item in value.values() for nested in keys(item)
            }
        if isinstance(value, (tuple, list)):
            return {nested for item in value for nested in keys(item)}
        return set()

    if keys(body) & _FORBIDDEN_PREVIEW_FIELDS:
        raise LivePreflightError("PROVIDER_PREVIEW_DANGEROUS_FIELD_REJECTED")
    assert_secret_free(body)


@dataclass(frozen=True, slots=True)
class ProviderPreviewCapture:
    capture_id: str
    approval_id: str | None
    intent_id: str
    intent_hash: str
    provider: Provider
    status: ProviderPreviewStatus
    request_identity: str | None
    sanitized_response_hash: str | None
    provider_validation_results: tuple[str, ...]
    estimated_fees: Decimal | None
    product_constraints: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    limitations: tuple[str, ...]
    captured_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    response_redacted: bool = True
    provider_request_count: int = 0
    provider_write_count: int = 0
    provider_order_id: str | None = None
    live_authority: str = "NONE"

    @classmethod
    def unavailable(
        cls, *, capture_id: str, intent: LiveIntentProposal, captured_at: datetime
    ) -> ProviderPreviewCapture:
        if intent.provider is not Provider.KALSHI:
            raise LivePreflightError("PROVIDER_PREVIEW_UNAVAILABLE_PROVIDER_INVALID")
        values = (
            _id(capture_id, "PROVIDER_PREVIEW_CAPTURE_ID_INVALID"),
            None,
            intent.intent_id,
            intent.canonical_hash,
            Provider.KALSHI,
            ProviderPreviewStatus.UNAVAILABLE,
            None,
            None,
            (),
            None,
            (),
            ("DO_NOT_CALL_ORDER_CREATION_TO_VALIDATE",),
            (KALSHI_PREVIEW_LIMITATION, "INTERNAL_PREFLIGHT_ONLY"),
            _time(captured_at, "PROVIDER_PREVIEW_CAPTURE_TIME_INVALID"),
            "NONE",
            True,
            0,
            0,
            None,
            "NONE",
        )
        return cls(
            *values[:14],
            content_digest((PROVIDER_PREVIEW_CAPTURE_V1, *values)),
            *values[14:],
        )

    @classmethod
    def create_coinbase(
        cls,
        *,
        capture_id: str,
        approval: ProviderPreviewApproval,
        intent: LiveIntentProposal,
        request_identity: str,
        sanitized_response_hash: str,
        provider_validation_results: Sequence[str],
        estimated_fees: Decimal | str | None,
        product_constraints: Sequence[tuple[str, str]],
        warnings: Sequence[str],
        captured_at: datetime,
        mismatch: bool = False,
    ) -> ProviderPreviewCapture:
        validations = tuple(sorted(set(provider_validation_results)))
        status = (
            ProviderPreviewStatus.MISMATCH
            if mismatch
            else ProviderPreviewStatus.PROVIDER_REJECTED
            if validations
            else ProviderPreviewStatus.SUCCEEDED
        )
        values = (
            _id(capture_id, "PROVIDER_PREVIEW_CAPTURE_ID_INVALID"),
            approval.approval_id,
            intent.intent_id,
            intent.canonical_hash,
            Provider.COINBASE,
            status,
            _id(request_identity, "PROVIDER_PREVIEW_REQUEST_ID_INVALID"),
            _hash(sanitized_response_hash, "PROVIDER_PREVIEW_RESPONSE_HASH_INVALID"),
            validations,
            _decimal(estimated_fees, "PROVIDER_PREVIEW_FEES_INVALID"),
            tuple(
                sorted(
                    (_id(key, "PROVIDER_PREVIEW_CONSTRAINT_INVALID"), str(value))
                    for key, value in product_constraints
                )
            ),
            tuple(sorted(set(_id(item, "PROVIDER_PREVIEW_WARNING_INVALID") for item in warnings))),
            (),
            _time(captured_at, "PROVIDER_PREVIEW_CAPTURE_TIME_INVALID"),
            "NONE",
            True,
            1,
            0,
            None,
            "NONE",
        )
        assert_secret_free(values)
        return cls(
            *values[:14],
            content_digest((PROVIDER_PREVIEW_CAPTURE_V1, *values)),
            *values[14:],
        )


@dataclass(frozen=True, slots=True)
class ProviderPreviewAttempt:
    attempt_id: str
    approval_id: str | None
    intent_id: str
    intent_hash: str
    provider: Provider
    status: ProviderPreviewStatus
    capture_id: str | None
    blocked_reason: str | None
    attempted_at: datetime
    request_count: int
    provider_write_count: int
    canonical_hash: str
    authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        attempt_id: str,
        approval_id: str | None,
        intent: LiveIntentProposal,
        status: ProviderPreviewStatus,
        capture_id: str | None,
        blocked_reason: str | None,
        attempted_at: datetime,
        request_count: int,
    ) -> ProviderPreviewAttempt:
        if request_count not in {0, 1} or (
            status is ProviderPreviewStatus.BLOCKED and request_count
        ):
            raise LivePreflightError("PROVIDER_PREVIEW_ATTEMPT_REQUEST_COUNT_INVALID")
        if status is ProviderPreviewStatus.SUCCEEDED and capture_id is None:
            raise LivePreflightError("PROVIDER_PREVIEW_ATTEMPT_CAPTURE_REQUIRED")
        values = (
            _id(attempt_id, "PROVIDER_PREVIEW_ATTEMPT_ID_INVALID"),
            approval_id,
            intent.intent_id,
            intent.canonical_hash,
            intent.provider,
            status,
            capture_id,
            blocked_reason,
            _time(attempted_at, "PROVIDER_PREVIEW_ATTEMPT_TIME_INVALID"),
            request_count,
            0,
            "NONE",
            "NONE",
        )
        return cls(*values[:11], content_digest(values), *values[11:])


@dataclass(frozen=True, slots=True)
class LivePreflightAssessment:
    assessment_id: str
    intent_id: str
    intent_hash: str
    capability_snapshot_id: str
    capability_snapshot_hash: str
    live_readiness_assessment_id: str
    live_readiness_assessment_hash: str
    risk_decision_id: str
    risk_decision_hash: str
    eligibility_decision_id: str
    internal_preflight_id: str
    internal_preflight_hash: str
    provider_preview_capture_id: str | None
    provider_preview_capture_hash: str | None
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    recommendation: PreflightRecommendation
    readiness_before: ReadinessRecommendation
    readiness_after: ReadinessRecommendation
    closed_readiness_blockers: tuple[str, ...]
    assessed_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    provider_write_count: int = 0
    order_created: bool = False
    reservation_created: bool = False
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        assessment_id: str,
        preflight: InternalPreflight,
        preview: ProviderPreviewCapture | None,
        assessed_at: datetime,
        readiness_blockers_before: Sequence[str] = (),
    ) -> LivePreflightAssessment:
        blockers = list(preflight.blockers)
        warnings = list(preflight.warnings)
        if "CURRENT_PRIVATE_CAPABILITY_MISSING" in blockers:
            recommendation = PreflightRecommendation.REQUIRES_PRIVATE_CAPABILITY
        elif "REQUIRED_PAPER_EVIDENCE_INSUFFICIENT" in blockers:
            recommendation = PreflightRecommendation.REQUIRES_PAPER_EVIDENCE
        elif preflight.status in {
            InternalPreflightStatus.BLOCKED,
            InternalPreflightStatus.EXPIRED,
            InternalPreflightStatus.INVALID,
        }:
            recommendation = PreflightRecommendation.BLOCKED
        elif (
            preflight.status is InternalPreflightStatus.READY_FOR_PROVIDER_PREVIEW
            and preview is None
        ):
            recommendation = PreflightRecommendation.REQUIRES_PROVIDER_PREVIEW
        elif preflight.status is InternalPreflightStatus.PROVIDER_PREVIEW_UNAVAILABLE or (
            preview is not None and preview.status is ProviderPreviewStatus.SUCCEEDED
        ):
            recommendation = PreflightRecommendation.READY_FOR_LIVE_REVIEW
        else:
            recommendation = PreflightRecommendation.BLOCKED
            if preview is not None:
                blockers.append(f"PROVIDER_PREVIEW_{preview.status.value}")
        if preview is not None:
            warnings.extend(preview.warnings)
            blockers.extend(preview.provider_validation_results)
        supported_closures = {
            "PRIVATE_ACCOUNT_VIEW_UNAVAILABLE": "BALANCE",
            "FEE_EVIDENCE_MISSING": "MINIMUM_NOTIONAL",
            "PRODUCT_CONSTRAINT_EVIDENCE_MISSING": "MINIMUM_QUANTITY",
            "LIQUIDITY_EVIDENCE_MISSING": "LIQUIDITY",
        }
        checks = dict(preflight.constraint_checks)
        closed = tuple(
            sorted(
                blocker
                for blocker in readiness_blockers_before
                if checks.get(supported_closures.get(blocker, "")) == "PASS"
            )
        )
        readiness_after = preflight.readiness_recommendation
        if preflight.readiness_recommendation is ReadinessRecommendation.LIVE_NOT_READY or set(
            readiness_blockers_before
        ) - set(closed):
            readiness_after = ReadinessRecommendation.LIVE_NOT_READY
        values = (
            _id(assessment_id, "LIVE_PREFLIGHT_ASSESSMENT_ID_INVALID"),
            preflight.intent_id,
            preflight.intent_hash,
            preflight.capability_snapshot_id,
            preflight.capability_snapshot_hash,
            preflight.readiness_assessment_id,
            preflight.readiness_assessment_hash,
            preflight.risk_decision_id,
            preflight.risk_decision_hash,
            preflight.eligibility_decision_id,
            preflight.preflight_id,
            preflight.canonical_hash,
            None if preview is None else preview.capture_id,
            None if preview is None else preview.canonical_hash,
            tuple(sorted(set(blockers))),
            tuple(sorted(set(warnings))),
            recommendation,
            preflight.readiness_recommendation,
            readiness_after,
            closed,
            _time(assessed_at, "LIVE_PREFLIGHT_ASSESSMENT_TIME_INVALID"),
            "NONE",
            0,
            False,
            False,
            "NONE",
        )
        return cls(
            *values[:21],
            content_digest((LIVE_PREFLIGHT_ASSESSMENT_V1, *values)),
            *values[21:],
        )


@dataclass(frozen=True, slots=True)
class LivePreflightOperatingTask:
    task_type: str
    disposition: str
    evidence_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    provider_request_count: int = 0
    provider_write_count: int = 0
    authority: str = "NONE"


def build_live_preflight_operating_tasks(
    *,
    intents: Sequence[LiveIntentProposal],
    preflights: Sequence[InternalPreflight],
    approvals: Sequence[ProviderPreviewApproval],
    assessments: Sequence[LivePreflightAssessment],
    as_of: datetime,
) -> tuple[LivePreflightOperatingTask, ...]:
    now = _time(as_of, "LIVE_PREFLIGHT_OPERATING_TIME_INVALID")
    active = tuple(item for item in intents if item.status_at(now) is LiveIntentState.ACTIVE)
    stale = tuple(item for item in preflights if (now - item.evaluated_at).total_seconds() > 900)
    due_previews = tuple(item for item in approvals if item.active_at(now))
    latest = None if not assessments else assessments[-1]
    tasks = (
        LivePreflightOperatingTask(
            "LIVE_INTENT_ELIGIBILITY_REVIEW",
            "REVIEW_REQUIRED" if active else "ZERO_INTENT",
            tuple(item.intent_id for item in active),
            () if active else ("NO_ELIGIBLE_INTENT_CONSTRUCTED",),
        ),
        LivePreflightOperatingTask(
            "STALE_PREFLIGHT_DETECTION",
            "STALE" if stale else "COMPLETED",
            tuple(item.preflight_id for item in stale),
            ("STALE_PREFLIGHT_REQUIRES_EXPLICIT_RERUN",) if stale else (),
        ),
        LivePreflightOperatingTask(
            "APPROVED_PROVIDER_PREVIEW_DUE_STATUS",
            "APPROVED_NOT_RUN" if due_previews else "NO_APPROVAL",
            tuple(item.approval_id for item in due_previews),
            ("DEFAULT_OPERATION_PERFORMS_ZERO_PRIVATE_REQUESTS",),
        ),
        LivePreflightOperatingTask(
            "PREFLIGHT_BLOCKER_CHANGES",
            "REVIEW_REQUIRED" if latest is not None and latest.blockers else "NO_CHANGE",
            () if latest is None else (latest.assessment_id,),
            () if latest is None else latest.blockers,
        ),
        LivePreflightOperatingTask(
            "READINESS_REASSESSMENT",
            "DUE" if latest is not None else "NO_INTENT",
            () if latest is None else (latest.assessment_id,),
            (
                "PREFLIGHT_CANNOT_CREATE_LIVE_PILOT_ELIGIBILITY",
                "EXPLICIT_REASSESSMENT_ONLY",
            ),
        ),
    )
    return tasks
