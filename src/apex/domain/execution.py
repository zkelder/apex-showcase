"""Immutable synthetic order, execution, position, and settlement state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256

from apex.domain.decisions import (
    AllocationDecision,
    OpportunityProposal,
    RestingLimitProposal,
    RiskDecision,
    Side,
    proposal_order_limit,
)
from apex.domain.market import (
    ContractRuleVersion,
    Instrument,
    VoidContractRuleVersion,
)
from apex.domain.market import (
    SettlementOutcome as SettlementOutcome,
)
from apex.domain.values import (
    ContentHash,
    Money,
    Price,
    Quantity,
    ValueValidationError,
    content_digest,
    deterministic_id,
    parse_decimal,
)


class ReservationState(StrEnum):
    HELD = "HELD"
    OPEN = "OPEN"
    RELEASED = "RELEASED"


class OrderState(StrEnum):
    READY = "READY"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


class ExecutionEventType(StrEnum):
    OPENED = "OPENED"
    FILL = "FILL"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


class SettlementState(StrEnum):
    OPEN = "OPEN"
    SETTLED = "SETTLED"


@dataclass(frozen=True, slots=True)
class RiskReservation:
    reservation_id: str
    campaign_id: str
    risk_decision_id: str
    order_id: str
    exact_order_fingerprint: str
    maximum_loss: Money
    state: ReservationState


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    client_order_id: str
    risk_decision_id: str
    proposal_id: str
    exact_order_fingerprint: str
    side: Side
    limit_price: Price
    quantity: Quantity
    filled_quantity: Quantity
    reserved_maximum_loss: Money
    state: OrderState
    last_execution_sequence: int

    @property
    def remaining_quantity(self) -> Quantity:
        return Quantity(
            self.quantity.value - self.filled_quantity.value,
            self.quantity.increment,
        )


@dataclass(frozen=True, slots=True)
class ExpiringLimitOrder(Order):
    executable_ask: Price
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.limit_price.tick != self.executable_ask.tick:
            raise ValueValidationError("order limit and executable ask ticks must match")
        if self.limit_price.value >= self.executable_ask.value:
            raise ValueValidationError("expiring YES order must be strictly nonmarketable")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueValidationError("order expiration must be timezone-aware")
        object.__setattr__(self, "expires_at", self.expires_at.astimezone(UTC))


def create_reserved_order(
    *,
    campaign_id: str,
    proposal: OpportunityProposal,
    allocation: AllocationDecision,
    risk_decision: RiskDecision,
) -> tuple[RiskReservation, Order]:
    """Create the exact reservation and order as one pure state transition."""

    if not risk_decision.accepted:
        raise ValueValidationError("a rejected risk decision cannot create an order")
    if risk_decision.reason_codes != ("ALL_INVARIANTS_PASSED",):
        raise ValueValidationError("accepted risk authority has invalid reason codes")
    if not allocation.accepted or allocation.quantity.value <= 0:
        raise ValueValidationError("order creation requires an accepted positive allocation")
    if risk_decision.allocation_id != allocation.allocation_id:
        raise ValueValidationError("risk decision does not authorize this allocation")
    if allocation.proposal_id != proposal.proposal_id:
        raise ValueValidationError("allocation does not belong to this proposal")
    if allocation.reason_codes != ("ALLOCATED",):
        raise ValueValidationError("accepted allocation has invalid reason codes")
    expected_maximum_loss = proposal.maximum_loss_per_unit.amount * allocation.quantity.value
    if allocation.maximum_loss.amount != expected_maximum_loss:
        raise ValueValidationError("allocation maximum loss does not match the exact order loss")
    expected_allocation_id = deterministic_id(
        "allocation",
        {
            "proposal_id": proposal.proposal_id,
            "quantity": allocation.quantity,
            "maximum_loss": allocation.maximum_loss,
            "snapshot_hash": allocation.snapshot_hash,
        },
    )
    if allocation.allocation_id != expected_allocation_id:
        raise ValueValidationError("allocation identity does not match its accepted terms")
    limit_price = proposal_order_limit(proposal)
    fingerprint_payload = {
        "proposal_id": proposal.proposal_id,
        "side": proposal.side,
        "limit_price": limit_price,
        "quantity": allocation.quantity,
        "rule_version_id": proposal.rule_version_id,
        "context_hash": risk_decision.context_hash,
    }
    if isinstance(proposal, RestingLimitProposal):
        fingerprint_payload["expires_at"] = proposal.order_expires_at
    if content_digest(fingerprint_payload) != risk_decision.exact_order_fingerprint:
        raise ValueValidationError("risk decision fingerprint does not authorize these order terms")
    expected_risk_decision_id = deterministic_id(
        "risk_decision",
        {
            "allocation_id": allocation.allocation_id,
            "context_hash": risk_decision.context_hash,
            "order_fingerprint": risk_decision.exact_order_fingerprint,
            "accepted": True,
            "reason_codes": (),
        },
    )
    if risk_decision.risk_decision_id != expected_risk_decision_id:
        raise ValueValidationError("risk decision identity does not match its accepted authority")
    order_payload = {
        "campaign_id": campaign_id,
        "proposal_id": proposal.proposal_id,
        "allocation_id": allocation.allocation_id,
        "risk_decision_id": risk_decision.risk_decision_id,
        "fingerprint": risk_decision.exact_order_fingerprint,
    }
    order_id = deterministic_id("order", order_payload)
    client_order_id = deterministic_id("client_order", order_payload)
    reservation = RiskReservation(
        reservation_id=deterministic_id("reservation", order_payload),
        campaign_id=campaign_id,
        risk_decision_id=risk_decision.risk_decision_id,
        order_id=order_id,
        exact_order_fingerprint=risk_decision.exact_order_fingerprint,
        maximum_loss=allocation.maximum_loss,
        state=ReservationState.HELD,
    )
    filled_quantity = Quantity(0, allocation.quantity.increment)
    order: Order
    if isinstance(proposal, RestingLimitProposal):
        order = ExpiringLimitOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            risk_decision_id=risk_decision.risk_decision_id,
            proposal_id=proposal.proposal_id,
            exact_order_fingerprint=risk_decision.exact_order_fingerprint,
            side=proposal.side,
            limit_price=limit_price,
            quantity=allocation.quantity,
            filled_quantity=filled_quantity,
            reserved_maximum_loss=allocation.maximum_loss,
            state=OrderState.READY,
            last_execution_sequence=0,
            executable_ask=proposal.executable_ask,
            expires_at=proposal.order_expires_at,
        )
    else:
        order = Order(
            order_id=order_id,
            client_order_id=client_order_id,
            risk_decision_id=risk_decision.risk_decision_id,
            proposal_id=proposal.proposal_id,
            exact_order_fingerprint=risk_decision.exact_order_fingerprint,
            side=proposal.side,
            limit_price=limit_price,
            quantity=allocation.quantity,
            filled_quantity=filled_quantity,
            reserved_maximum_loss=allocation.maximum_loss,
            state=OrderState.READY,
            last_execution_sequence=0,
        )
    return reservation, order


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    event_id: str
    order_id: str
    sequence: int
    event_type: ExecutionEventType
    occurred_at: datetime
    quantity: Quantity
    price: Price | None
    fee: Money
    payload_hash: ContentHash

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or self.sequence <= 0:
            raise ValueValidationError("execution sequence must be a positive integer")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueValidationError("execution time must be timezone-aware")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))


def validate_execution_event_identity(event: ExecutionEvent, script_id: str) -> None:
    """Verify a first-seen event against the simulator's canonical identity."""

    if not script_id:
        raise ValueValidationError("execution script identifier must not be empty")
    payload = {
        "order_id": event.order_id,
        "script_id": script_id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at,
        "quantity": event.quantity,
        "price": event.price,
        "fee": event.fee,
    }
    expected_hash = content_digest(payload)
    if event.payload_hash.value != expected_hash:
        raise ValueValidationError("execution event payload hash is not canonical")
    if event.event_id != deterministic_id("execution_event", payload):
        raise ValueValidationError("execution event identifier is not canonical")


