"""Deterministic, provider-shaped external PAPER broker fixture.

This adapter is deliberately local: it has no credentials, transport, or Alpaca
identity.  Its observations use the same sanitised evidence contract as the
real Paper adapter so lifecycle and reconciliation code exercise the real port.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.domain.external_paper import (
    ExternalPaperBrokerError,
    ExternalPaperBrokerEvidence,
    ExternalPaperInstruction,
    ExternalPaperSubmissionAmbiguous,
)


@dataclass(frozen=True, slots=True)
class FixturePaperScenario:
    name: str
    submitted_status: str = "accepted"
    filled_quantity: str = "0"
    price: str = "100"
    ambiguous_after_submission: bool = False
    unavailable: bool = False
    authentication_rejected: bool = False
    cancel_fill_quantity: str | None = None


FULL_FILL = FixturePaperScenario("FULL_FILL", "filled", "1")
PARTIAL_FILL = FixturePaperScenario("PARTIAL_FILL", "partially_filled", "0.5")
REJECTED = FixturePaperScenario("REJECTED", "rejected", "0")
OPEN = FixturePaperScenario("OPEN", "accepted", "0")
AMBIGUOUS = FixturePaperScenario("AMBIGUOUS", "accepted", "1", ambiguous_after_submission=True)
UNAVAILABLE = FixturePaperScenario("UNAVAILABLE", unavailable=True)
AUTHENTICATION_REJECTED = FixturePaperScenario(
    "AUTHENTICATION_REJECTED", authentication_rejected=True
)


class DeterministicPaperBrokerFixture:
    """A replayable fixture that rejects conflicting idempotency input."""

    identity = "DETERMINISTIC_PAPER_BROKER_FIXTURE_V1"

    def __init__(self, *, scenarios: dict[str, FixturePaperScenario], now: datetime) -> None:
        self._scenarios = dict(scenarios)
        self._now = now.astimezone(UTC)
        self._orders: dict[str, dict[str, str | None]] = {}
        self._by_instruction: dict[str, str] = {}
        self._scenario_by_instruction: dict[str, FixturePaperScenario] = {}
        self._evidence: list[ExternalPaperBrokerEvidence] = []

    @property
    def network_calls(self) -> int:
        return 0

    @property
    def evidence(self) -> tuple[ExternalPaperBrokerEvidence, ...]:
        return tuple(self._evidence)

    def submit(self, instruction: ExternalPaperInstruction) -> ExternalPaperBrokerEvidence:
        existing_id = self._by_instruction.get(instruction.instruction_id)
        if existing_id is not None:
            order = self._orders[existing_id]
            if order["qty"] != str(instruction.quantity) or order["symbol"] != instruction.symbol:
                raise ExternalPaperBrokerError("FIXTURE_CONFLICTING_REPLAY")
            return self._retain(instruction.instruction_id, "ORDER_ACKNOWLEDGMENT", order)
        scenario = self._scenarios.get(instruction.idempotency_key, OPEN)
        if scenario.unavailable:
            raise ExternalPaperBrokerError("FIXTURE_PROVIDER_UNAVAILABLE")
        if scenario.authentication_rejected:
            raise ExternalPaperBrokerError("FIXTURE_AUTHENTICATION_REJECTED")
        broker_order_id = f"paper-fixture-{len(self._orders) + 1:04d}"
        order = {
            "id": broker_order_id,
            "client_order_id": f"fixture-{instruction.instruction_id[-16:]}",
            "symbol": instruction.symbol,
            "side": instruction.side.lower(),
            "status": scenario.submitted_status,
            "qty": str(instruction.quantity),
            "filled_qty": scenario.filled_quantity,
            "filled_avg_price": scenario.price if Decimal(scenario.filled_quantity) else None,
            "submitted_at": self._timestamp(0),
            "updated_at": self._timestamp(0),
        }
        self._orders[broker_order_id] = order
        self._by_instruction[instruction.instruction_id] = broker_order_id
        self._scenario_by_instruction[instruction.instruction_id] = scenario
        if scenario.ambiguous_after_submission:
            raise ExternalPaperSubmissionAmbiguous("FIXTURE_TIMEOUT_AFTER_POSSIBLE_SUBMISSION")
        return self._retain(instruction.instruction_id, "ORDER_ACKNOWLEDGMENT", order)

    def order(self, *, instruction_id: str, broker_order_id: str) -> ExternalPaperBrokerEvidence:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise ExternalPaperBrokerError("FIXTURE_ORDER_NOT_FOUND")
        return self._retain(instruction_id, "ORDER_STATUS", order)

    def cancel(self, *, instruction_id: str, broker_order_id: str) -> ExternalPaperBrokerEvidence:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise ExternalPaperBrokerError("FIXTURE_ORDER_NOT_FOUND")
        if order["status"] not in {"filled", "rejected", "canceled"}:
            scenario = self._scenario_by_instruction[instruction_id]
            fill_quantity = scenario.cancel_fill_quantity
            order = {
                **order,
                "status": "canceled",
                "filled_qty": fill_quantity if fill_quantity is not None else order["filled_qty"],
                "filled_avg_price": (
                    scenario.price
                    if fill_quantity is not None and Decimal(fill_quantity) > 0
                    else order["filled_avg_price"]
                ),
                "canceled_at": self._timestamp(1),
                "updated_at": self._timestamp(1),
            }
            self._orders[broker_order_id] = order
        return self._retain(instruction_id, "ORDER_CANCELLATION", order)

    def account(self) -> ExternalPaperBrokerEvidence:
        return self._retain(None, "ACCOUNT_READINESS", {"cash": "1000", "buying_power": "1000"})

    def orders(self) -> tuple[ExternalPaperBrokerEvidence, ...]:
        return tuple(
            self._retain(None, "ORDER_LIST_ITEM", order)
            for _, order in sorted(self._orders.items())
        )

    def positions(self) -> tuple[ExternalPaperBrokerEvidence, ...]:
        return ()

    def activities(self) -> tuple[ExternalPaperBrokerEvidence, ...]:
        return tuple(
            self._retain(
                None,
                "FILL_ACTIVITY_ITEM",
                {
                    "order_id": str(order["id"]),
                    "symbol": str(order["symbol"]),
                    "side": str(order["side"]),
                    "qty": str(order["filled_qty"]),
                    "price": order["filled_avg_price"],
                },
            )
            for _, order in sorted(self._orders.items())
            if Decimal(str(order["filled_qty"])) > 0
        )

    def complete_partial(self, broker_order_id: str) -> None:
        order = self._orders[broker_order_id]
        self._orders[broker_order_id] = {
            **order,
            "status": "filled",
            "filled_qty": order["qty"],
            "filled_at": self._timestamp(2),
            "updated_at": self._timestamp(2),
        }

    def recover_submission(self, *, instruction_id: str) -> ExternalPaperBrokerEvidence:
        broker_order_id = self._by_instruction.get(instruction_id)
        if broker_order_id is None:
            raise ExternalPaperBrokerError("FIXTURE_ORDER_NOT_FOUND")
        return self._retain(instruction_id, "ORDER_RECOVERY", self._orders[broker_order_id])

    def _retain(
        self, instruction_id: str | None, observation_type: str, payload: Mapping[str, str | None]
    ) -> ExternalPaperBrokerEvidence:
        evidence = ExternalPaperBrokerEvidence.create(
            instruction_id=instruction_id,
            endpoint_identity=f"PAPER:FIXTURE:{observation_type}",
            observation_type=observation_type,
            observed_at=self._now,
            broker_reported_at=self._now,
            request_id=f"fixture-request-{len(self._evidence) + 1:04d}",
            sanitized_payload=dict(payload),
        )
        self._evidence.append(evidence)
        return evidence

    def _timestamp(self, seconds: int) -> str:
        return (self._now + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
