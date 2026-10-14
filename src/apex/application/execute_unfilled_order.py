"""Durable synthetic OPENED -> EXPIRED execution application service."""

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
from apex.application.interfaces import UnfilledExpiryExecutionScript
from apex.domain.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExpiringLimitOrder,
    OrderState,
    ReservationState,
    RiskReservation,
    apply_unfilled_order_event,
    validate_execution_event_identity,
)
from apex.domain.values import Money, ValueValidationError


class InjectedExecutionFault(RuntimeError):
    """A test-only direct fault interrupted one execution transaction."""


class ExecutionFaultPoint(StrEnum):
    AFTER_EVENT_INSERT = "AFTER_EVENT_INSERT"
    AFTER_ORDER_UPDATE = "AFTER_ORDER_UPDATE"
    AFTER_RESERVATION_UPDATE = "AFTER_RESERVATION_UPDATE"
    BEFORE_COMMIT = "BEFORE_COMMIT"


@dataclass(frozen=True, slots=True)
class ApplyExecutionEventCommand:
    campaign_id: str
    order_id: str
    script: UnfilledExpiryExecutionScript
    event: ExecutionEvent
    recorded_at: datetime
    fault_at: ExecutionFaultPoint | None = None


@dataclass(frozen=True, slots=True)
class DurableRiskReservationState:
    reservation_id: str
    campaign_id: str
    risk_decision_id: str
    order_id: str
    exact_order_fingerprint: str
    maximum_loss: Money
    current_amount: Money
    state: ReservationState
    released_at: datetime | None

    def __post_init__(self) -> None:
        if self.maximum_loss.amount <= 0:
            raise ValueValidationError("durable reservation maximum loss must remain positive")
        if not 0 <= self.current_amount.amount <= self.maximum_loss.amount:
            raise ValueValidationError("durable reservation current amount is out of bounds")
        if self.state is ReservationState.HELD:
            if self.current_amount != self.maximum_loss or self.released_at is not None:
                raise ValueValidationError("held durable reservation is inconsistent")
        elif self.state is ReservationState.OPEN:
            if self.current_amount.amount <= 0 or self.released_at is not None:
                raise ValueValidationError("open durable reservation is inconsistent")
        elif self.state is ReservationState.RELEASED:
            if self.current_amount.amount != 0 or self.released_at is None:
                raise ValueValidationError("released durable reservation is inconsistent")
        else:
            raise ValueValidationError("durable reservation state is unsupported")
        if self.released_at is not None:
            if self.released_at.tzinfo is None or self.released_at.utcoffset() is None:
                raise ValueValidationError("reservation release time must be timezone-aware")
            object.__setattr__(self, "released_at", self.released_at.astimezone(UTC))

    def transition_value(self) -> RiskReservation:
        return RiskReservation(
            reservation_id=self.reservation_id,
            campaign_id=self.campaign_id,
            risk_decision_id=self.risk_decision_id,
            order_id=self.order_id,
            exact_order_fingerprint=self.exact_order_fingerprint,
            maximum_loss=self.current_amount,
            state=self.state,
        )