@dataclass(frozen=True, slots=True)
class PositionState:
    instrument_id: str
    rule_version_id: str
    open_quantity: Quantity
    cost_basis: Money
    maximum_remaining_loss: Money
    settlement_state: SettlementState


@dataclass(frozen=True, slots=True)
class FilledPositionState(PositionState):
    side: Side
    originating_order_id: str
    originating_fill_event_id: str

    def __post_init__(self) -> None:
        if not self.instrument_id or not self.rule_version_id:
            raise ValueValidationError("filled position requires instrument and rule provenance")
        if self.open_quantity.value <= 0:
            raise ValueValidationError("filled position requires positive open quantity")
        if self.cost_basis.amount <= 0 or self.maximum_remaining_loss.amount <= 0:
            raise ValueValidationError("filled position requires positive cost and bounded risk")
        if self.settlement_state is not SettlementState.OPEN:
            raise ValueValidationError("filled position must remain unresolved")
        if not self.originating_order_id or not self.originating_fill_event_id:
            raise ValueValidationError("filled position requires order and fill provenance")


@dataclass(frozen=True, slots=True)
class AppliedExecution:
    order: Order
    reservation: RiskReservation
    position: PositionState


@dataclass(frozen=True, slots=True)
class AppliedUnfilledOrderEvent:
    order: ExpiringLimitOrder
    reservation: RiskReservation
    released_maximum_loss: Money


@dataclass(frozen=True, slots=True)
class AppliedPartialFillCanceledEvent:
    order: ExpiringLimitOrder
    reservation: RiskReservation
    position: FilledPositionState | None
    released_maximum_loss: Money


