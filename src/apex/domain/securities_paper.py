"""Pure governed Securities PAPER execution and accounting evidence.

The prediction-market Order and Position types deliberately encode binary
contract rules and prices in ``[0, 1]``.  Continuous-market execution therefore
uses this narrow typed extension while retaining Apex Campaign, governance,
trial, risk, and ledger authority in the application layer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from apex.domain.securities import (
    SecuritiesAssetClass,
    SecuritiesBarEvidence,
    SecuritiesCorporateActionEvidence,
    SecuritiesCorporateActionType,
    SecuritiesSignal,
    SecuritiesSignalDirection,
)
from apex.domain.values import (
    ContentHash,
    DecimalInput,
    Money,
    Quantity,
    RiskFraction,
    content_digest,
    deterministic_id,
    parse_decimal,
)

SECURITIES_PAPER_WARNING = "SECURITIES PAPER — SIMULATED CAPITAL ONLY"
NO_BROKER_WARNING = "NO BROKER OR LIVE MARKET AUTHORITY"
EXTERNAL_ENGINE_WARNING = "EXTERNAL ENGINE OUTPUT IS EVIDENCE, NOT APEX AUTHORITY"
SECURITIES_EXECUTION_MODEL = "SECURITIES_NEXT_BAR_CROSS_V1"


class SecuritiesPaperError(ValueError):
    """A governed Securities PAPER invariant was violated."""


class SecuritiesPaperRunState(StrEnum):
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class SecuritiesOrderAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SecuritiesOrderState(StrEnum):
    READY = "READY"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"


class SecuritiesFillState(StrEnum):
    NO_FILL = "NO_FILL"
    PARTIAL_FILL = "PARTIAL_FILL"
    FULL_FILL = "FULL_FILL"


class SecuritiesExecutionSource(StrEnum):
    INTERNAL_SIMULATION = "INTERNAL_SIMULATION"
    EXTERNAL_PAPER_VALIDATED_FILL = "EXTERNAL_PAPER_VALIDATED_FILL"


class SecuritiesPositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    DELISTING_BLOCKED = "DELISTING_BLOCKED"


class SecuritiesActionApplicationState(StrEnum):
    APPLIED = "APPLIED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    REJECTED_INTEGRITY = "REJECTED_INTEGRITY"
    ALREADY_APPLIED = "ALREADY_APPLIED"


class SecuritiesExitReason(StrEnum):
    FIXED_HORIZON = "FIXED_HORIZON"
    STRATEGY_FLAT = "STRATEGY_FLAT"
    TERMINAL_RUN_DATE = "TERMINAL_RUN_DATE"
    CAMPAIGN_KILL = "CAMPAIGN_KILL"
    RISK_REDUCTION = "RISK_REDUCTION"
    DELISTING = "DELISTING"
    OPERATOR_EXIT = "OPERATOR_EXIT"


class SecuritiesPaperConformanceClassification(StrEnum):
    CONFORMANT = "CONFORMANT"
    EXPLAINED_DIFFERENCE = "EXPLAINED_DIFFERENCE"
    MATERIAL_MISMATCH = "MATERIAL_MISMATCH"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    INSUFFICIENT_COMPARABLE_OUTPUT = "INSUFFICIENT_COMPARABLE_OUTPUT"


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SecuritiesPaperError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _positive(value: DecimalInput, label: str) -> Decimal:
    parsed = parse_decimal(value, allow_zero=False)
    if parsed <= 0:
        raise SecuritiesPaperError(f"{label} must be positive")
    return parsed


def _signed(value: DecimalInput) -> Decimal:
    return parse_decimal(value, allow_negative=True)


def _floor(value: Decimal, increment: Decimal) -> Decimal:
    steps = (value / increment).to_integral_value(rounding=ROUND_FLOOR)
    return parse_decimal(steps * increment)


@dataclass(frozen=True, slots=True)
class SecuritiesExecutionPolicy:
    policy_id: str
    policy_version: str
    execution_model: str
    permitted_directions: tuple[SecuritiesOrderAction, ...]
    permitted_asset_types: tuple[SecuritiesAssetClass, ...]
    fill_price_rule: str
    decision_to_fill_delay_rule: str
    half_spread_per_share: Decimal
    slippage_per_share: Decimal
    commission_per_share: Decimal
    commission_rule: str
    volume_participation_cap: RiskFraction
    minimum_quantity: Decimal
    maximum_quantity: Decimal
    quantity_increment: Decimal
    unfilled_expiration_sessions: int
    allow_partial_fills: bool
    session_policy: str
    corporate_action_policy: str
    created_at: datetime
    superseded_at: datetime | None
    canonical_payload: dict[str, object]
    canonical_hash: ContentHash

    @classmethod
    def next_bar_cross_v1(
        cls,
        *,
        created_at: datetime,
        half_spread_per_share: DecimalInput = "0.01",
        slippage_per_share: DecimalInput = "0.01",
        commission_per_share: DecimalInput = "0.005",
        volume_participation_cap: DecimalInput = "0.1",
        minimum_quantity: DecimalInput = "1",
        maximum_quantity: DecimalInput = "1000",
        quantity_increment: DecimalInput = "1",
    ) -> SecuritiesExecutionPolicy:
        created = _aware(created_at, "execution policy creation")
        half_spread = parse_decimal(half_spread_per_share)
        slippage = parse_decimal(slippage_per_share)
        commission = parse_decimal(commission_per_share)
        minimum = _positive(minimum_quantity, "minimum quantity")
        maximum = _positive(maximum_quantity, "maximum quantity")
        increment = _positive(quantity_increment, "quantity increment")
        participation = RiskFraction(volume_participation_cap)
        if participation.value <= 0:
            raise SecuritiesPaperError("volume participation must be positive")
        if minimum > maximum or minimum % increment or maximum % increment:
            raise SecuritiesPaperError("execution quantity bounds must align to the increment")
        payload: dict[str, object] = {
            "policy_version": "v1",
            "execution_model": SECURITIES_EXECUTION_MODEL,
            "permitted_directions": (SecuritiesOrderAction.BUY, SecuritiesOrderAction.SELL),
            "permitted_asset_types": (
                SecuritiesAssetClass.COMMON_STOCK,
                SecuritiesAssetClass.ETF,
            ),
            "fill_price_rule": "NEXT_ELIGIBLE_BAR_OPEN_PLUS_OR_MINUS_COSTS",
            "decision_to_fill_delay_rule": "STRICTLY_LATER_ELIGIBLE_SESSION",
            "half_spread_per_share": half_spread,
            "slippage_per_share": slippage,
            "commission_per_share": commission,
            "volume_participation_cap": participation,
            "minimum_quantity": minimum,
            "maximum_quantity": maximum,
            "quantity_increment": increment,
            "allow_partial_fills": True,
            "unfilled_expiration_sessions": 1,
            "session_policy": "REGULAR_SESSION_ONLY",
            "corporate_action_policy": "RETAINED_EVIDENCE_ONLY_V1",
        }
        policy_hash = ContentHash(content_digest(payload))
        return cls(
            policy_id=deterministic_id("securities_execution_policy", payload),
            policy_version="v1",
            execution_model=SECURITIES_EXECUTION_MODEL,
            permitted_directions=(SecuritiesOrderAction.BUY, SecuritiesOrderAction.SELL),
            permitted_asset_types=(
                SecuritiesAssetClass.COMMON_STOCK,
                SecuritiesAssetClass.ETF,
            ),
            fill_price_rule="NEXT_ELIGIBLE_BAR_OPEN_PLUS_OR_MINUS_COSTS",
            decision_to_fill_delay_rule="STRICTLY_LATER_ELIGIBLE_SESSION",
            half_spread_per_share=half_spread,
            slippage_per_share=slippage,
            commission_per_share=commission,
            commission_rule="PER_EXECUTED_SHARE",
            volume_participation_cap=participation,
            minimum_quantity=minimum,
            maximum_quantity=maximum,
            quantity_increment=increment,
            unfilled_expiration_sessions=1,
            allow_partial_fills=True,
            session_policy="REGULAR_SESSION_ONLY",
            corporate_action_policy="RETAINED_EVIDENCE_ONLY_V1",
            created_at=created,
            superseded_at=None,
            canonical_payload=payload,
            canonical_hash=policy_hash,
        )


@dataclass(frozen=True, slots=True)
class SecuritiesPaperRun:
    run_id: str
    campaign_id: str
    trial_authorization_id: str
    strategy_id: str
    strategy_version: str
    strategy_configuration_hash: ContentHash
    data_batch_id: str
    data_batch_hash: ContentHash
    execution_policy_id: str
    execution_policy_hash: ContentHash
    start_date: str
    end_date: str
    state: SecuritiesPaperRunState
    signal_count: int
    opportunity_count: int
    accepted_opportunity_count: int
    rejected_opportunity_count: int
    order_count: int
    fill_count: int
    position_count: int
    closed_position_count: int
    mark_count: int
    corporate_action_application_count: int
    starting_cash: Money
    ending_cash: Money
    gross_realized_pnl: Decimal
    net_realized_pnl: Decimal
    maximum_drawdown: Decimal
    idempotency_key: str
    started_at: datetime
    completed_at: datetime | None
    failure_code: str | None
    canonical_input_hash: ContentHash
    canonical_result_hash: ContentHash | None


@dataclass(frozen=True, slots=True)
class SecuritiesPaperOpportunity:
    opportunity_id: str
    run_id: str
    # Engineering acceptance creates no research or SHADOW signal.  Ordinary
    # Securities PAPER opportunities always retain their originating signal.
    signal_id: str | None
    instrument_id: str
    decision_at: datetime
    earliest_fill_at: datetime
    reference_price: Decimal
    requested_quantity: Quantity
    intended_exposure: Money
    modeled_spread: Money
    modeled_slippage: Money
    modeled_commission: Money
    maximum_loss: Money
    holding_horizon_sessions: int
    evidence_bar_ids: tuple[str, ...]
    configuration_hash: ContentHash
    canonical_hash: ContentHash


def build_paper_opportunity(
    *,
    run_id: str,
    signal: SecuritiesSignal,
    next_bar: SecuritiesBarEvidence,
    requested_quantity: Quantity,
    policy: SecuritiesExecutionPolicy,
    holding_horizon_sessions: int,
) -> SecuritiesPaperOpportunity:
    """Bind a LONG signal to the next eligible bar without reading its price."""

    if signal.direction is not SecuritiesSignalDirection.LONG:
        raise SecuritiesPaperError("only LONG signals create Securities PAPER opportunities")
    if next_bar.instrument_id != signal.instrument_id:
        raise SecuritiesPaperError("next-bar evidence belongs to another instrument")
    if next_bar.period_start <= signal.signal_at or next_bar.available_at <= signal.signal_at:
        raise SecuritiesPaperError("PAPER fill evidence must be strictly later than the signal")
    if requested_quantity.increment != policy.quantity_increment:
        raise SecuritiesPaperError("requested quantity increment does not match execution policy")
    if not policy.minimum_quantity <= requested_quantity.value <= policy.maximum_quantity:
        raise SecuritiesPaperError("requested quantity is outside execution policy bounds")
    reference = parse_decimal(signal.reference_close, allow_zero=False)
    spread = Money(policy.half_spread_per_share * requested_quantity.value)
    slippage = Money(policy.slippage_per_share * requested_quantity.value)
    commission = Money(policy.commission_per_share * requested_quantity.value)
    exposure = Money(reference * requested_quantity.value)
    maximum_loss = Money(exposure.amount + spread.amount + slippage.amount + commission.amount)
    payload = {
        "run_id": run_id,
        "signal_id": signal.signal_id,
        "instrument_id": signal.instrument_id,
        "decision_at": signal.signal_at,
        "earliest_fill_at": next_bar.period_start,
        "reference_price": reference,
        "requested_quantity": requested_quantity,
        "intended_exposure": exposure,
        "modeled_spread": spread,
        "modeled_slippage": slippage,
        "modeled_commission": commission,
        "maximum_loss": maximum_loss,
        "holding_horizon_sessions": holding_horizon_sessions,
        # Identity and timestamps are permitted at authorization.  OPEN/HLCV are
        # deliberately absent so authorization cannot read the future fill price.
        "eligible_bar_identity": next_bar.bar_id,
        "eligible_bar_period_start": next_bar.period_start,
        "eligible_bar_available_at": next_bar.available_at,
        "evidence_bar_ids": signal.source_bar_ids,
        "execution_policy_hash": policy.canonical_hash,
    }
    canonical_hash = ContentHash(content_digest(payload))
    return SecuritiesPaperOpportunity(
        opportunity_id=deterministic_id("securities_opportunity", payload),
        run_id=run_id,
        signal_id=signal.signal_id,
        instrument_id=signal.instrument_id,
        decision_at=_aware(signal.signal_at, "opportunity decision"),
        earliest_fill_at=_aware(next_bar.period_start, "earliest fill"),
        reference_price=reference,
        requested_quantity=requested_quantity,
        intended_exposure=exposure,
        modeled_spread=spread,
        modeled_slippage=slippage,
        modeled_commission=commission,
        maximum_loss=maximum_loss,
        holding_horizon_sessions=holding_horizon_sessions,
        evidence_bar_ids=signal.source_bar_ids,
        configuration_hash=policy.canonical_hash,
        canonical_hash=canonical_hash,
    )


@dataclass(frozen=True, slots=True)
class SecuritiesPaperAuthorization:
    authorization_id: str
    opportunity_id: str
    allocation_id: str
    risk_decision_id: str
    reservation_id: str
    accepted: bool
    quantity: Quantity
    maximum_loss: Money
    reason_codes: tuple[str, ...]
    canonical_hash: ContentHash


def authorize_paper_opportunity(
    *,
    opportunity: SecuritiesPaperOpportunity,
    settled_cash: Money,
    risk_capacity: Money,
    active_risk: Money,
    maximum_per_opportunity_risk: Money,
    maximum_strategy_active_risk: Money,
    trial_cumulative_remaining_risk: Money,
) -> SecuritiesPaperAuthorization:
    """Apply bounded capital/risk/trial limits before creating an Order."""

    caps = (
        settled_cash.amount,
        risk_capacity.amount,
        maximum_per_opportunity_risk.amount,
        max(Decimal(0), maximum_strategy_active_risk.amount - active_risk.amount),
        trial_cumulative_remaining_risk.amount,
    )
    per_share_loss = opportunity.maximum_loss.amount / opportunity.requested_quantity.value
    quantity_value = min(
        opportunity.requested_quantity.value,
        *(cap / per_share_loss for cap in caps),
    )
    quantity_value = _floor(quantity_value, opportunity.requested_quantity.increment)
    quantity = Quantity(quantity_value, opportunity.requested_quantity.increment)
    maximum_loss = Money(per_share_loss * quantity.value)
    accepted = quantity.value > 0
    reasons = (
        ("ALL_SECURITIES_RISK_INVARIANTS_PASSED",)
        if accepted
        else ("SECURITIES_RISK_CAPACITY_EXHAUSTED",)
    )
    payload = {
        "opportunity_id": opportunity.opportunity_id,
        "quantity": quantity,
        "maximum_loss": maximum_loss,
        "reason_codes": reasons,
    }
    canonical_hash = ContentHash(content_digest(payload))
    return SecuritiesPaperAuthorization(
        authorization_id=deterministic_id("securities_authorization", payload),
        opportunity_id=opportunity.opportunity_id,
        allocation_id=deterministic_id("securities_allocation", payload),
        risk_decision_id=deterministic_id("securities_risk_decision", payload),
        reservation_id=deterministic_id("securities_reservation", payload),
        accepted=accepted,
        quantity=quantity,
        maximum_loss=maximum_loss,
        reason_codes=reasons,
        canonical_hash=canonical_hash,
    )


@dataclass(frozen=True, slots=True)
class SecuritiesPaperOrder:
    order_id: str
    run_id: str
    opportunity_id: str | None
    authorization_id: str
    position_id: str | None
    instrument_id: str
    action: SecuritiesOrderAction
    requested_quantity: Quantity
    filled_quantity: Quantity
    reserved_amount: Money
    decision_at: datetime
    earliest_fill_at: datetime
    expires_at: datetime
    state: SecuritiesOrderState
    last_sequence: int
    idempotency_key: str
    canonical_hash: ContentHash


def create_securities_order(
    *,
    run_id: str,
    instrument_id: str,
    action: SecuritiesOrderAction,
    quantity: Quantity,
    reserved_amount: Money,
    decision_at: datetime,
    earliest_fill_at: datetime,
    expires_at: datetime,
    idempotency_key: str,
    authorization_id: str,
    opportunity_id: str | None = None,
    position_id: str | None = None,
) -> SecuritiesPaperOrder:
    decision = _aware(decision_at, "order decision")
    earliest = _aware(earliest_fill_at, "order eligibility")
    expiry = _aware(expires_at, "order expiry")
    if not decision < earliest <= expiry:
        raise SecuritiesPaperError("order timing must preserve next-bar eligibility")
    if action is SecuritiesOrderAction.BUY and opportunity_id is None:
        raise SecuritiesPaperError("BUY Order requires its authorized opportunity")
    if action is SecuritiesOrderAction.SELL and position_id is None:
        raise SecuritiesPaperError("SELL Order requires its Position")
    payload = {
        "run_id": run_id,
        "instrument_id": instrument_id,
        "action": action,
        "quantity": quantity,
        "reserved_amount": reserved_amount,
        "decision_at": decision,
        "earliest_fill_at": earliest,
        "expires_at": expiry,
        "idempotency_key": idempotency_key,
        "authorization_id": authorization_id,
        "opportunity_id": opportunity_id,
        "position_id": position_id,
    }
    canonical_hash = ContentHash(content_digest(payload))
    return SecuritiesPaperOrder(
        order_id=deterministic_id("securities_order", payload),
        run_id=run_id,
        opportunity_id=opportunity_id,
        authorization_id=authorization_id,
        position_id=position_id,
        instrument_id=instrument_id,
        action=action,
        requested_quantity=quantity,
        filled_quantity=Quantity(0, quantity.increment),
        reserved_amount=reserved_amount,
        decision_at=decision,
        earliest_fill_at=earliest,
        expires_at=expiry,
        state=SecuritiesOrderState.READY,
        last_sequence=0,
        idempotency_key=idempotency_key,
        canonical_hash=canonical_hash,
    )


@dataclass(frozen=True, slots=True)
class SecuritiesFillEvidence:
    fill_id: str
    run_id: str
    order_id: str
    position_id: str | None
    instrument_id: str
    action: SecuritiesOrderAction
    quantity: Quantity
    fill_price: Decimal | None
    notional: Money
    modeled_spread: Money
    modeled_slippage: Money
    modeled_commission: Money
    decision_at: datetime
    eligible_fill_at: datetime
    occurred_at: datetime
    bar_id: str
    execution_policy_hash: ContentHash
    state: SecuritiesFillState
    reason_codes: tuple[str, ...]
    canonical_hash: ContentHash
    increment_ordinal: int = 1
    cumulative_quantity: Quantity | None = None
    remaining_quantity: Quantity | None = None
    execution_source: SecuritiesExecutionSource = SecuritiesExecutionSource.INTERNAL_SIMULATION
    source_evidence_id: str | None = None
    economic_identity_hash: ContentHash | None = None


@dataclass(frozen=True, slots=True)
class AppliedSecuritiesFill:
    order: SecuritiesPaperOrder
    fill: SecuritiesFillEvidence
    released_reservation: Money


def execute_next_bar_order(
    *,
    order: SecuritiesPaperOrder,
    bar: SecuritiesBarEvidence | None,
    policy: SecuritiesExecutionPolicy,
    available_cash: Money,
) -> AppliedSecuritiesFill:
    """Apply the exact next-bar OPEN, participation, cost, and expiry rules."""

    if order.state is not SecuritiesOrderState.READY:
        raise SecuritiesPaperError("only a READY Securities Order can execute")
    if bar is None:
        state = SecuritiesFillState.NO_FILL
        quantity = Quantity(0, order.requested_quantity.increment)
        price = None
        bar_id = "MISSING_NEXT_BAR"
        occurred_at = order.expires_at
        reasons = ("NEXT_ELIGIBLE_BAR_ABSENT",)
    else:
        if bar.instrument_id != order.instrument_id:
            raise SecuritiesPaperError("fill bar belongs to another instrument")
        if (
            bar.period_start < order.earliest_fill_at
            or bar.period_start <= order.decision_at
            or bar.available_at <= order.decision_at
        ):
            raise SecuritiesPaperError("same-bar or future-availability fill is forbidden")
        if bar.period_start > order.expires_at:
            raise SecuritiesPaperError("fill bar occurs after Order expiry")
        direction = Decimal(1) if order.action is SecuritiesOrderAction.BUY else Decimal(-1)
        price = parse_decimal(
            bar.open + direction * (policy.half_spread_per_share + policy.slippage_per_share),
            allow_zero=False,
        )
        participation_quantity = _floor(
            bar.volume * policy.volume_participation_cap.value,
            order.requested_quantity.increment,
        )
        reservation_quantity = (
            _floor(
                order.reserved_amount.amount / (price + policy.commission_per_share),
                order.requested_quantity.increment,
            )
            if order.action is SecuritiesOrderAction.BUY
            else order.requested_quantity.value
        )
        cash_quantity = (
            _floor(
                available_cash.amount / (price + policy.commission_per_share),
                order.requested_quantity.increment,
            )
            if order.action is SecuritiesOrderAction.BUY
            else order.requested_quantity.value
        )
        filled_value = min(
            order.requested_quantity.value,
            participation_quantity,
            reservation_quantity,
            cash_quantity,
        )
        filled_value = _floor(filled_value, order.requested_quantity.increment)
        quantity = Quantity(filled_value, order.requested_quantity.increment)
        if quantity.value == 0:
            state = SecuritiesFillState.NO_FILL
            reasons = ("VOLUME_OR_CASH_CAP_PRODUCED_NO_FILL",)
            price = None
        elif quantity.value == order.requested_quantity.value:
            state = SecuritiesFillState.FULL_FILL
            reasons = ("NEXT_BAR_OPEN_FULL_FILL",)
        else:
            state = SecuritiesFillState.PARTIAL_FILL
            reasons = ("VOLUME_PARTICIPATION_PARTIAL_FILL",)
        bar_id = bar.bar_id
        occurred_at = bar.period_start
    notional = Money((price or Decimal(0)) * quantity.value)
    spread = Money(policy.half_spread_per_share * quantity.value)
    slippage = Money(policy.slippage_per_share * quantity.value)
    commission = Money(policy.commission_per_share * quantity.value)
    payload = {
        "order_id": order.order_id,
        "action": order.action,
        "quantity": quantity,
        "fill_price": price,
        "notional": notional,
        "spread": spread,
        "slippage": slippage,
        "commission": commission,
        "bar_id": bar_id,
        "occurred_at": occurred_at,
        "execution_policy_hash": policy.canonical_hash,
        "state": state,
        "reason_codes": reasons,
    }
    fill = SecuritiesFillEvidence(
        fill_id=deterministic_id("securities_fill", payload),
        run_id=order.run_id,
        order_id=order.order_id,
        position_id=order.position_id,
        instrument_id=order.instrument_id,
        action=order.action,
        quantity=quantity,
        fill_price=price,
        notional=notional,
        modeled_spread=spread,
        modeled_slippage=slippage,
        modeled_commission=commission,
        decision_at=order.decision_at,
        eligible_fill_at=order.earliest_fill_at,
        occurred_at=_aware(occurred_at, "fill occurrence"),
        bar_id=bar_id,
        execution_policy_hash=policy.canonical_hash,
        state=state,
        reason_codes=reasons,
        canonical_hash=ContentHash(content_digest(payload)),
    )
    terminal_state = {
        SecuritiesFillState.NO_FILL: SecuritiesOrderState.EXPIRED,
        SecuritiesFillState.PARTIAL_FILL: SecuritiesOrderState.PARTIALLY_FILLED,
        SecuritiesFillState.FULL_FILL: SecuritiesOrderState.FILLED,
    }[state]
    released = (
        order.reserved_amount
        if quantity.value == 0
        else Money(
            max(
                Decimal(0),
                order.reserved_amount.amount - notional.amount - commission.amount,
            )
        )
    )
    return AppliedSecuritiesFill(
        order=replace(
            order,
            filled_quantity=quantity,
            reserved_amount=Money(0),
            state=terminal_state,
            last_sequence=2,
        ),
        fill=fill,
        released_reservation=released,
    )


@dataclass(frozen=True, slots=True)
class SecuritiesPositionState:
    position_id: str
    run_id: str
    campaign_id: str
    instrument_id: str
    current_symbol: str
    open_quantity: Quantity
    average_cost: Decimal
    cumulative_purchase_cost: Money
    cumulative_sale_proceeds: Money
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    last_mark_price: Decimal | None
    last_mark_at: datetime | None
    state: SecuritiesPositionStatus
    originating_fill_id: str
    row_version: int
    canonical_hash: ContentHash


def position_from_buy_fill(
    *,
    campaign_id: str,
    symbol: str,
    fill: SecuritiesFillEvidence,
) -> SecuritiesPositionState:
    if (
        fill.action is not SecuritiesOrderAction.BUY
        or fill.quantity.value <= 0
        or fill.fill_price is None
    ):
        raise SecuritiesPaperError("a Position requires a positive BUY fill")
    payload = {
        "campaign_id": campaign_id,
        "run_id": fill.run_id,
        "instrument_id": fill.instrument_id,
        "symbol": symbol,
        "originating_fill_id": fill.fill_id,
    }
    position_id = deterministic_id("securities_position", payload)
    return SecuritiesPositionState(
        position_id=position_id,
        run_id=fill.run_id,
        campaign_id=campaign_id,
        instrument_id=fill.instrument_id,
        current_symbol=symbol,
        open_quantity=fill.quantity,
        average_cost=fill.fill_price,
        cumulative_purchase_cost=fill.notional,
        cumulative_sale_proceeds=Money(0),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        last_mark_price=None,
        last_mark_at=None,
        state=SecuritiesPositionStatus.OPEN,
        originating_fill_id=fill.fill_id,
        row_version=1,
        canonical_hash=ContentHash(content_digest(payload)),
    )


def add_buy_fill_to_position(
    *, position: SecuritiesPositionState, fill: SecuritiesFillEvidence
) -> SecuritiesPositionState:
    if (
        fill.action is not SecuritiesOrderAction.BUY
        or fill.quantity.value <= 0
        or fill.fill_price is None
        or position.instrument_id != fill.instrument_id
        or position.state is not SecuritiesPositionStatus.OPEN
    ):
        raise SecuritiesPaperError("BUY fill cannot be added to Securities Position")
    quantity = Quantity(
        position.open_quantity.value + fill.quantity.value,
        position.open_quantity.increment,
    )
    purchase_cost = Money(position.cumulative_purchase_cost.amount + fill.notional.amount)
    average_cost = parse_decimal(purchase_cost.amount / quantity.value, allow_zero=False)
    return replace(
        position,
        open_quantity=quantity,
        average_cost=average_cost,
        cumulative_purchase_cost=purchase_cost,
        row_version=position.row_version + 1,
        canonical_hash=ContentHash(
            content_digest(
                {
                    "position_id": position.position_id,
                    "fill_id": fill.fill_id,
                    "quantity": quantity,
                    "average_cost": average_cost,
                    "purchase_cost": purchase_cost,
                }
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class SecuritiesMarkEvidence:
    mark_id: str
    run_id: str
    position_id: str
    instrument_id: str
    source_bar_id: str
    marked_at: datetime
    mark_price: Decimal
    quantity: Quantity
    market_value: Money
    unrealized_pnl: Decimal
    execution_policy_hash: ContentHash
    source_lineage_hash: ContentHash
    canonical_hash: ContentHash


@dataclass(frozen=True, slots=True)
class AppliedSecuritiesMark:
    position: SecuritiesPositionState
    mark: SecuritiesMarkEvidence


def mark_position(
    *,
    position: SecuritiesPositionState,
    bar: SecuritiesBarEvidence,
    policy: SecuritiesExecutionPolicy,
    as_of: datetime,
) -> AppliedSecuritiesMark:
    moment = _aware(as_of, "mark as_of")
    if position.state not in {
        SecuritiesPositionStatus.OPEN,
        SecuritiesPositionStatus.DELISTING_BLOCKED,
    }:
        raise SecuritiesPaperError("a closed Position cannot be marked")
    if bar.instrument_id != position.instrument_id:
        raise SecuritiesPaperError("mark evidence belongs to another instrument")
    if bar.available_at > moment or (
        position.last_mark_at is not None and bar.available_at <= position.last_mark_at
    ):
        raise SecuritiesPaperError("mark evidence is future, stale, or non-sequential")
    value = Money(bar.close * position.open_quantity.value)
    unrealized = _signed(value.amount - position.average_cost * position.open_quantity.value)
    payload = {
        "position_id": position.position_id,
        "source_bar_id": bar.bar_id,
        "marked_at": moment,
        "mark_price": bar.close,
        "quantity": position.open_quantity,
        "market_value": value,
        "unrealized_pnl": unrealized,
        "execution_policy_hash": policy.canonical_hash,
        "source_lineage_hash": bar.content_hash,
    }
    mark = SecuritiesMarkEvidence(
        mark_id=deterministic_id("securities_mark", payload),
        run_id=position.run_id,
        position_id=position.position_id,
        instrument_id=position.instrument_id,
        source_bar_id=bar.bar_id,
        marked_at=moment,
        mark_price=bar.close,
        quantity=position.open_quantity,
        market_value=value,
        unrealized_pnl=unrealized,
        execution_policy_hash=policy.canonical_hash,
        source_lineage_hash=ContentHash(bar.content_hash),
        canonical_hash=ContentHash(content_digest(payload)),
    )
    return AppliedSecuritiesMark(
        position=replace(
            position,
            unrealized_pnl=unrealized,
            last_mark_price=bar.close,
            last_mark_at=moment,
            row_version=position.row_version + 1,
            canonical_hash=ContentHash(
                content_digest(
                    {
                        "position_id": position.position_id,
                        "mark_id": mark.mark_id,
                        "open_quantity": position.open_quantity,
                        "unrealized_pnl": unrealized,
                    }
                )
            ),
        ),
        mark=mark,
    )


@dataclass(frozen=True, slots=True)
class SecuritiesCorporateActionApplication:
    application_id: str
    action_batch_id: str
    action_id: str
    position_id: str
    applied_at: datetime
    prior_quantity: Quantity
    resulting_quantity: Quantity
    prior_cost_basis: Money
    resulting_cost_basis: Money
    cash_entitlement: Money
    ledger_journal_id: str | None
    state: SecuritiesActionApplicationState
    reason_codes: tuple[str, ...]
    canonical_hash: ContentHash


@dataclass(frozen=True, slots=True)
class AppliedCorporateAction:
    position: SecuritiesPositionState
    application: SecuritiesCorporateActionApplication


def apply_corporate_action(
    *,
    position: SecuritiesPositionState,
    action: SecuritiesCorporateActionEvidence,
    action_batch_id: str,
    applied_at: datetime,
    quantity_increment: Decimal,
    terminal_value: Decimal | None = None,
) -> AppliedCorporateAction:
    moment = _aware(applied_at, "corporate action application")
    if action.instrument_id != position.instrument_id:
        raise SecuritiesPaperError("corporate action belongs to another instrument")
    if action.available_at > moment:
        raise SecuritiesPaperError("corporate action is not yet available")
    prior_cost = Money(position.average_cost * position.open_quantity.value)
    quantity = position.open_quantity
    average_cost = position.average_cost
    symbol = position.current_symbol
    cash = Money(0)
    state = SecuritiesActionApplicationState.APPLIED
    reasons: tuple[str, ...]
    position_state = position.state
    if action.action_type is SecuritiesCorporateActionType.SPLIT:
        assert action.split_ratio is not None
        resulting = action.split_ratio * quantity.value
        if resulting % quantity_increment:
            raise SecuritiesPaperError("split creates an unsupported fractional quantity")
        quantity = Quantity(resulting, quantity_increment)
        average_cost = parse_decimal(position.average_cost / action.split_ratio)
        reasons = ("SPLIT_COST_PRESERVED",)
    elif action.action_type is SecuritiesCorporateActionType.CASH_DIVIDEND:
        assert action.cash_amount is not None
        cash = Money(action.cash_amount * quantity.value)
        reasons = ("CASH_DIVIDEND_ENTITLEMENT",)
    elif action.action_type is SecuritiesCorporateActionType.SYMBOL_CHANGE:
        assert action.successor_symbol is not None
        symbol = action.successor_symbol
        reasons = ("SYMBOL_LINEAGE_PRESERVED",)
    else:
        if terminal_value is None:
            position_state = SecuritiesPositionStatus.DELISTING_BLOCKED
            state = SecuritiesActionApplicationState.REJECTED_INTEGRITY
            reasons = ("DELISTING_TERMINAL_VALUE_REQUIRED",)
        else:
            reasons = ("DELISTING_TERMINAL_VALUE_RETAINED",)
    resulting_cost = Money(average_cost * quantity.value)
    if action.action_type is SecuritiesCorporateActionType.SPLIT and resulting_cost != prior_cost:
        raise SecuritiesPaperError("split must preserve total economic cost")
    payload = {
        "action_batch_id": action_batch_id,
        "action_id": action.action_id,
        "position_id": position.position_id,
        "applied_at": moment,
        "prior_quantity": position.open_quantity,
        "resulting_quantity": quantity,
        "prior_cost_basis": prior_cost,
        "resulting_cost_basis": resulting_cost,
        "cash_entitlement": cash,
        "state": state,
        "reason_codes": reasons,
    }
    return AppliedCorporateAction(
        position=replace(
            position,
            current_symbol=symbol,
            open_quantity=quantity,
            average_cost=average_cost,
            state=position_state,
            row_version=position.row_version + 1,
            canonical_hash=ContentHash(
                content_digest(
                    {
                        "position_id": position.position_id,
                        "action_id": action.action_id,
                        "quantity": quantity,
                        "average_cost": average_cost,
                        "symbol": symbol,
                        "state": position_state,
                    }
                )
            ),
        ),
        application=SecuritiesCorporateActionApplication(
            application_id=deterministic_id("securities_action_application", payload),
            action_batch_id=action_batch_id,
            action_id=action.action_id,
            position_id=position.position_id,
            applied_at=moment,
            prior_quantity=position.open_quantity,
            resulting_quantity=quantity,
            prior_cost_basis=prior_cost,
            resulting_cost_basis=resulting_cost,
            cash_entitlement=cash,
            ledger_journal_id=None,
            state=state,
            reason_codes=reasons,
            canonical_hash=ContentHash(content_digest(payload)),
        ),
    )


@dataclass(frozen=True, slots=True)
class SecuritiesExitDecision:
    exit_decision_id: str
    run_id: str
    position_id: str
    instrument_id: str
    reason: SecuritiesExitReason
    decision_at: datetime
    earliest_fill_at: datetime
    source_identity: str
    canonical_input_hash: ContentHash
    canonical_result_hash: ContentHash


def build_exit_decision(
    *,
    position: SecuritiesPositionState,
    reason: SecuritiesExitReason,
    decision_at: datetime,
    next_bar: SecuritiesBarEvidence,
    source_identity: str,
) -> SecuritiesExitDecision:
    decision = _aware(decision_at, "exit decision")
    if next_bar.instrument_id != position.instrument_id or next_bar.period_start <= decision:
        raise SecuritiesPaperError("exit requires a later exact-instrument bar")
    payload = {
        "run_id": position.run_id,
        "position_id": position.position_id,
        "instrument_id": position.instrument_id,
        "reason": reason,
        "decision_at": decision,
        "earliest_fill_at": next_bar.period_start,
        "source_identity": source_identity,
    }
    digest = ContentHash(content_digest(payload))
    return SecuritiesExitDecision(
        exit_decision_id=deterministic_id("securities_exit", payload),
        run_id=position.run_id,
        position_id=position.position_id,
        instrument_id=position.instrument_id,
        reason=reason,
        decision_at=decision,
        earliest_fill_at=next_bar.period_start,
        source_identity=source_identity,
        canonical_input_hash=digest,
        canonical_result_hash=digest,
    )


@dataclass(frozen=True, slots=True)
class AppliedSecuritiesExit:
    position: SecuritiesPositionState
    realized_pnl: Decimal
    allocated_cost_basis: Money


def apply_sell_fill(
    *,
    position: SecuritiesPositionState,
    fill: SecuritiesFillEvidence,
) -> AppliedSecuritiesExit:
    if (
        fill.action is not SecuritiesOrderAction.SELL
        or fill.quantity.value <= 0
        or fill.fill_price is None
    ):
        raise SecuritiesPaperError("Position exit requires a positive SELL fill")
    if fill.instrument_id != position.instrument_id:
        raise SecuritiesPaperError("SELL fill belongs to another instrument")
    if fill.quantity.value > position.open_quantity.value:
        raise SecuritiesPaperError("SELL fill exceeds open Position quantity")
    allocated_cost = Money(position.average_cost * fill.quantity.value)
    realized = _signed(fill.notional.amount - allocated_cost.amount)
    remaining = Quantity(
        position.open_quantity.value - fill.quantity.value,
        position.open_quantity.increment,
    )
    state = (
        SecuritiesPositionStatus.CLOSED if remaining.value == 0 else SecuritiesPositionStatus.OPEN
    )
    cumulative_proceeds = Money(position.cumulative_sale_proceeds.amount + fill.notional.amount)
    total_realized = _signed(position.realized_pnl + realized)
    return AppliedSecuritiesExit(
        position=replace(
            position,
            open_quantity=remaining,
            cumulative_sale_proceeds=cumulative_proceeds,
            realized_pnl=total_realized,
            unrealized_pnl=(
                Decimal(0) if state is SecuritiesPositionStatus.CLOSED else position.unrealized_pnl
            ),
            state=state,
            row_version=position.row_version + 1,
            canonical_hash=ContentHash(
                content_digest(
                    {
                        "position_id": position.position_id,
                        "fill_id": fill.fill_id,
                        "remaining_quantity": remaining,
                        "realized_pnl": total_realized,
                        "state": state,
                    }
                )
            ),
        ),
        realized_pnl=realized,
        allocated_cost_basis=allocated_cost,
    )


@dataclass(frozen=True, slots=True)
class SecuritiesExecutionLineage:
    lineage_id: str
    run_id: str
    signal_id: str
    opportunity_id: str
    strategy_authority_decision_id: str
    trial_authorization_id: str
    allocation_id: str
    risk_decision_id: str
    reservation_id: str
    order_id: str
    fill_id: str | None
    position_id: str | None
    execution_policy_hash: ContentHash
    data_evidence_hash: ContentHash
    external_run_id: str | None
    canonical_hash: ContentHash


@dataclass(frozen=True, slots=True)
class ExternalSecuritiesPaperRun:
    external_run_id: str
    engine_identity: str
    engine_version: str
    state: str
    apex_run_id: str
    request_hash: ContentHash
    result_hash: ContentHash | None
    order_count: int
    fill_count: int
    position_count: int
    ending_cash: Money | None
    realized_pnl: Decimal | None
    maximum_drawdown: Decimal | None
    canonical_events: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SecuritiesPaperConformanceReport:
    report_id: str
    paper_run_id: str
    external_run_id: str
    classification: SecuritiesPaperConformanceClassification
    matching_event_count: int
    differing_event_count: int
    cash_difference: Decimal | None
    realized_pnl_difference: Decimal | None
    reason_codes: tuple[str, ...]
    canonical_input_hash: ContentHash
    canonical_result_hash: ContentHash


def compare_paper_execution(
    *,
    run: SecuritiesPaperRun,
    apex_events: tuple[tuple[str, str], ...],
    external: ExternalSecuritiesPaperRun,
) -> SecuritiesPaperConformanceReport:
    payload = {
        "paper_run_id": run.run_id,
        "external_run_id": external.external_run_id,
        "apex_result_hash": run.canonical_result_hash,
        "external_result_hash": external.result_hash,
    }
    input_hash = ContentHash(content_digest(payload))
    if external.state == "LEAN_RUNTIME_UNAVAILABLE":
        classification = SecuritiesPaperConformanceClassification.ENGINE_UNAVAILABLE
        matching = differing = 0
        reasons = ("LEAN_RUNTIME_UNAVAILABLE",)
    elif not external.canonical_events:
        classification = SecuritiesPaperConformanceClassification.INSUFFICIENT_COMPARABLE_OUTPUT
        matching = differing = 0
        reasons = ("EXTERNAL_EXECUTION_EVENTS_ABSENT",)
    else:
        matching = sum(item in external.canonical_events for item in apex_events)
        differing = max(len(apex_events), len(external.canonical_events)) - matching
        if differing == 0:
            classification = SecuritiesPaperConformanceClassification.CONFORMANT
            reasons = ("EXACT_PAPER_EXECUTION_MATCH",)
        elif differing <= 1:
            classification = SecuritiesPaperConformanceClassification.EXPLAINED_DIFFERENCE
            reasons = ("BOUNDED_EXECUTION_DIFFERENCE",)
        else:
            classification = SecuritiesPaperConformanceClassification.MATERIAL_MISMATCH
            reasons = ("MATERIAL_EXECUTION_DIFFERENCE",)
    cash_difference = (
        None
        if external.ending_cash is None
        else _signed(run.ending_cash.amount - external.ending_cash.amount)
    )
    pnl_difference = (
        None
        if external.realized_pnl is None
        else _signed(run.net_realized_pnl - external.realized_pnl)
    )
    result = {
        "classification": classification,
        "matching": matching,
        "differing": differing,
        "cash_difference": cash_difference,
        "realized_pnl_difference": pnl_difference,
        "reason_codes": reasons,
    }
    result_hash = ContentHash(content_digest(result))
    return SecuritiesPaperConformanceReport(
        report_id=deterministic_id(
            "securities_paper_conformance",
            {"input_hash": input_hash, "result_hash": result_hash},
        ),
        paper_run_id=run.run_id,
        external_run_id=external.external_run_id,
        classification=classification,
        matching_event_count=matching,
        differing_event_count=differing,
        cash_difference=cash_difference,
        realized_pnl_difference=pnl_difference,
        reason_codes=reasons,
        canonical_input_hash=input_hash,
        canonical_result_hash=result_hash,
    )


def date_in_instrument_lifecycle(
    *,
    session_date: date,
    valid_from: str,
    valid_to: str | None,
) -> bool:
    start = date.fromisoformat(valid_from)
    end = date.fromisoformat(valid_to) if valid_to is not None else None
    return session_date >= start and (end is None or session_date <= end)
