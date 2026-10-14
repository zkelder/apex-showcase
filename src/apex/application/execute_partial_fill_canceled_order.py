"""Durable synthetic OPENED -> strict partial FILL -> CANCELED execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from apex.application.authorize_order import (
    CampaignRepository,
    IdempotencyConflictError,
    MissingPersistenceStateError,
    PersistenceConflictError,
)
from apex.application.execute_unfilled_order import DurableRiskReservationState
from apex.application.interfaces import PartialFillCanceledExecutionScript
from apex.domain.decisions import RestingLimitProposal
from apex.domain.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExpiringLimitOrder,
    FilledPositionState,
    OrderState,
    ReservationState,
    apply_partial_fill_canceled_event,
    validate_execution_event_identity,
)
from apex.domain.ledger import Journal, JournalFactory
from apex.domain.values import (
    Money,
    Price,
    Quantity,
    ValueValidationError,
    floor_to_increment,
    parse_decimal,
)


class InjectedPartialFillExecutionFault(RuntimeError):
    """A test-only direct fault interrupted one partial-fill transaction."""


class PartialFillExecutionFaultPoint(StrEnum):
    AFTER_EVENT_INSERT = "AFTER_EVENT_INSERT"
    AFTER_ORDER_UPDATE = "AFTER_ORDER_UPDATE"
    AFTER_RESERVATION_UPDATE = "AFTER_RESERVATION_UPDATE"
    AFTER_POSITION_INSERT = "AFTER_POSITION_INSERT"
    AFTER_PURCHASE_JOURNAL = "AFTER_PURCHASE_JOURNAL"
    AFTER_FEE_JOURNAL = "AFTER_FEE_JOURNAL"
    BEFORE_COMMIT = "BEFORE_COMMIT"


@dataclass(frozen=True, slots=True)
class ApplyPartialFillExecutionEventCommand:
    campaign_id: str
    order_id: str
    script: PartialFillCanceledExecutionScript
    event: ExecutionEvent
    recorded_at: datetime
    fault_at: PartialFillExecutionFaultPoint | None = None


@dataclass(frozen=True, slots=True)
class DurablePartialFillCanceledState:
    campaign_id: str
    order: ExpiringLimitOrder
    reservation: DurableRiskReservationState
    proposal: RestingLimitProposal
    authorization_at: datetime
    execution_events: tuple[ExecutionEvent, ...]
    position: FilledPositionState | None
    order_row_version: int

    def __post_init__(self) -> None:
        if self.authorization_at.tzinfo is None or self.authorization_at.utcoffset() is None:
            raise ValueValidationError("durable authorization time must be timezone-aware")
        object.__setattr__(self, "authorization_at", self.authorization_at.astimezone(UTC))
        if (
            self.reservation.campaign_id != self.campaign_id
            or self.reservation.order_id != self.order.order_id
            or self.reservation.risk_decision_id != self.order.risk_decision_id
            or self.reservation.exact_order_fingerprint != self.order.exact_order_fingerprint
        ):
            raise ValueValidationError("durable reservation is not bound to the exact order")
        if (
            self.proposal.proposal_id != self.order.proposal_id
            or self.proposal.side is not self.order.side
            or self.proposal.limit_price != self.order.limit_price
            or self.proposal.executable_ask != self.order.executable_ask
            or self.proposal.order_expires_at != self.order.expires_at
        ):
            raise ValueValidationError("durable proposal is not bound to the exact order")

        expected_events: tuple[ExecutionEventType, ...]
        expected_reservation_state: ReservationState
        expected_sequence: int
        expected_version: int
        expected_position = False
        if self.order.state is OrderState.READY:
            expected_events = ()
            expected_reservation_state = ReservationState.HELD
            expected_sequence = 0
            expected_version = 1
        elif self.order.state is OrderState.OPEN:
            expected_events = (ExecutionEventType.OPENED,)
            expected_reservation_state = ReservationState.OPEN
            expected_sequence = 1
            expected_version = 2
        elif self.order.state is OrderState.PARTIALLY_FILLED:
            expected_events = (ExecutionEventType.OPENED, ExecutionEventType.FILL)
            expected_reservation_state = ReservationState.OPEN
            expected_sequence = 2
            expected_version = 3
            expected_position = True
        elif self.order.state is OrderState.CANCELED:
            expected_events = (
                ExecutionEventType.OPENED,
                ExecutionEventType.FILL,
                ExecutionEventType.CANCELED,
            )
            expected_reservation_state = ReservationState.RELEASED
            expected_sequence = 3
            expected_version = 4
            expected_position = True
        else:
            raise ValueValidationError("durable partial-fill order state is unsupported")
        if (
            tuple(event.event_type for event in self.execution_events) != expected_events
            or tuple(event.sequence for event in self.execution_events)
            != tuple(range(1, len(self.execution_events) + 1))
            or any(event.order_id != self.order.order_id for event in self.execution_events)
            or self.order.last_execution_sequence != expected_sequence
            or self.order_row_version != expected_version
            or self.reservation.state is not expected_reservation_state
            or (self.position is not None) is not expected_position
        ):
            raise ValueValidationError("durable partial-fill lifecycle is inconsistent")

        full_risk = self.proposal.maximum_loss_per_unit.amount * self.order.quantity.value
        if self.reservation.maximum_loss.amount != full_risk:
            raise ValueValidationError("durable reservation lost its authorized maximum")
        if self.order.state in (OrderState.READY, OrderState.OPEN):
            if (
                self.order.filled_quantity.value != 0
                or self.order.reserved_maximum_loss.amount != full_risk
                or self.reservation.current_amount.amount != full_risk
            ):
                raise ValueValidationError("durable pre-fill risk is inconsistent")
        else:
            if self.position is None or not (
                0 < self.order.filled_quantity.value < self.order.quantity.value
            ):
                raise ValueValidationError("durable fill must remain strictly partial")
            remaining_risk = (
                self.proposal.maximum_loss_per_unit.amount * self.order.remaining_quantity.value
            )
            filled_risk = (
                self.proposal.maximum_loss_per_unit.amount * self.order.filled_quantity.value
            )
            fill_event = self.execution_events[1]
            if (
                fill_event.quantity != self.order.filled_quantity
                or fill_event.price is None
                or self.position.instrument_id != self.proposal.instrument_id
                or self.position.rule_version_id != self.proposal.rule_version_id
                or self.position.side is not self.order.side
                or self.position.open_quantity != self.order.filled_quantity
                or self.position.cost_basis.amount
                != fill_event.quantity.value * fill_event.price.value
                or self.position.maximum_remaining_loss.amount != filled_risk
                or self.position.originating_order_id != self.order.order_id
                or self.position.originating_fill_event_id != fill_event.event_id
            ):
                raise ValueValidationError("durable open position is inconsistent")
            if self.order.state is OrderState.PARTIALLY_FILLED:
                if (
                    self.order.reserved_maximum_loss.amount != remaining_risk
                    or self.reservation.current_amount.amount != remaining_risk
                    or self.reservation.released_at is not None
                ):
                    raise ValueValidationError("durable partial-fill reservation is inconsistent")
            elif (
                self.order.reserved_maximum_loss.amount != 0
                or self.reservation.current_amount.amount != 0
                or self.reservation.released_at != self.execution_events[2].occurred_at
            ):
                raise ValueValidationError("durable cancellation release is inconsistent")
        if self.execution_events:
            opened = self.execution_events[0]
            if not self.authorization_at <= opened.occurred_at < self.order.expires_at:
                raise ValueValidationError("durable OPENED timestamp is inconsistent")


@dataclass(frozen=True, slots=True)
class DurablePartialFillExecutionResult:
    state: DurablePartialFillCanceledState
    released_maximum_loss: Money
    idempotent_replay: bool


class PartialFillExecutionRepository(Protocol):
    def campaign_id_for_order(self, order_id: str) -> str | None: ...

    def lock_partial_fill_state(self, order_id: str) -> DurablePartialFillCanceledState | None: ...


class PartialFillExecutionUnitOfWork(Protocol):
    campaigns: CampaignRepository
    partial_fill_executions: PartialFillExecutionRepository

    def __enter__(self) -> PartialFillExecutionUnitOfWork: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def stage_partial_fill_execution_transition(
        self,
        *,
        before: DurablePartialFillCanceledState,
        after_order: ExpiringLimitOrder,
        after_current_amount: Money,
        after_reservation_state: ReservationState,
        released_at: datetime | None,
        after_position: FilledPositionState | None,
        event: ExecutionEvent,
        purchase_journal: Journal | None,
        fee_journal: Journal | None,
        recorded_at: datetime,
        fault: Callable[[str], None] | None = None,
    ) -> DurablePartialFillCanceledState: ...

    def commit(self) -> None: ...


type PartialFillExecutionUnitOfWorkFactory = Callable[[], PartialFillExecutionUnitOfWork]


class DurablePartialFillCanceledExecutionService:
    """Own one row-locked transaction for the exact partial-fill lifecycle."""

    def __init__(self, unit_of_work_factory: PartialFillExecutionUnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    @staticmethod
    def _raise_at(command: ApplyPartialFillExecutionEventCommand, point: str) -> None:
        if command.fault_at is not None and command.fault_at.value == point:
            raise InjectedPartialFillExecutionFault(
                f"partial-fill execution interrupted at {point}"
            )

    @staticmethod
    def _recorded_at(command: ApplyPartialFillExecutionEventCommand) -> datetime:
        if not command.campaign_id or not command.order_id:
            raise ValueError("campaign and order identities must not be empty")
        if command.recorded_at.tzinfo is None or command.recorded_at.utcoffset() is None:
            raise ValueError("execution recording time must be timezone-aware")
        recorded_at = command.recorded_at.astimezone(UTC)
        if recorded_at < command.event.occurred_at:
            raise ValueError("execution cannot be recorded before it occurred")
        return recorded_at

    @staticmethod
    def _expected_fill(
        state: DurablePartialFillCanceledState,
        script: PartialFillCanceledExecutionScript,
    ) -> tuple[Quantity, Price, Money]:
        baseline_ordered = parse_decimal(script.ordered_quantity)
        baseline_fill = parse_decimal(script.fill_quantity)
        baseline_remaining = parse_decimal(script.remaining_quantity)
        if (
            baseline_ordered <= 0
            or not 0 < baseline_fill < baseline_ordered
            or baseline_fill + baseline_remaining != baseline_ordered
        ):
            raise ValueValidationError("partial-fill script ratio is invalid")
        fill_value = max(
            state.order.quantity.increment,
            floor_to_increment(
                state.order.quantity.value * baseline_fill / baseline_ordered,
                state.order.quantity.increment,
            ),
        )
        if not 0 < fill_value < state.order.quantity.value:
            raise ValueValidationError("scaled durable fill is not strictly partial")
        quantity = Quantity(fill_value, state.order.quantity.increment)
        price = Price(script.fill_price, state.order.limit_price.tick)
        fee_per_unit = parse_decimal(script.fee_per_contract, allow_zero=False)
        return quantity, price, Money(fee_per_unit * quantity.value)

    @classmethod
    def _validate_script_and_event(
        cls,
        state: DurablePartialFillCanceledState,
        script: PartialFillCanceledExecutionScript,
        event: ExecutionEvent,
    ) -> None:
        order = state.order
        if script.kind != "PARTIAL_FILL_THEN_CANCEL" or script.side != "YES":
            raise ValueValidationError("durable execution requires the partial-fill YES script")
        if (
            script.opened_sequence != 1
            or script.fill_sequence != 2
            or script.canceled_sequence != 3
        ):
            raise ValueValidationError("partial-fill execution sequence must be exactly 1, 2, 3")
        if not (
            state.authorization_at
            <= script.opened_at
            < script.filled_at
            < script.canceled_at
            < order.expires_at
        ):
            raise ValueValidationError("partial-fill execution timestamps are inconsistent")
        if event.order_id != order.order_id:
            raise ValueValidationError("execution event belongs to another order")
        zero = Quantity(0, order.quantity.increment)
        expected_quantity = zero
        expected_price: Price | None = None
        expected_fee = Money(0)
        if event.event_type is ExecutionEventType.OPENED:
            expected_sequence = script.opened_sequence
            expected_at = script.opened_at
        elif event.event_type is ExecutionEventType.FILL:
            expected_sequence = script.fill_sequence
            expected_at = script.filled_at
            expected_quantity, expected_price, expected_fee = cls._expected_fill(state, script)
            if expected_price.value > order.limit_price.value:
                raise ValueValidationError("partial fill price violates the order limit")
            if expected_fee.amount != state.proposal.fee_per_unit.amount * expected_quantity.value:
                raise ValueValidationError("partial fill fee does not match the proposal")
        elif event.event_type is ExecutionEventType.CANCELED:
            expected_sequence = script.canceled_sequence
            expected_at = script.canceled_at
        else:
            raise ValueValidationError(
                "durable partial-fill execution accepts only OPENED, FILL, or CANCELED"
            )
        if (
            event.sequence != expected_sequence
            or event.occurred_at != expected_at
            or event.quantity != expected_quantity
            or event.price != expected_price
            or event.fee != expected_fee
        ):
            raise ValueValidationError("partial-fill event does not match the bound script")
        validate_execution_event_identity(event, script.script_id)

    @staticmethod
    def _released_for(
        state: DurablePartialFillCanceledState,
        event: ExecutionEvent,
    ) -> Money:
        if event.event_type is not ExecutionEventType.CANCELED:
            return Money(0)
        return Money(
            state.proposal.maximum_loss_per_unit.amount * state.order.remaining_quantity.value
        )

    @classmethod
    def _existing_result(
        cls,
        state: DurablePartialFillCanceledState,
        event: ExecutionEvent,
    ) -> DurablePartialFillExecutionResult | None:
        existing_by_id = next(
            (item for item in state.execution_events if item.event_id == event.event_id),
            None,
        )
        if existing_by_id is not None:
            if existing_by_id != event:
                raise IdempotencyConflictError("execution event identity has conflicting content")
            return DurablePartialFillExecutionResult(state, cls._released_for(state, event), True)
        existing_by_sequence = next(
            (item for item in state.execution_events if item.sequence == event.sequence),
            None,
        )
        if existing_by_sequence is not None:
            if existing_by_sequence != event:
                raise IdempotencyConflictError("execution sequence has conflicting content")
            return DurablePartialFillExecutionResult(state, Money(0), True)
        return None

    def recover(self, campaign_id: str, order_id: str) -> DurablePartialFillCanceledState:
        if not campaign_id or not order_id:
            raise ValueError("campaign and order identities must not be empty")
        with self._unit_of_work_factory() as unit_of_work:
            associated = unit_of_work.partial_fill_executions.campaign_id_for_order(order_id)
            if associated is None:
                raise MissingPersistenceStateError("durable order was not found")
            if associated != campaign_id:
                raise PersistenceConflictError("order belongs to another campaign")
            if unit_of_work.campaigns.lock(campaign_id) is None:
                raise MissingPersistenceStateError("campaign is not initialized")
            state = unit_of_work.partial_fill_executions.lock_partial_fill_state(order_id)
            if state is None:
                raise MissingPersistenceStateError("durable partial-fill lifecycle is incomplete")
            unit_of_work.commit()
            return state

    def apply_execution_event(
        self, command: ApplyPartialFillExecutionEventCommand
    ) -> DurablePartialFillExecutionResult:
        recorded_at = self._recorded_at(command)
        with self._unit_of_work_factory() as unit_of_work:
            associated = unit_of_work.partial_fill_executions.campaign_id_for_order(
                command.order_id
            )
            if associated is None:
                raise MissingPersistenceStateError("durable order was not found")
            if associated != command.campaign_id:
                raise PersistenceConflictError("order belongs to another campaign")
            if unit_of_work.campaigns.lock(command.campaign_id) is None:
                raise MissingPersistenceStateError("campaign is not initialized")
            state = unit_of_work.partial_fill_executions.lock_partial_fill_state(command.order_id)
            if state is None:
                raise MissingPersistenceStateError("durable partial-fill lifecycle is incomplete")
            existing = self._existing_result(state, command.event)
            if existing is not None:
                unit_of_work.commit()
                return existing
            self._validate_script_and_event(state, command.script, command.event)
            applied = apply_partial_fill_canceled_event(
                state.order,
                state.reservation.transition_value(),
                state.position,
                command.event,
                instrument_id=state.proposal.instrument_id,
                rule_version_id=state.proposal.rule_version_id,
                maximum_loss_per_unit=state.proposal.maximum_loss_per_unit,
                fee_per_unit=state.proposal.fee_per_unit,
            )
            purchase_journal: Journal | None = None
            fee_journal: Journal | None = None
            if command.event.event_type is ExecutionEventType.FILL:
                if command.event.price is None:
                    raise ValueValidationError("partial fill is missing its exact price")
                purchase_journal = JournalFactory.purchase(
                    command.event.event_id,
                    Money(command.event.quantity.value * command.event.price.value),
                    command.event.occurred_at,
                )
                fee_journal = JournalFactory.fee(
                    command.event.event_id,
                    command.event.fee,
                    command.event.occurred_at,
                )
            released_at = (
                command.event.occurred_at
                if command.event.event_type is ExecutionEventType.CANCELED
                else None
            )
            after = unit_of_work.stage_partial_fill_execution_transition(
                before=state,
                after_order=applied.order,
                after_current_amount=applied.reservation.maximum_loss,
                after_reservation_state=applied.reservation.state,
                released_at=released_at,
                after_position=applied.position,
                event=command.event,
                purchase_journal=purchase_journal,
                fee_journal=fee_journal,
                recorded_at=recorded_at,
                fault=lambda point: self._raise_at(command, point),
            )
            self._raise_at(command, PartialFillExecutionFaultPoint.BEFORE_COMMIT.value)
            unit_of_work.commit()
            return DurablePartialFillExecutionResult(
                state=after,
                released_maximum_loss=applied.released_maximum_loss,
                idempotent_replay=False,
            )