def apply_partial_fill_canceled_event(
    order: ExpiringLimitOrder,
    reservation: RiskReservation,
    position: FilledPositionState | None,
    event: ExecutionEvent,
    *,
    instrument_id: str,
    rule_version_id: str,
    maximum_loss_per_unit: Money,
    fee_per_unit: Money,
) -> AppliedPartialFillCanceledEvent:
    """Apply the narrow open, one-partial-fill, then cancel lifecycle."""

    if event.order_id != order.order_id:
        raise ValueValidationError("execution event belongs to another order")
    if (
        reservation.order_id != order.order_id
        or reservation.risk_decision_id != order.risk_decision_id
        or reservation.exact_order_fingerprint != order.exact_order_fingerprint
    ):
        raise ValueValidationError("reservation is not bound to the exact order")
    if event.sequence != order.last_execution_sequence + 1:
        raise ValueValidationError("execution event is out of sequence")
    if event.occurred_at >= order.expires_at:
        raise ValueValidationError("partial-fill lifecycle event cannot occur at or after expiry")
    if maximum_loss_per_unit.amount <= 0:
        raise ValueValidationError("partial fill requires positive bounded per-unit loss")
    if not instrument_id or not rule_version_id:
        raise ValueValidationError("partial fill requires instrument and rule provenance")
    full_order_risk = maximum_loss_per_unit.amount * order.quantity.value

    if event.event_type is ExecutionEventType.OPENED:
        if (
            order.state is not OrderState.READY
            or reservation.state is not ReservationState.HELD
            or position is not None
        ):
            raise ValueValidationError("invalid partial-fill OPENED transition")
        if event.quantity.value != 0 or event.price is not None or event.fee.amount != 0:
            raise ValueValidationError("OPENED event cannot contain fill economics")
        if (
            reservation.maximum_loss.amount != full_order_risk
            or order.reserved_maximum_loss != reservation.maximum_loss
        ):
            raise ValueValidationError("opening reservation does not cover the full order risk")
        return AppliedPartialFillCanceledEvent(
            order=replace(
                order,
                state=OrderState.OPEN,
                last_execution_sequence=event.sequence,
            ),
            reservation=replace(reservation, state=ReservationState.OPEN),
            position=None,
            released_maximum_loss=Money(0),
        )

    if event.event_type is ExecutionEventType.FILL:
        if (
            order.state is not OrderState.OPEN
            or reservation.state is not ReservationState.OPEN
            or position is not None
            or order.filled_quantity.value != 0
        ):
            raise ValueValidationError("invalid partial FILL transition")
        if event.price is None or not 0 < event.quantity.value < order.remaining_quantity.value:
            raise ValueValidationError("partial fill quantity must be positive and below remaining")
        if event.quantity.increment != order.quantity.increment:
            raise ValueValidationError("partial fill quantity increment does not match the order")
        if (
            event.price.tick != order.limit_price.tick
            or event.price.value > order.limit_price.value
        ):
            raise ValueValidationError("partial fill price violates the order terms")
        expected_fee = fee_per_unit.amount * event.quantity.value
        if event.fee.amount != expected_fee:
            raise ValueValidationError("partial fill fee does not match the fee rule")
        if (
            reservation.maximum_loss.amount != full_order_risk
            or order.reserved_maximum_loss != reservation.maximum_loss
        ):
            raise ValueValidationError("partial fill did not begin with full risk coverage")
        remaining_quantity = order.quantity.value - event.quantity.value
        remaining_order_risk = maximum_loss_per_unit.amount * remaining_quantity
        filled_position_risk = maximum_loss_per_unit.amount * event.quantity.value
        position_cost = event.quantity.value * event.price.value
        new_position = FilledPositionState(
            instrument_id=instrument_id,
            rule_version_id=rule_version_id,
            open_quantity=event.quantity,
            cost_basis=Money(position_cost),
            maximum_remaining_loss=Money(filled_position_risk),
            settlement_state=SettlementState.OPEN,
            side=order.side,
            originating_order_id=order.order_id,
            originating_fill_event_id=event.event_id,
        )
        return AppliedPartialFillCanceledEvent(
            order=replace(
                order,
                filled_quantity=event.quantity,
                reserved_maximum_loss=Money(remaining_order_risk),
                state=OrderState.PARTIALLY_FILLED,
                last_execution_sequence=event.sequence,
            ),
            reservation=replace(
                reservation,
                maximum_loss=Money(remaining_order_risk),
                state=ReservationState.OPEN,
            ),
            position=new_position,
            released_maximum_loss=Money(0),
        )

    if event.event_type is ExecutionEventType.CANCELED:
        if (
            order.state is not OrderState.PARTIALLY_FILLED
            or reservation.state is not ReservationState.OPEN
            or position is None
        ):
            raise ValueValidationError("invalid partial-fill CANCELED transition")
        if event.quantity.value != 0 or event.price is not None or event.fee.amount != 0:
            raise ValueValidationError("CANCELED event cannot contain fill economics")
        expected_unfilled_risk = maximum_loss_per_unit.amount * order.remaining_quantity.value
        expected_filled_risk = maximum_loss_per_unit.amount * order.filled_quantity.value
        if (
            reservation.maximum_loss.amount != expected_unfilled_risk
            or order.reserved_maximum_loss != reservation.maximum_loss
            or position.maximum_remaining_loss.amount != expected_filled_risk
            or position.open_quantity != order.filled_quantity
            or position.originating_order_id != order.order_id
            or position.side is not order.side
            or position.instrument_id != instrument_id
            or position.rule_version_id != rule_version_id
        ):
            raise ValueValidationError("cancellation risk decomposition is inconsistent")
        released = reservation.maximum_loss
        if released.amount <= 0:
            raise ValueValidationError("partial cancellation requires unfilled risk to release")
        return AppliedPartialFillCanceledEvent(
            order=replace(
                order,
                reserved_maximum_loss=Money(0),
                state=OrderState.CANCELED,
                last_execution_sequence=event.sequence,
            ),
            reservation=replace(
                reservation,
                maximum_loss=Money(0),
                state=ReservationState.RELEASED,
            ),
            position=position,
            released_maximum_loss=released,
        )

    raise ValueValidationError("partial-fill lifecycle accepts only OPENED, FILL, or CANCELED")


