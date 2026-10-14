"""The only execution mechanism in the golden slice: deterministic synthetic fills."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from apex.application.interfaces import (
    ExecutionScript,
    PartialFillCanceledExecutionScript,
    UnfilledExpiryExecutionScript,
)
from apex.domain.decisions import Side
from apex.domain.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExpiringLimitOrder,
    Order,
    OrderState,
)
from apex.domain.market import MarketSnapshot
from apex.domain.values import (
    ContentHash,
    Money,
    Price,
    Quantity,
    ValueValidationError,
    content_digest,
    deterministic_id,
    floor_to_increment,
    parse_decimal,
)


class SimulationError(ValueError):
    """An order cannot be represented by the fixed golden execution script."""


def _event(
    *,
    order: Order,
    script: ExecutionScript,
    sequence: int,
    event_type: ExecutionEventType,
    occurred_at: datetime,
    quantity: Quantity,
    price: Price | None,
    fee: Money,
) -> ExecutionEvent:
    payload = {
        "order_id": order.order_id,
        "script_id": script.script_id,
        "sequence": sequence,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "quantity": quantity,
        "price": price,
        "fee": fee,
    }
    return ExecutionEvent(
        event_id=deterministic_id("execution_event", payload),
        order_id=order.order_id,
        sequence=sequence,
        event_type=event_type,
        occurred_at=occurred_at,
        quantity=quantity,
        price=price,
        fee=fee,
        payload_hash=ContentHash(content_digest(payload)),
    )


class DeterministicSimulator:
    """Emit one of the frozen synthetic execution lifecycles; never perform I/O."""

    def simulate(
        self,
        order: Order,
        script: ExecutionScript,
        market: MarketSnapshot,
    ) -> tuple[ExecutionEvent, ...]:
        if isinstance(script, PartialFillCanceledExecutionScript):
            return self._simulate_partial_fill_cancellation(order, script, market)
        if isinstance(script, UnfilledExpiryExecutionScript):
            return self._simulate_unfilled_expiration(order, script, market)
        if script.kind != "FULL_MARKETABLE_FILL":
            raise SimulationError("the golden simulator supports a full marketable fill only")
        if order.side is not Side.YES:
            raise SimulationError("the golden simulator supports the engineered YES order only")
        if order.state is not OrderState.READY or order.last_execution_sequence != 0:
            raise SimulationError("the golden simulator requires a fresh READY order")
        if script.fill_sequence != script.opened_sequence + 1:
            raise SimulationError("scripted execution sequence is not contiguous")
        if script.filled_at < script.opened_at:
            raise SimulationError("scripted fill cannot occur before order opening")
        try:
            fill_price = Price(script.fill_price, order.limit_price.tick)
            fee_per_contract = parse_decimal(script.fee_per_contract)
            fee = Money(fee_per_contract * order.quantity.value)
        except ValueValidationError as error:
            raise SimulationError("scripted price or fee is invalid") from error
        if fill_price.value > order.limit_price.value:
            raise SimulationError("scripted fill price violates the order limit")
        zero_quantity = Quantity(Decimal(0), order.quantity.increment)
        opened = _event(
            order=order,
            script=script,
            sequence=script.opened_sequence,
            event_type=ExecutionEventType.OPENED,
            occurred_at=script.opened_at,
            quantity=zero_quantity,
            price=None,
            fee=Money(0),
        )
        filled = _event(
            order=order,
            script=script,
            sequence=script.fill_sequence,
            event_type=ExecutionEventType.FILL,
            occurred_at=script.filled_at,
            quantity=order.quantity,
            price=fill_price,
            fee=fee,
        )
        return opened, filled

    @staticmethod
    def _simulate_unfilled_expiration(
        order: Order,
        script: UnfilledExpiryExecutionScript,
        market: MarketSnapshot,
    ) -> tuple[ExecutionEvent, ExecutionEvent]:
        if script.kind != "NONMARKETABLE_EXPIRE_UNFILLED":
            raise SimulationError("unfilled script kind is invalid")
        if not isinstance(order, ExpiringLimitOrder):
            raise SimulationError("unfilled expiration requires an expiring limit order")
        if order.side is not Side.YES or script.side != "YES":
            raise SimulationError("unfilled expiration supports the engineered YES order only")
        if order.state is not OrderState.READY or order.last_execution_sequence != 0:
            raise SimulationError("unfilled expiration requires a fresh READY order")
        if script.expired_sequence != script.opened_sequence + 1:
            raise SimulationError("scripted unfilled event sequence is not contiguous")
        if script.expires_at <= script.opened_at:
            raise SimulationError("scripted expiration must occur after opening")
        if script.expires_at != order.expires_at:
            raise SimulationError("script expiration does not match the authorized order")
        try:
            scripted_yes_ask = Price(script.executable_yes_ask, order.limit_price.tick)
            scripted_no_ask = Price(script.executable_no_ask, order.limit_price.tick)
            scripted_visible = Quantity(
                script.visible_yes_quantity,
                order.quantity.increment,
            )
        except ValueValidationError as error:
            raise SimulationError("scripted unfilled book is invalid") from error
        if (
            scripted_yes_ask != market.yes_ask
            or scripted_no_ask != market.no_ask
            or scripted_visible != market.visible_yes_quantity
        ):
            raise SimulationError("scripted unfilled book does not match the information set")
        if order.executable_ask != market.yes_ask:
            raise SimulationError("authorized order executable ask does not match the market")
        if order.limit_price.value >= market.yes_ask.value:
            raise SimulationError("YES limit is marketable against the synthetic ask")
        zero_quantity = Quantity(Decimal(0), order.quantity.increment)
        opened = _event(
            order=order,
            script=script,
            sequence=script.opened_sequence,
            event_type=ExecutionEventType.OPENED,
            occurred_at=script.opened_at,
            quantity=zero_quantity,
            price=None,
            fee=Money(0),
        )
        expired = _event(
            order=order,
            script=script,
            sequence=script.expired_sequence,
            event_type=ExecutionEventType.EXPIRED,
            occurred_at=script.expires_at,
            quantity=zero_quantity,
            price=None,
            fee=Money(0),
        )
        return opened, expired

    @staticmethod
    def _simulate_partial_fill_cancellation(
        order: Order,
        script: PartialFillCanceledExecutionScript,
        market: MarketSnapshot,
    ) -> tuple[ExecutionEvent, ExecutionEvent, ExecutionEvent]:
        if script.kind != "PARTIAL_FILL_THEN_CANCEL":
            raise SimulationError("partial-fill script kind is invalid")
        if not isinstance(order, ExpiringLimitOrder):
            raise SimulationError("partial fill requires an expiring resting-limit order")
        if order.side is not Side.YES or script.side != "YES":
            raise SimulationError("partial fill supports the engineered YES order only")
        if order.state is not OrderState.READY or order.last_execution_sequence != 0:
            raise SimulationError("partial fill requires a fresh READY order")
        if (
            script.opened_sequence != 1
            or script.fill_sequence != 2
            or script.canceled_sequence != 3
        ):
            raise SimulationError("partial-fill sequence must be exactly 1, 2, 3")
        if not (script.opened_at < script.filled_at < script.canceled_at < order.expires_at):
            raise SimulationError("partial-fill timestamps are inconsistent")
        try:
            scripted_yes_ask = Price(script.executable_yes_ask, order.limit_price.tick)
            scripted_no_ask = Price(script.executable_no_ask, order.limit_price.tick)
            scripted_visible = Quantity(
                script.visible_yes_quantity,
                order.quantity.increment,
            )
            baseline_ordered = Quantity(
                script.ordered_quantity,
                order.quantity.increment,
            )
            baseline_fill = Quantity(
                script.fill_quantity,
                order.quantity.increment,
            )
            baseline_remaining = Quantity(
                script.remaining_quantity,
                order.quantity.increment,
            )
            fill_price = Price(script.fill_price, order.limit_price.tick)
            fee_per_contract = parse_decimal(script.fee_per_contract, allow_zero=False)
        except ValueValidationError as error:
            raise SimulationError("partial-fill book or economics are invalid") from error
        if (
            scripted_yes_ask != market.yes_ask
            or scripted_no_ask != market.no_ask
            or scripted_visible != market.visible_yes_quantity
        ):
            raise SimulationError("partial-fill book does not match the information set")
        if (
            order.executable_ask != market.yes_ask
            or order.limit_price.value >= market.yes_ask.value
        ):
            raise SimulationError("partial-fill order is not the authorized resting order")
        if (
            baseline_ordered.value <= 0
            or not 0 < baseline_fill.value < baseline_ordered.value
            or baseline_fill.value + baseline_remaining.value != baseline_ordered.value
        ):
            raise SimulationError("partial-fill baseline quantities are inconsistent")
        if fill_price.value > order.limit_price.value:
            raise SimulationError("partial fill price violates the order limit")
        fill_ratio = baseline_fill.value / baseline_ordered.value
        runtime_fill_value = max(
            order.quantity.increment,
            floor_to_increment(
                order.quantity.value * fill_ratio,
                order.quantity.increment,
            ),
        )
        if not 0 < runtime_fill_value < order.quantity.value:
            raise SimulationError("scaled partial fill is not strictly partial")
        fill_quantity = Quantity(runtime_fill_value, order.quantity.increment)
        fee = Money(fee_per_contract * fill_quantity.value)
        zero_quantity = Quantity(Decimal(0), order.quantity.increment)
        opened = _event(
            order=order,
            script=script,
            sequence=script.opened_sequence,
            event_type=ExecutionEventType.OPENED,
            occurred_at=script.opened_at,
            quantity=zero_quantity,
            price=None,
            fee=Money(0),
        )
        filled = _event(
            order=order,
            script=script,
            sequence=script.fill_sequence,
            event_type=ExecutionEventType.FILL,
            occurred_at=script.filled_at,
            quantity=fill_quantity,
            price=fill_price,
            fee=fee,
        )
        canceled = _event(
            order=order,
            script=script,
            sequence=script.canceled_sequence,
            event_type=ExecutionEventType.CANCELED,
            occurred_at=script.canceled_at,
            quantity=zero_quantity,
            price=None,
            fee=Money(0),
        )
        return opened, filled, canceled