@dataclass(frozen=True, slots=True)
class DurableUnfilledOrderState:
    campaign_id: str
    order: ExpiringLimitOrder
    reservation: DurableRiskReservationState
    authorization_at: datetime
    execution_events: tuple[ExecutionEvent, ...]
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
        if self.order.filled_quantity.value != 0:
            raise ValueValidationError("durable unfilled order cannot contain a fill")
        expected_events: tuple[ExecutionEventType, ...]
        expected_reservation_state: ReservationState
        expected_current: Money
        if self.order.state is OrderState.READY:
            expected_events = ()
            expected_reservation_state = ReservationState.HELD
            expected_current = self.reservation.maximum_loss
            expected_sequence = 0
            expected_version = 1
        elif self.order.state is OrderState.OPEN:
            expected_events = (ExecutionEventType.OPENED,)
            expected_reservation_state = ReservationState.OPEN
            expected_current = self.reservation.maximum_loss
            expected_sequence = 1
            expected_version = 2
        elif self.order.state is OrderState.EXPIRED:
            expected_events = (ExecutionEventType.OPENED, ExecutionEventType.EXPIRED)
            expected_reservation_state = ReservationState.RELEASED
            expected_current = Money(0)
            expected_sequence = 2
            expected_version = 3
        else:
            raise ValueValidationError("durable unfilled order state is unsupported")
        if (
            tuple(event.event_type for event in self.execution_events) != expected_events
            or tuple(event.sequence for event in self.execution_events)
            != tuple(range(1, len(self.execution_events) + 1))
            or any(event.order_id != self.order.order_id for event in self.execution_events)
            or self.order.last_execution_sequence != expected_sequence
            or self.order_row_version != expected_version
            or self.reservation.state is not expected_reservation_state
            or self.reservation.current_amount != expected_current
            or self.order.reserved_maximum_loss != expected_current
        ):
            raise ValueValidationError("durable unfilled lifecycle is inconsistent")
        if self.execution_events:
            opened = self.execution_events[0]
            if not self.authorization_at <= opened.occurred_at < self.order.expires_at:
                raise ValueValidationError(
                    "durable OPENED timestamp is inconsistent with authorization"
                )
        if self.order.state is OrderState.EXPIRED:
            expired = self.execution_events[1]
            if (
                expired.occurred_at != self.order.expires_at
                or self.reservation.released_at != expired.occurred_at
            ):
                raise ValueValidationError(
                    "durable expiration and reservation release timestamps differ"
                )


@dataclass(frozen=True, slots=True)
class DurableExecutionResult:
    state: DurableUnfilledOrderState
    released_maximum_loss: Money
    idempotent_replay: bool


class UnfilledExecutionRepository(Protocol):
    def campaign_id_for_order(self, order_id: str) -> str | None: ...

    def lock_order_then_reservation(self, order_id: str) -> DurableUnfilledOrderState | None: ...


class ExecutionUnitOfWork(Protocol):
    campaigns: CampaignRepository
    unfilled_executions: UnfilledExecutionRepository

    def __enter__(self) -> ExecutionUnitOfWork: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def stage_unfilled_execution_transition(
        self,
        *,
        before: DurableUnfilledOrderState,
        after_order: ExpiringLimitOrder,
        after_current_amount: Money,
        after_reservation_state: ReservationState,
        released_at: datetime | None,
        event: ExecutionEvent,
        recorded_at: datetime,
        fault: Callable[[str], None] | None = None,
    ) -> DurableUnfilledOrderState: ...

    def commit(self) -> None: ...


type ExecutionUnitOfWorkFactory = Callable[[], ExecutionUnitOfWork]