def apply_unfilled_order_event(
    order: ExpiringLimitOrder,
    reservation: RiskReservation,
    event: ExecutionEvent,
) -> AppliedUnfilledOrderEvent:
    if event.order_id != order.order_id:
        raise ValueValidationError("execution event belongs to another order")
    if (
        reservation.order_id != order.order_id
        or reservation.risk_decision_id != order.risk_decision_id
        or reservation.exact_order_fingerprint != order.exact_order_fingerprint
    ):
        raise ValueValidationError("reservation is not bound to the exact order")
    if event.sequence != order.last_execution_sequence + 1:
        raise ValueValidationError("execution event is out of sequence")
    if event.quantity.value != 0 or event.price is not None or event.fee.amount != 0:
        raise ValueValidationError("unfilled lifecycle events cannot carry fill economics")
    if event.event_type is ExecutionEventType.OPENED:
        if order.state is not OrderState.READY or reservation.state is not ReservationState.HELD:
            raise ValueValidationError("invalid unfilled OPENED transition")
        if reservation.maximum_loss != order.reserved_maximum_loss:
            raise ValueValidationError("open order and reservation maximum loss must match")
        return AppliedUnfilledOrderEvent(
            order=replace(
                order,
                state=OrderState.OPEN,
                last_execution_sequence=event.sequence,
            ),
            reservation=replace(reservation, state=ReservationState.OPEN),
            released_maximum_loss=Money(0),
        )
    if event.event_type is ExecutionEventType.EXPIRED:
        if order.state is not OrderState.OPEN or reservation.state is not ReservationState.OPEN:
            raise ValueValidationError("invalid unfilled EXPIRED transition")
        if order.filled_quantity.value != 0:
            raise ValueValidationError("an order with fills cannot use unfilled expiration")
        if event.occurred_at != order.expires_at:
            raise ValueValidationError("expiration event time does not match the order")
        released = reservation.maximum_loss
        if released.amount <= 0 or released != order.reserved_maximum_loss:
            raise ValueValidationError("expiration requires the full active reservation")
        return AppliedUnfilledOrderEvent(
            order=replace(
                order,
                reserved_maximum_loss=Money(0),
                state=OrderState.EXPIRED,
                last_execution_sequence=event.sequence,
            ),
            reservation=replace(
                reservation,
                maximum_loss=Money(0),
                state=ReservationState.RELEASED,
            ),
            released_maximum_loss=released,
        )
    raise ValueValidationError("unfilled order lifecycle accepts only OPENED or EXPIRED")


def initial_position(
    instrument_id: str,
    rule_version_id: str,
    quantity_increment: Decimal,
) -> PositionState:
    return PositionState(
        instrument_id=instrument_id,
        rule_version_id=rule_version_id,
        open_quantity=Quantity(0, quantity_increment),
        cost_basis=Money(0),
        maximum_remaining_loss=Money(0),
        settlement_state=SettlementState.OPEN,
    )


def apply_execution_event(
    order: Order,
    reservation: RiskReservation,
    position: PositionState,
    event: ExecutionEvent,
) -> AppliedExecution:
    if event.order_id != order.order_id:
        raise ValueValidationError("execution event belongs to another order")
    if event.sequence != order.last_execution_sequence + 1:
        raise ValueValidationError("execution event is out of sequence")
    if event.event_type is ExecutionEventType.OPENED:
        if order.state is not OrderState.READY or event.quantity.value != 0:
            raise ValueValidationError("invalid OPENED transition")
        return AppliedExecution(
            order=replace(
                order,
                state=OrderState.OPEN,
                last_execution_sequence=event.sequence,
            ),
            reservation=replace(reservation, state=ReservationState.OPEN),
            position=position,
        )
    if event.event_type is not ExecutionEventType.FILL or order.state is not OrderState.OPEN:
        raise ValueValidationError("invalid fill transition")
    if event.price is None or event.quantity.value <= 0:
        raise ValueValidationError("fill requires a positive quantity and price")
    remaining = order.quantity.value - order.filled_quantity.value
    if event.quantity.value > remaining:
        raise ValueValidationError("fill exceeds remaining order quantity")
    if event.price.value > order.limit_price.value:
        raise ValueValidationError("fill price violates the order limit")
    filled = order.filled_quantity.value + event.quantity.value
    cost = event.quantity.value * event.price.value
    total_cost = position.cost_basis.amount + cost
    new_quantity = position.open_quantity.value + event.quantity.value
    new_order = replace(
        order,
        filled_quantity=Quantity(filled, order.quantity.increment),
        reserved_maximum_loss=Money(total_cost),
        state=OrderState.FILLED if filled == order.quantity.value else OrderState.OPEN,
        last_execution_sequence=event.sequence,
    )
    new_reservation = replace(
        reservation,
        maximum_loss=Money(total_cost),
        state=ReservationState.OPEN,
    )
    new_position = PositionState(
        instrument_id=position.instrument_id,
        rule_version_id=position.rule_version_id,
        open_quantity=Quantity(new_quantity, position.open_quantity.increment),
        cost_basis=Money(total_cost),
        maximum_remaining_loss=Money(total_cost),
        settlement_state=SettlementState.OPEN,
    )
    return AppliedExecution(new_order, new_reservation, new_position)


@dataclass(frozen=True, slots=True)
class SettlementFact:
    source_id: str
    instrument_id: str
    rule_version_id: str
    source_event_at: datetime
    available_at: datetime
    settlement_value: Decimal
    content_hash: ContentHash

    def __post_init__(self) -> None:
        if self.source_event_at.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueValidationError("settlement timestamps must be timezone-aware")
        event_at = self.source_event_at.astimezone(UTC)
        available_at = self.available_at.astimezone(UTC)
        if event_at > available_at:
            raise ValueValidationError("settlement cannot be available before its source event")
        object.__setattr__(self, "source_event_at", event_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "settlement_value", parse_decimal(self.settlement_value))


@dataclass(frozen=True, slots=True)
class RuleBoundSettlementFact(SettlementFact):
    fixture_id: str
    schema_version: str
    settlement_value_text: str
    resolution_source: str
    rule_content_hash: ContentHash
    raw_payload: str
    raw_payload_hash: ContentHash

    def __post_init__(self) -> None:
        SettlementFact.__post_init__(self)
        if self.schema_version != "2":
            raise ValueValidationError("unsupported rule-bound settlement schema version")
        if not self.fixture_id or not self.resolution_source or not self.raw_payload:
            raise ValueValidationError(
                "rule-bound settlement requires source and raw payload provenance"
            )
        if parse_decimal(self.settlement_value_text) != self.settlement_value:
            raise ValueValidationError(
                "rule-bound settlement value text does not match its authoritative value"
            )
        if sha256(self.raw_payload.encode("utf-8")).hexdigest() != self.raw_payload_hash.value:
            raise ValueValidationError("rule-bound settlement raw payload hash mismatch")
        evidence = {
            "fixture_id": self.fixture_id,
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "instrument_id": self.instrument_id,
            "rule_version_id": self.rule_version_id,
            "rule_content_hash": self.rule_content_hash.value,
            "resolution_source": self.resolution_source,
            "source_event_at": self.source_event_at,
            "available_at": self.available_at,
            "settlement_value": self.settlement_value_text,
            "raw_payload": self.raw_payload,
            "raw_payload_hash": self.raw_payload_hash.value,
        }
        if content_digest(evidence) != self.content_hash.value:
            raise ValueValidationError("rule-bound settlement evidence content hash mismatch")


@dataclass(frozen=True, slots=True)
class ClassifiedRuleBoundSettlementFact(SettlementFact):
    fixture_id: str
    schema_version: str
    settlement_value_text: str
    resolution_source: str
    rule_content_hash: ContentHash
    outcome_classification: SettlementOutcome
    raw_payload: str
    raw_payload_hash: ContentHash

    def __post_init__(self) -> None:
        SettlementFact.__post_init__(self)
        if self.schema_version != "2":
            raise ValueValidationError("unsupported classified settlement schema version")
        if not all(
            (
                self.fixture_id,
                self.source_id,
                self.instrument_id,
                self.rule_version_id,
                self.resolution_source,
                self.raw_payload,
            )
        ):
            raise ValueValidationError(
                "classified settlement requires complete source and rule provenance"
            )
        if not isinstance(self.outcome_classification, SettlementOutcome):
            raise ValueValidationError("settlement outcome classification is invalid")
        if self.settlement_value > Decimal(1):
            raise ValueValidationError("classified settlement value must be between zero and one")
        if parse_decimal(self.settlement_value_text) != self.settlement_value:
            raise ValueValidationError(
                "classified settlement value text does not match its authoritative value"
            )
        expected_raw_payload = (
            f"resolution:{self.outcome_classification.value};"
            f"settlement_value={self.settlement_value_text};"
            f"instrument={self.instrument_id};"
            f"rule={self.rule_version_id};"
            f"rule_hash={self.rule_content_hash.value};"
            f"source={self.resolution_source}"
        )
        if self.raw_payload != expected_raw_payload:
            raise ValueValidationError(
                "classified settlement raw payload does not match its authenticated fields"
            )
        if sha256(self.raw_payload.encode("utf-8")).hexdigest() != self.raw_payload_hash.value:
            raise ValueValidationError("classified settlement raw payload hash mismatch")
        evidence = {
            "fixture_id": self.fixture_id,
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "instrument_id": self.instrument_id,
            "rule_version_id": self.rule_version_id,
            "rule_content_hash": self.rule_content_hash.value,
            "resolution_source": self.resolution_source,
            "outcome_classification": self.outcome_classification,
            "source_event_at": self.source_event_at,
            "available_at": self.available_at,
            "settlement_value": self.settlement_value_text,
            "raw_payload": self.raw_payload,
            "raw_payload_hash": self.raw_payload_hash.value,
        }
        if content_digest(evidence) != self.content_hash.value:
            raise ValueValidationError("classified settlement evidence content hash mismatch")


@dataclass(frozen=True, slots=True)
class SettlementResult:
    settlement_id: str
    source_id: str
    filled_quantity: Quantity
    settlement_cash: Money
    realized_pnl: Decimal
    reservation_release: Money


@dataclass(frozen=True, slots=True)
class PartialPositionSettlementResult(SettlementResult):
    position_risk_release: Money

    def __post_init__(self) -> None:
        if self.reservation_release.amount != 0:
            raise ValueValidationError(
                "canceled-order settlement cannot release an order reservation"
            )
        if self.position_risk_release.amount <= 0:
            raise ValueValidationError("settlement must release positive filled-position risk")