class DurableUnfilledExecutionService:
    """Own one row-locked transaction for the exact unfilled lifecycle."""

    def __init__(self, unit_of_work_factory: ExecutionUnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    @staticmethod
    def _raise_at(command: ApplyExecutionEventCommand, point: str) -> None:
        if command.fault_at is not None and command.fault_at.value == point:
            raise InjectedExecutionFault(f"execution interrupted at {point}")

    @staticmethod
    def _recorded_at(command: ApplyExecutionEventCommand) -> datetime:
        if not command.campaign_id or not command.order_id:
            raise ValueError("campaign and order identities must not be empty")
        if command.recorded_at.tzinfo is None or command.recorded_at.utcoffset() is None:
            raise ValueError("execution recording time must be timezone-aware")
        recorded_at = command.recorded_at.astimezone(UTC)
        if recorded_at < command.event.occurred_at:
            raise ValueError("execution cannot be recorded before it occurred")
        return recorded_at

    @staticmethod
    def _validate_script_and_event(
        state: DurableUnfilledOrderState,
        script: UnfilledExpiryExecutionScript,
        event: ExecutionEvent,
    ) -> None:
        order = state.order
        if script.kind != "NONMARKETABLE_EXPIRE_UNFILLED" or script.side != "YES":
            raise ValueValidationError("durable execution requires the unfilled YES script")
        if script.opened_sequence != 1 or script.expired_sequence != 2:
            raise ValueValidationError("unfilled execution sequence must be exactly 1, 2")
        if not state.authorization_at <= script.opened_at < script.expires_at:
            raise ValueValidationError("unfilled execution timestamps are inconsistent")
        if script.expires_at != order.expires_at:
            raise ValueValidationError("script expiration does not match the authorized order")
        if event.order_id != order.order_id:
            raise ValueValidationError("execution event belongs to another order")
        if event.event_type is ExecutionEventType.OPENED:
            if event.sequence != script.opened_sequence or event.occurred_at != script.opened_at:
                raise ValueValidationError("OPENED event does not match the bound script")
        elif event.event_type is ExecutionEventType.EXPIRED:
            if event.sequence != script.expired_sequence or event.occurred_at != script.expires_at:
                raise ValueValidationError("EXPIRED event does not match the bound script")
        else:
            raise ValueValidationError("durable execution accepts only OPENED or EXPIRED")
        validate_execution_event_identity(event, script.script_id)

    @staticmethod
    def _existing_result(
        state: DurableUnfilledOrderState,
        event: ExecutionEvent,
    ) -> DurableExecutionResult | None:
        existing_by_id = next(
            (item for item in state.execution_events if item.event_id == event.event_id),
            None,
        )
        if existing_by_id is not None:
            if existing_by_id != event:
                raise IdempotencyConflictError("execution event identity has conflicting content")
            released = (
                state.reservation.maximum_loss
                if event.event_type is ExecutionEventType.EXPIRED
                else Money(0)
            )
            return DurableExecutionResult(state, released, True)
        existing_by_sequence = next(
            (item for item in state.execution_events if item.sequence == event.sequence),
            None,
        )
        if existing_by_sequence is not None:
            if existing_by_sequence != event:
                raise IdempotencyConflictError("execution sequence has conflicting content")
            return DurableExecutionResult(state, Money(0), True)
        return None

    def recover(self, campaign_id: str, order_id: str) -> DurableUnfilledOrderState:
        if not campaign_id or not order_id:
            raise ValueError("campaign and order identities must not be empty")
        with self._unit_of_work_factory() as unit_of_work:
            associated_campaign = unit_of_work.unfilled_executions.campaign_id_for_order(order_id)
            if associated_campaign is None:
                raise MissingPersistenceStateError("durable order was not found")
            if associated_campaign != campaign_id:
                raise PersistenceConflictError("order belongs to another campaign")
            if unit_of_work.campaigns.lock(campaign_id) is None:
                raise MissingPersistenceStateError("campaign is not initialized")
            state = unit_of_work.unfilled_executions.lock_order_then_reservation(order_id)
            if state is None:
                raise MissingPersistenceStateError("durable unfilled lifecycle is incomplete")
            unit_of_work.commit()
            return state

    def apply_execution_event(self, command: ApplyExecutionEventCommand) -> DurableExecutionResult:
        recorded_at = self._recorded_at(command)
        with self._unit_of_work_factory() as unit_of_work:
            associated_campaign = unit_of_work.unfilled_executions.campaign_id_for_order(
                command.order_id
            )
            if associated_campaign is None:
                raise MissingPersistenceStateError("durable order was not found")
            if associated_campaign != command.campaign_id:
                raise PersistenceConflictError("order belongs to another campaign")
            if unit_of_work.campaigns.lock(command.campaign_id) is None:
                raise MissingPersistenceStateError("campaign is not initialized")
            state = unit_of_work.unfilled_executions.lock_order_then_reservation(command.order_id)
            if state is None:
                raise MissingPersistenceStateError("durable unfilled lifecycle is incomplete")
            existing = self._existing_result(state, command.event)
            if existing is not None:
                unit_of_work.commit()
                return existing
            self._validate_script_and_event(state, command.script, command.event)
            applied = apply_unfilled_order_event(
                state.order,
                state.reservation.transition_value(),
                command.event,
            )
            released_at = (
                command.event.occurred_at
                if command.event.event_type is ExecutionEventType.EXPIRED
                else None
            )
            after = unit_of_work.stage_unfilled_execution_transition(
                before=state,
                after_order=applied.order,
                after_current_amount=applied.reservation.maximum_loss,
                after_reservation_state=applied.reservation.state,
                released_at=released_at,
                event=command.event,
                recorded_at=recorded_at,
                fault=lambda point: self._raise_at(command, point),
            )
            self._raise_at(command, ExecutionFaultPoint.BEFORE_COMMIT.value)
            unit_of_work.commit()
            return DurableExecutionResult(
                state=after,
                released_maximum_loss=applied.released_maximum_loss,
                idempotent_replay=False,
            )