@dataclass(frozen=True, slots=True)
class ClassifiedPartialPositionSettlementResult(PartialPositionSettlementResult):
    outcome_classification: SettlementOutcome

    def __post_init__(self) -> None:
        PartialPositionSettlementResult.__post_init__(self)
        if not isinstance(self.outcome_classification, SettlementOutcome):
            raise ValueValidationError("settlement result outcome classification is invalid")


@dataclass(frozen=True, slots=True)
class AppliedSettlement:
    result: SettlementResult
    reservation: RiskReservation
    position: PositionState


@dataclass(frozen=True, slots=True)
class SettledFilledPositionState(PositionState):
    side: Side
    settled_quantity: Quantity
    settlement_value: Decimal
    settlement_id: str
    settlement_source_id: str
    originating_order_id: str
    originating_fill_event_id: str

    def __post_init__(self) -> None:
        settlement_value = parse_decimal(self.settlement_value)
        if (
            self.open_quantity.value != 0
            or self.cost_basis.amount != 0
            or self.maximum_remaining_loss.amount != 0
            or self.settlement_state is not SettlementState.SETTLED
        ):
            raise ValueValidationError("settled filled position must have no open asset or risk")
        if self.settled_quantity.value <= 0:
            raise ValueValidationError(
                "settled filled position requires positive historical quantity"
            )
        if not all(
            (
                self.instrument_id,
                self.rule_version_id,
                self.settlement_id,
                self.settlement_source_id,
                self.originating_order_id,
                self.originating_fill_event_id,
            )
        ):
            raise ValueValidationError("settled filled position requires complete provenance")
        object.__setattr__(self, "settlement_value", settlement_value)


@dataclass(frozen=True, slots=True)
class ClassifiedSettledFilledPositionState(SettledFilledPositionState):
    outcome_classification: SettlementOutcome
    settlement_cash: Money
    realized_contract_pnl: Decimal

    def __post_init__(self) -> None:
        SettledFilledPositionState.__post_init__(self)
        if not isinstance(self.outcome_classification, SettlementOutcome):
            raise ValueValidationError("settled position outcome classification is invalid")
        expected_cash = self.settled_quantity.value * self.settlement_value
        if self.settlement_cash.amount != expected_cash:
            raise ValueValidationError(
                "settled position cash does not match quantity and settlement value"
            )
        object.__setattr__(
            self,
            "realized_contract_pnl",
            parse_decimal(self.realized_contract_pnl, allow_negative=True),
        )


@dataclass(frozen=True, slots=True)
class AppliedPartialPositionSettlement:
    result: PartialPositionSettlementResult
    reservation: RiskReservation
    position: SettledFilledPositionState


@dataclass(frozen=True, slots=True)
class AppliedClassifiedPartialPositionSettlement:
    result: ClassifiedPartialPositionSettlementResult
    reservation: RiskReservation
    position: ClassifiedSettledFilledPositionState


def settle_position(
    position: PositionState,
    reservation: RiskReservation,
    instrument: Instrument,
    rule: ContractRuleVersion,
    fact: SettlementFact,
) -> AppliedSettlement:
    if isinstance(rule, VoidContractRuleVersion):
        raise ValueValidationError("VOID contract rules require their classified settlement path")
    if type(fact) is not SettlementFact:
        raise ValueValidationError(
            "classified or rule-bound settlement facts require their explicit settlement path"
        )
    if position.settlement_state is not SettlementState.OPEN:
        raise ValueValidationError("position is already settled")
    if position.open_quantity.value <= 0:
        raise ValueValidationError("settlement requires a filled position")
    if (
        fact.instrument_id != instrument.instrument_id
        or position.instrument_id != instrument.instrument_id
    ):
        raise ValueValidationError("settlement instrument mismatch")
    if (
        fact.rule_version_id != rule.rule_version_id
        or position.rule_version_id != rule.rule_version_id
    ):
        raise ValueValidationError("settlement rule version mismatch")
    if fact.available_at < rule.determination_at:
        raise ValueValidationError("settlement fact was available before determination")
    allowed_values = (
        instrument.yes_settlement_value,
        instrument.no_settlement_value,
        instrument.void_settlement_value,
    )
    if fact.settlement_value not in allowed_values:
        raise ValueValidationError("settlement value is not defined by the exact rule version")
    proceeds = position.open_quantity.value * fact.settlement_value
    realized = proceeds - position.cost_basis.amount
    payload = {
        "source_id": fact.source_id,
        "instrument_id": fact.instrument_id,
        "rule_version_id": fact.rule_version_id,
        "filled_quantity": position.open_quantity,
        "settlement_value": fact.settlement_value,
        "content_hash": fact.content_hash,
    }
    result = SettlementResult(
        settlement_id=deterministic_id("settlement", payload),
        source_id=fact.source_id,
        filled_quantity=position.open_quantity,
        settlement_cash=Money(proceeds),
        realized_pnl=parse_decimal(realized, allow_negative=True),
        reservation_release=reservation.maximum_loss,
    )
    released = replace(
        reservation,
        maximum_loss=Money(0),
        state=ReservationState.RELEASED,
    )
    settled_position = PositionState(
        instrument_id=position.instrument_id,
        rule_version_id=position.rule_version_id,
        open_quantity=Quantity(0, position.open_quantity.increment),
        cost_basis=Money(0),
        maximum_remaining_loss=Money(0),
        settlement_state=SettlementState.SETTLED,
    )
    return AppliedSettlement(result, released, settled_position)


def rule_bound_position_settlement_id(
    position: FilledPositionState,
    fact: RuleBoundSettlementFact,
) -> str:
    """Derive the exact settlement identity from fact and fill provenance."""

    payload = {
        "source_id": fact.source_id,
        "instrument_id": fact.instrument_id,
        "rule_version_id": fact.rule_version_id,
        "rule_content_hash": fact.rule_content_hash,
        "resolution_source": fact.resolution_source,
        "filled_quantity": position.open_quantity,
        "settlement_value": fact.settlement_value,
        "content_hash": fact.content_hash,
        "originating_order_id": position.originating_order_id,
        "originating_fill_event_id": position.originating_fill_event_id,
    }
    return deterministic_id("settlement", payload)


def classified_rule_bound_position_settlement_id(
    position: FilledPositionState,
    fact: ClassifiedRuleBoundSettlementFact,
) -> str:
    """Bind classified settlement identity to exact evidence and fill provenance."""

    payload = {
        "source_id": fact.source_id,
        "instrument_id": fact.instrument_id,
        "rule_version_id": fact.rule_version_id,
        "rule_content_hash": fact.rule_content_hash,
        "resolution_source": fact.resolution_source,
        "outcome_classification": fact.outcome_classification,
        "filled_quantity": position.open_quantity,
        "settlement_value": fact.settlement_value,
        "content_hash": fact.content_hash,
        "originating_order_id": position.originating_order_id,
        "originating_fill_event_id": position.originating_fill_event_id,
    }
    return deterministic_id("settlement", payload)


def settle_canceled_filled_position(
    position: FilledPositionState,
    reservation: RiskReservation,
    order: ExpiringLimitOrder,
    instrument: Instrument,
    rule: ContractRuleVersion,
    fact: RuleBoundSettlementFact,
    *,
    execution_completed_at: datetime,
) -> AppliedPartialPositionSettlement:
    """Settle the retained YES position without changing its canceled order."""

    if execution_completed_at.tzinfo is None or execution_completed_at.utcoffset() is None:
        raise ValueValidationError("execution completion time must be timezone-aware")
    execution_completed_at = execution_completed_at.astimezone(UTC)
    if (
        order.state is not OrderState.CANCELED
        or order.filled_quantity.value <= 0
        or order.remaining_quantity.value <= 0
        or order.reserved_maximum_loss.amount != 0
    ):
        raise ValueValidationError("partial-position settlement requires a canceled partial order")
    if (
        reservation.state is not ReservationState.RELEASED
        or reservation.maximum_loss.amount != 0
        or reservation.order_id != order.order_id
        or reservation.risk_decision_id != order.risk_decision_id
        or reservation.exact_order_fingerprint != order.exact_order_fingerprint
    ):
        raise ValueValidationError("canceled order reservation is not fully and exactly released")
    if (
        position.settlement_state is not SettlementState.OPEN
        or position.open_quantity != order.filled_quantity
        or position.originating_order_id != order.order_id
        or position.side is not order.side
        or position.side is not Side.YES
    ):
        raise ValueValidationError("settlement position does not match the canceled order fill")
    if (
        fact.instrument_id != instrument.instrument_id
        or position.instrument_id != instrument.instrument_id
    ):
        raise ValueValidationError("settlement instrument mismatch")
    if (
        fact.rule_version_id != rule.rule_version_id
        or position.rule_version_id != rule.rule_version_id
    ):
        raise ValueValidationError("settlement rule version mismatch")
    if fact.rule_content_hash != rule.content_hash:
        raise ValueValidationError("settlement rule content hash mismatch")
    if fact.resolution_source != rule.resolution_source:
        raise ValueValidationError("settlement resolution source mismatch")
    if (
        fact.source_event_at <= execution_completed_at
        or fact.available_at <= execution_completed_at
    ):
        raise ValueValidationError("settlement was available before execution completed")
    if fact.source_event_at < rule.determination_at or fact.available_at < rule.determination_at:
        raise ValueValidationError("settlement fact precedes contract determination")
    if instrument.no_settlement_value != Decimal(0):
        raise ValueValidationError("the settled-NO slice requires a zero NO payout")
    if fact.settlement_value != instrument.no_settlement_value:
        raise ValueValidationError("settlement value is not the exact NO rule value")

    position_risk_release = position.maximum_remaining_loss
    realized = parse_decimal(-position.cost_basis.amount, allow_negative=True)
    settlement_id = rule_bound_position_settlement_id(position, fact)
    result = PartialPositionSettlementResult(
        settlement_id=settlement_id,
        source_id=fact.source_id,
        filled_quantity=position.open_quantity,
        settlement_cash=Money(0),
        realized_pnl=realized,
        reservation_release=Money(0),
        position_risk_release=position_risk_release,
    )
    settled_position = SettledFilledPositionState(
        instrument_id=position.instrument_id,
        rule_version_id=position.rule_version_id,
        open_quantity=Quantity(0, position.open_quantity.increment),
        cost_basis=Money(0),
        maximum_remaining_loss=Money(0),
        settlement_state=SettlementState.SETTLED,
        side=position.side,
        settled_quantity=position.open_quantity,
        settlement_value=fact.settlement_value,
        settlement_id=settlement_id,
        settlement_source_id=fact.source_id,
        originating_order_id=position.originating_order_id,
        originating_fill_event_id=position.originating_fill_event_id,
    )
    return AppliedPartialPositionSettlement(
        result=result,
        reservation=reservation,
        position=settled_position,
    )


def settle_canceled_filled_void_position(
    position: FilledPositionState,
    reservation: RiskReservation,
    order: ExpiringLimitOrder,
    instrument: Instrument,
    rule: VoidContractRuleVersion,
    fact: ClassifiedRuleBoundSettlementFact,
    *,
    execution_completed_at: datetime,
) -> AppliedClassifiedPartialPositionSettlement:
    """Apply an exact contract-specific VOID fact to only the retained fill."""

    if execution_completed_at.tzinfo is None or execution_completed_at.utcoffset() is None:
        raise ValueValidationError("execution completion time must be timezone-aware")
    execution_completed_at = execution_completed_at.astimezone(UTC)
    if (
        order.state is not OrderState.CANCELED
        or order.filled_quantity.value <= 0
        or order.remaining_quantity.value <= 0
        or order.reserved_maximum_loss.amount != 0
    ):
        raise ValueValidationError("VOID settlement requires a canceled partial order")
    if (
        reservation.state is not ReservationState.RELEASED
        or reservation.maximum_loss.amount != 0
        or reservation.order_id != order.order_id
        or reservation.risk_decision_id != order.risk_decision_id
        or reservation.exact_order_fingerprint != order.exact_order_fingerprint
    ):
        raise ValueValidationError("canceled order reservation is not fully and exactly released")
    if (
        position.settlement_state is not SettlementState.OPEN
        or position.open_quantity.value <= 0
        or position.open_quantity != order.filled_quantity
        or position.originating_order_id != order.order_id
        or position.side is not order.side
        or position.side is not Side.YES
    ):
        raise ValueValidationError("VOID settlement position does not match the canceled fill")
    if (
        fact.instrument_id != instrument.instrument_id
        or position.instrument_id != instrument.instrument_id
    ):
        raise ValueValidationError("settlement instrument mismatch")
    if (
        fact.rule_version_id != rule.rule_version_id
        or position.rule_version_id != rule.rule_version_id
    ):
        raise ValueValidationError("settlement rule version mismatch")
    if fact.rule_content_hash != rule.content_hash:
        raise ValueValidationError("settlement rule content hash mismatch")
    if fact.resolution_source != rule.resolution_source:
        raise ValueValidationError("settlement resolution source mismatch")
    if (
        fact.source_event_at <= execution_completed_at
        or fact.available_at <= execution_completed_at
    ):
        raise ValueValidationError("settlement was available before execution completed")
    if fact.source_event_at < rule.determination_at or fact.available_at < rule.determination_at:
        raise ValueValidationError("settlement fact precedes contract determination")
    if fact.outcome_classification is not SettlementOutcome.VOID:
        raise ValueValidationError("settlement outcome classification must be VOID")
    if not Decimal(0) < fact.settlement_value <= Decimal(1):
        raise ValueValidationError("VOID settlement value must be bounded and nonzero")
    rule_term = rule.void_settlement_term
    if (
        fact.outcome_classification is not rule_term.outcome_classification
        or fact.settlement_value != rule_term.settlement_value
        or fact.settlement_value_text != rule_term.settlement_value_text
    ):
        raise ValueValidationError("settlement value is not the exact VOID rule value")
    if instrument.void_settlement_value != rule_term.settlement_value:
        raise ValueValidationError("instrument VOID value does not match the exact rule term")

    position_risk_release = position.maximum_remaining_loss
    proceeds = position.open_quantity.value * fact.settlement_value
    realized = parse_decimal(proceeds - position.cost_basis.amount, allow_negative=True)
    settlement_id = classified_rule_bound_position_settlement_id(position, fact)
    result = ClassifiedPartialPositionSettlementResult(
        settlement_id=settlement_id,
        source_id=fact.source_id,
        filled_quantity=position.open_quantity,
        settlement_cash=Money(proceeds),
        realized_pnl=realized,
        reservation_release=Money(0),
        position_risk_release=position_risk_release,
        outcome_classification=fact.outcome_classification,
    )
    settled_position = ClassifiedSettledFilledPositionState(
        instrument_id=position.instrument_id,
        rule_version_id=position.rule_version_id,
        open_quantity=Quantity(0, position.open_quantity.increment),
        cost_basis=Money(0),
        maximum_remaining_loss=Money(0),
        settlement_state=SettlementState.SETTLED,
        side=position.side,
        settled_quantity=position.open_quantity,
        settlement_value=fact.settlement_value,
        settlement_id=settlement_id,
        settlement_source_id=fact.source_id,
        originating_order_id=position.originating_order_id,
        originating_fill_event_id=position.originating_fill_event_id,
        outcome_classification=fact.outcome_classification,
        settlement_cash=Money(proceeds),
        realized_contract_pnl=realized,
    )
    return AppliedClassifiedPartialPositionSettlement(
        result=result,
        reservation=reservation,
        position=settled_position,
    )
