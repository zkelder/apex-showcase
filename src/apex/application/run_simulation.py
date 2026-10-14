"""The single synchronous in-memory Apex golden-path use case."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from apex.application.interfaces import (
    DecisionArtifacts,
    DecisionInputs,
    ExecutionScript,
    InvariantRejectedResult,
    NoProposalResult,
    PartialFillCanceledExecutionScript,
    PartialFillCanceledResult,
    PartialFillCanceledSettledNoResult,
    PartialFillCanceledSettledVoidResult,
    PolicyRejectedResult,
    RunMetadata,
    RunResult,
    SettlementInputs,
    SimulationResult,
    UnfilledExpiredResult,
    UnfilledExpiryExecutionScript,
)
from apex.domain.campaign import (
    Campaign,
    CampaignSnapshot,
    OperationalInvariants,
    active_campaign_risk,
)
from apex.domain.decisions import (
    INVARIANT_MARKET_DATA_STALE,
    POLICY_MAX_CAPITAL_LOCKUP_EXCEEDED,
    AllocationDecision,
    CapitalAllocator,
    OpportunityProposal,
    ReferenceEdgeStrategy,
    RestingLimitProposal,
    RiskDecision,
    RiskKernel,
    StaleMarketRiskDecision,
    capital_lockup_policy_explanation,
    expected_capital_lockup_seconds,
)
from apex.domain.execution import (
    AppliedClassifiedPartialPositionSettlement,
    AppliedPartialPositionSettlement,
    AppliedSettlement,
    ClassifiedRuleBoundSettlementFact,
    ExecutionEvent,
    ExecutionEventType,
    ExpiringLimitOrder,
    FilledPositionState,
    Order,
    PositionState,
    RiskReservation,
    RuleBoundSettlementFact,
    SettlementFact,
    SettlementResult,
    apply_execution_event,
    apply_partial_fill_canceled_event,
    apply_unfilled_order_event,
    create_reserved_order,
    initial_position,
    settle_canceled_filled_position,
    settle_canceled_filled_void_position,
    settle_position,
    validate_execution_event_identity,
)
from apex.domain.ledger import JournalFactory, Ledger, LedgerMetrics
from apex.domain.market import (
    InformationSetBuilder,
    MarketSnapshot,
    ReferenceEstimate,
    VoidContractRuleVersion,
)
from apex.domain.values import (
    Money,
    Price,
    Quantity,
    RiskFraction,
    ValueValidationError,
    deterministic_id,
    floor_to_increment,
    parse_decimal,
)

_PARTIAL_FILL_CANCELED_SETTLED_NO_FIXTURE_ID = "partial-fill-canceled-settled-no"
_PARTIAL_FILL_CANCELED_SETTLED_VOID_FIXTURE_ID = "partial-fill-canceled-settled-void"
_PARTIAL_FILL_CANCELED_SETTLED_FIXTURE_IDS = frozenset(
    (
        _PARTIAL_FILL_CANCELED_SETTLED_NO_FIXTURE_ID,
        _PARTIAL_FILL_CANCELED_SETTLED_VOID_FIXTURE_ID,
    )
)
_PARTIAL_SETTLEMENT_SOURCE_IDS = {
    _PARTIAL_FILL_CANCELED_SETTLED_NO_FIXTURE_ID: "settlement-partial-fill-canceled-no",
    _PARTIAL_FILL_CANCELED_SETTLED_VOID_FIXTURE_ID: "settlement-partial-fill-canceled-void",
}


class SimulationConflictError(ValueError):
    """An idempotency identity was reused with conflicting content."""


class SyntheticExecutionEmitter(Protocol):
    """The deliberately narrow event emitter needed by this synthetic slice."""

    def simulate(
        self,
        order: Order,
        script: ExecutionScript,
        market: MarketSnapshot,
    ) -> tuple[ExecutionEvent, ...]: ...


@dataclass(frozen=True, slots=True)
class ExecutionCheckpoint:
    decision: DecisionArtifacts
    events: tuple[ExecutionEvent, ...]
    position: PositionState
    metrics: LedgerMetrics


@dataclass(frozen=True, slots=True)
class OrderCheckpoint:
    decision: DecisionArtifacts
    events: tuple[ExecutionEvent, ...]
    reservation_lifecycle: tuple[RiskReservation, ...]
    metrics: LedgerMetrics


@dataclass(frozen=True, slots=True)
class PartialExecutionCheckpoint:
    decision: DecisionArtifacts
    events: tuple[ExecutionEvent, ...]
    reservation_lifecycle: tuple[RiskReservation, ...]
    position: FilledPositionState | None
    metrics: LedgerMetrics


class AtomicOrderBook:
    """Consume accepted decisions exactly once while creating reservation and order together."""

    def __init__(self) -> None:
        self._by_risk_decision: dict[str, tuple[RiskReservation, Order]] = {}

    def authorize_and_reserve(
        self,
        *,
        campaign_id: str,
        proposal: OpportunityProposal,
        allocation: AllocationDecision,
        risk_decision: RiskDecision,
    ) -> tuple[RiskReservation, Order]:
        candidate = create_reserved_order(
            campaign_id=campaign_id,
            proposal=proposal,
            allocation=allocation,
            risk_decision=risk_decision,
        )
        existing = self._by_risk_decision.get(risk_decision.risk_decision_id)
        if existing is not None:
            if existing == candidate:
                return existing
            raise SimulationConflictError("risk decision was reused for a different order")
        self._by_risk_decision[risk_decision.risk_decision_id] = candidate
        return candidate


class GoldenSimulation:
    """Own sequencing, idempotency, and the in-memory financial transaction boundary."""

    def __init__(
        self,
        *,
        decision_inputs: DecisionInputs,
        execution_script: ExecutionScript,
        run_metadata: RunMetadata,
        capital: Money,
        seed: int,
        engine_version: str,
        simulator: SyntheticExecutionEmitter,
    ) -> None:
        if capital.amount <= 0:
            raise ValueValidationError("campaign capital must be positive")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueValidationError("seed must be a non-negative integer")
        self._inputs = decision_inputs
        self._script = execution_script
        self._metadata = run_metadata
        self._capital = capital
        self._seed = seed
        self._engine_version = engine_version
        self._simulator = simulator
        if self._inputs.risk_authorization_at < self._inputs.strategy_decision_at:
            raise ValueValidationError(
                "risk authorization cannot precede the strategy decision time"
            )
        if self._inputs.fixture_id in _PARTIAL_FILL_CANCELED_SETTLED_FIXTURE_IDS and not isinstance(
            self._script, PartialFillCanceledExecutionScript
        ):
            raise ValueValidationError(
                "settled partial-fill fixture requires the exact partial execution script"
            )
        self._ledger = Ledger()
        self._orders = AtomicOrderBook()
        self._event_identities: dict[str, ExecutionEvent] = {}
        self._settlement_identities: dict[str, tuple[object, AppliedSettlement]] = {}
        self._partial_settlement_application: (
            tuple[
                SettlementInputs,
                AppliedPartialPositionSettlement | AppliedClassifiedPartialPositionSettlement,
            ]
            | None
        ) = None
        self._no_proposal_result: NoProposalResult | None = None
        self._policy_rejected_result: PolicyRejectedResult | None = None
        self._invariant_rejected_result: InvariantRejectedResult | None = None
        self._unfilled_expired_result: UnfilledExpiredResult | None = None
        self._partial_fill_canceled_result: PartialFillCanceledResult | None = None
        self._partial_fill_canceled_settled_no_result: PartialFillCanceledSettledNoResult | None = (
            None
        )
        self._partial_fill_canceled_settled_void_result: (
            PartialFillCanceledSettledVoidResult | None
        ) = None
        self._decision: DecisionArtifacts | None = None
        self._events: list[ExecutionEvent] = []
        self._reservation_lifecycle: list[RiskReservation] = []
        self._position: PositionState | None = None
        self._pre_settlement_position: FilledPositionState | None = None
        self._pre_settlement_metrics: LedgerMetrics | None = None
        self._partial_risk_capacities: tuple[Money, Money, Money, Money] | None = None
        self._settlement: SettlementResult | None = None

    def _run_id(self) -> str:
        return deterministic_id(
            "run",
            {
                "fixture_manifest_hash": self._metadata.fixture_manifest_hash,
                "capital": self._capital,
                "seed": self._seed,
                "engine_version": self._engine_version,
                "policy_hash": self._inputs.policy.content_hash,
            },
        )

    def run_decision_and_execution(
        self,
    ) -> (
        ExecutionCheckpoint
        | NoProposalResult
        | PolicyRejectedResult
        | InvariantRejectedResult
        | UnfilledExpiredResult
        | PartialFillCanceledResult
        | PartialFillCanceledSettledNoResult
        | PartialFillCanceledSettledVoidResult
        | PartialExecutionCheckpoint
    ):
        if self._no_proposal_result is not None:
            return self._no_proposal_result
        if self._policy_rejected_result is not None:
            return self._policy_rejected_result
        if self._invariant_rejected_result is not None:
            return self._invariant_rejected_result
        if self._unfilled_expired_result is not None:
            return self._unfilled_expired_result
        if self._partial_fill_canceled_result is not None:
            return self._partial_fill_canceled_result
        if self._partial_fill_canceled_settled_no_result is not None:
            return self._partial_fill_canceled_settled_no_result
        if self._partial_fill_canceled_settled_void_result is not None:
            return self._partial_fill_canceled_settled_void_result
        if self._decision is not None:
            if isinstance(self._script, PartialFillCanceledExecutionScript):
                if self._partial_settlement_application is not None:
                    raise RuntimeError("partial settlement result cache is incomplete")
                if (
                    self._inputs.fixture_id in _PARTIAL_FILL_CANCELED_SETTLED_FIXTURE_IDS
                    and isinstance(self._position, FilledPositionState)
                    and len(self._events) == 3
                    and len(self._reservation_lifecycle) == 4
                ):
                    return PartialExecutionCheckpoint(
                        self._decision,
                        tuple(self._events),
                        tuple(self._reservation_lifecycle),
                        self._position,
                        self._ledger.metrics(),
                    )
                raise RuntimeError("partial-fill cancellation state is incomplete")
            if self._position is None:
                raise RuntimeError("simulation state is incomplete")
            return ExecutionCheckpoint(
                self._decision,
                tuple(self._events),
                self._position,
                self._ledger.metrics(),
            )

        authorized_fraction = RiskFraction(self._inputs.authorized_loss_fraction)
        authorized_loss = Money(self._capital.amount * authorized_fraction.value)
        campaign = Campaign(
            campaign_id=self._inputs.campaign_id,
            contribution=self._capital,
            authorized_loss=authorized_loss,
            policy=self._inputs.policy,
        )
        contribution_source = deterministic_id(
            "contribution",
            {"campaign_id": campaign.campaign_id, "amount": self._capital},
        )
        self._ledger.post(
            JournalFactory.contribution(
                contribution_source,
                self._capital,
                self._inputs.rule.effective_at,
            )
        )
        opening_metrics = self._ledger.metrics()
        snapshot = CampaignSnapshot(
            campaign=campaign,
            ledger_equity=opening_metrics.ledger_equity,
            settled_cash=opening_metrics.cash,
            active_reservations=Money(0),
        )
        information_set = InformationSetBuilder.build(
            self._inputs.source_records,
            self._inputs.instrument,
            self._inputs.cutoff,
            self._inputs.rule,
            self._inputs.market,
        )
        estimate_input = self._inputs.reference_estimate
        estimate = ReferenceEstimate(
            estimate_id=estimate_input.estimate_id,
            source_id=estimate_input.source_id,
            probability_vector=estimate_input.probability_vector,
            published_at=estimate_input.published_at,
            expires_at=estimate_input.expires_at,
            method_version=estimate_input.method_version,
            information_set_id=information_set.information_set_id,
        )
        fee_per_unit = Money(self._inputs.fee_per_contract)
        evaluation = ReferenceEdgeStrategy.evaluate(
            information_set,
            estimate,
            self._inputs.policy,
            strategy_version=self._inputs.strategy_version,
            fee_model_version=self._inputs.fee_model_version,
            fee_per_unit=fee_per_unit,
            decision_at=self._inputs.strategy_decision_at,
            resting_limit_price=self._inputs.resting_limit_price,
            order_expires_at=self._inputs.order_expires_at,
        )
        proposal = evaluation.proposal
        if proposal is None:
            self._no_proposal_result = NoProposalResult(
                run_id=self._run_id(),
                fixture_id=self._inputs.fixture_id,
                fixture_manifest_hash=self._metadata.fixture_manifest_hash,
                engine_version=self._engine_version,
                seed=self._seed,
                logical_time=self._metadata.logical_time,
                campaign=campaign,
                information_set=information_set,
                strategy_evaluation=evaluation,
                journals=self._ledger.journals,
                metrics=self._ledger.metrics(),
            )
            return self._no_proposal_result
        allocation = CapitalAllocator.allocate(
            proposal,
            snapshot,
            information_set.market.visible_yes_quantity,
            self._inputs.policy,
            decision_at=self._inputs.strategy_decision_at,
            expected_settlement_at=information_set.rule.expected_settlement_at,
        )
        if not allocation.accepted:
            if allocation.reason_codes != (POLICY_MAX_CAPITAL_LOCKUP_EXCEEDED,):
                raise ValueValidationError(
                    f"unsupported allocation rejection: {allocation.reason_codes}"
                )
            self._policy_rejected_result = PolicyRejectedResult(
                run_id=self._run_id(),
                fixture_id=self._inputs.fixture_id,
                fixture_manifest_hash=self._metadata.fixture_manifest_hash,
                engine_version=self._engine_version,
                seed=self._seed,
                logical_time=self._metadata.logical_time,
                campaign=campaign,
                information_set=information_set,
                strategy_evaluation=evaluation,
                proposal=proposal,
                allocation=allocation,
                policy_reason_codes=allocation.reason_codes,
                policy_explanation=capital_lockup_policy_explanation(
                    expected_capital_lockup_seconds(
                        self._inputs.strategy_decision_at,
                        information_set.rule.expected_settlement_at,
                    ),
                    self._inputs.policy.max_capital_lockup_seconds,
                ),
                journals=self._ledger.journals,
                metrics=self._ledger.metrics(),
            )
            return self._policy_rejected_result
        risk_decision = RiskKernel.evaluate(
            proposal,
            allocation,
            snapshot,
            information_set,
            self._inputs.policy,
            OperationalInvariants(),
            authorization_at=self._inputs.risk_authorization_at,
            market_observation_source_id=self._inputs.market_observation_source_id,
        )
        if not risk_decision.accepted:
            if not isinstance(risk_decision, StaleMarketRiskDecision) or (
                risk_decision.reason_codes != (INVARIANT_MARKET_DATA_STALE,)
            ):
                raise ValueValidationError(
                    f"unsupported risk invariant rejection: {risk_decision.reason_codes}"
                )
            self._invariant_rejected_result = InvariantRejectedResult(
                run_id=self._run_id(),
                fixture_id=self._inputs.fixture_id,
                fixture_manifest_hash=self._metadata.fixture_manifest_hash,
                engine_version=self._engine_version,
                seed=self._seed,
                logical_time=self._metadata.logical_time,
                campaign=campaign,
                information_set=information_set,
                strategy_evaluation=evaluation,
                proposal=proposal,
                allocation=allocation,
                risk_decision=risk_decision,
                invariant_reason_codes=risk_decision.reason_codes,
                invariant_explanation=risk_decision.invariant_explanation,
                journals=self._ledger.journals,
                metrics=self._ledger.metrics(),
            )
            return self._invariant_rejected_result
        reservation, order = self._orders.authorize_and_reserve(
            campaign_id=campaign.campaign_id,
            proposal=proposal,
            allocation=allocation,
            risk_decision=risk_decision,
        )
        decision = DecisionArtifacts(
            campaign=campaign,
            information_set=information_set,
            strategy_evaluation=evaluation,
            allocation=allocation,
            risk_decision=risk_decision,
            reservation=reservation,
            order=order,
        )
        if isinstance(self._script, PartialFillCanceledExecutionScript):
            if not isinstance(proposal, RestingLimitProposal) or not isinstance(
                order, ExpiringLimitOrder
            ):
                raise ValueValidationError("partial fill requires bound resting-limit order terms")
            self._decision = decision
            self._reservation_lifecycle = [reservation]
            for event in self._simulator.simulate(
                order,
                self._script,
                information_set.market,
            ):
                self.apply_partial_execution(event)
            if (
                self._decision is None
                or not isinstance(self._decision.order, ExpiringLimitOrder)
                or not isinstance(self._position, FilledPositionState)
                or len(self._events) != 3
                or len(self._reservation_lifecycle) != 4
            ):
                raise RuntimeError("partial-fill cancellation did not produce complete state")
            (
                initial_reservation,
                open_reservation,
                after_fill_reservation,
                final_reservation,
            ) = self._reservation_lifecycle
            capacity_before = Money(snapshot.risk_capacity)
            full_active_risk = active_campaign_risk(
                Money(0),
                open_reservation.maximum_loss,
            )
            after_fill_active_risk = active_campaign_risk(
                self._position.maximum_remaining_loss,
                after_fill_reservation.maximum_loss,
            )
            final_active_risk = active_campaign_risk(
                self._position.maximum_remaining_loss,
                final_reservation.maximum_loss,
            )
            if any(
                active.amount > capacity_before.amount
                for active in (
                    full_active_risk,
                    after_fill_active_risk,
                    final_active_risk,
                )
            ):
                raise RuntimeError("partial-fill active risk exceeds campaign capacity")
            self._pre_settlement_position = self._position
            self._pre_settlement_metrics = self._ledger.metrics()
            self._partial_risk_capacities = (
                capacity_before,
                Money(capacity_before.amount - full_active_risk.amount),
                Money(capacity_before.amount - after_fill_active_risk.amount),
                Money(capacity_before.amount - final_active_risk.amount),
            )
            if self._inputs.fixture_id in _PARTIAL_FILL_CANCELED_SETTLED_FIXTURE_IDS:
                return PartialExecutionCheckpoint(
                    self._decision,
                    tuple(self._events),
                    tuple(self._reservation_lifecycle),
                    self._position,
                    self._pre_settlement_metrics,
                )
            self._partial_fill_canceled_result = PartialFillCanceledResult(
                run_id=self._run_id(),
                fixture_id=self._inputs.fixture_id,
                fixture_manifest_hash=self._metadata.fixture_manifest_hash,
                engine_version=self._engine_version,
                seed=self._seed,
                logical_time=self._metadata.logical_time,
                campaign=campaign,
                information_set=information_set,
                strategy_evaluation=evaluation,
                proposal=proposal,
                allocation=allocation,
                risk_decision=risk_decision,
                reservation_lifecycle=(
                    initial_reservation,
                    open_reservation,
                    after_fill_reservation,
                    final_reservation,
                ),
                order=self._decision.order,
                execution_events=(
                    self._events[0],
                    self._events[1],
                    self._events[2],
                ),
                position=self._position,
                risk_capacity_before_order=self._partial_risk_capacities[0],
                risk_capacity_while_fully_reserved=self._partial_risk_capacities[1],
                risk_capacity_after_partial_fill=self._partial_risk_capacities[2],
                risk_capacity_after_cancellation=self._partial_risk_capacities[3],
                journals=self._ledger.journals,
                metrics=self._pre_settlement_metrics,
            )
            return self._partial_fill_canceled_result
        if isinstance(self._script, UnfilledExpiryExecutionScript):
            if not isinstance(proposal, RestingLimitProposal) or not isinstance(
                order, ExpiringLimitOrder
            ):
                raise ValueValidationError(
                    "unfilled expiration requires bound resting-limit order terms"
                )
            self._decision = decision
            self._reservation_lifecycle = [reservation]
            for event in self._simulator.simulate(
                order,
                self._script,
                information_set.market,
            ):
                self.apply_unfilled_execution(event)
            if (
                self._decision is None
                or not isinstance(self._decision.order, ExpiringLimitOrder)
                or len(self._events) != 2
                or len(self._reservation_lifecycle) != 3
            ):
                raise RuntimeError("unfilled expiration did not produce complete state")
            final_order = self._decision.order
            initial_reservation, open_reservation, final_reservation = self._reservation_lifecycle
            capacity_before = Money(snapshot.risk_capacity)
            capacity_while_open = Money(
                CampaignSnapshot(
                    campaign=campaign,
                    ledger_equity=opening_metrics.ledger_equity,
                    settled_cash=opening_metrics.cash,
                    active_reservations=open_reservation.maximum_loss,
                ).risk_capacity
            )
            capacity_after = Money(
                CampaignSnapshot(
                    campaign=campaign,
                    ledger_equity=opening_metrics.ledger_equity,
                    settled_cash=opening_metrics.cash,
                    active_reservations=final_reservation.maximum_loss,
                ).risk_capacity
            )
            self._unfilled_expired_result = UnfilledExpiredResult(
                run_id=self._run_id(),
                fixture_id=self._inputs.fixture_id,
                fixture_manifest_hash=self._metadata.fixture_manifest_hash,
                engine_version=self._engine_version,
                seed=self._seed,
                logical_time=self._metadata.logical_time,
                campaign=campaign,
                information_set=information_set,
                strategy_evaluation=evaluation,
                proposal=proposal,
                allocation=allocation,
                risk_decision=risk_decision,
                reservation_lifecycle=(
                    initial_reservation,
                    open_reservation,
                    final_reservation,
                ),
                order=final_order,
                execution_events=(self._events[0], self._events[1]),
                risk_capacity_before_order=capacity_before,
                risk_capacity_while_open=capacity_while_open,
                risk_capacity_after_expiration=capacity_after,
                journals=self._ledger.journals,
                metrics=self._ledger.metrics(),
            )
            return self._unfilled_expired_result
        position = initial_position(
            self._inputs.instrument.instrument_id,
            self._inputs.rule.rule_version_id,
            self._inputs.instrument.quantity_increment,
        )
        self._decision = decision
        self._position = position
        for event in self._simulator.simulate(
            order,
            self._script,
            information_set.market,
        ):
            self.apply_execution(event)
        if self._decision is None or self._position is None:
            raise RuntimeError("execution did not produce complete state")
        return ExecutionCheckpoint(
            self._decision,
            tuple(self._events),
            self._position,
            self._ledger.metrics(),
        )

    def apply_partial_execution(
        self,
        event: ExecutionEvent,
    ) -> PartialExecutionCheckpoint:
        if (
            self._decision is None
            or not isinstance(self._decision.order, ExpiringLimitOrder)
            or not isinstance(self._script, PartialFillCanceledExecutionScript)
            or (self._position is not None and not isinstance(self._position, FilledPositionState))
        ):
            raise RuntimeError("partial-fill order authorization must exist before execution")
        proposal = self._decision.strategy_evaluation.proposal
        if not isinstance(proposal, RestingLimitProposal):
            raise RuntimeError("partial-fill execution is missing its resting-limit proposal")
        existing = self._event_identities.get(event.event_id)
        if existing is not None:
            if existing != event:
                raise SimulationConflictError("execution event identity has conflicting content")
            return PartialExecutionCheckpoint(
                self._decision,
                tuple(self._events),
                tuple(self._reservation_lifecycle),
                self._position,
                self._ledger.metrics(),
            )

        zero_quantity = Quantity(0, self._decision.order.quantity.increment)
        expected_quantity = zero_quantity
        expected_price: Price | None = None
        expected_fee = Money(0)
        if event.event_type is ExecutionEventType.OPENED:
            expected_sequence = self._script.opened_sequence
            expected_time = self._script.opened_at
        elif event.event_type is ExecutionEventType.FILL:
            expected_sequence = self._script.fill_sequence
            expected_time = self._script.filled_at
            baseline_ordered = parse_decimal(self._script.ordered_quantity)
            baseline_fill = parse_decimal(self._script.fill_quantity)
            baseline_remaining = parse_decimal(self._script.remaining_quantity)
            if (
                baseline_ordered <= 0
                or not 0 < baseline_fill < baseline_ordered
                or baseline_fill + baseline_remaining != baseline_ordered
            ):
                raise ValueValidationError("partial-fill script ratio is invalid")
            fill_value = max(
                self._decision.order.quantity.increment,
                floor_to_increment(
                    self._decision.order.quantity.value * baseline_fill / baseline_ordered,
                    self._decision.order.quantity.increment,
                ),
            )
            expected_quantity = Quantity(
                fill_value,
                self._decision.order.quantity.increment,
            )
            expected_price = Price(
                self._script.fill_price,
                self._decision.order.limit_price.tick,
            )
            expected_fee = Money(
                parse_decimal(self._script.fee_per_contract, allow_zero=False)
                * expected_quantity.value
            )
        elif event.event_type is ExecutionEventType.CANCELED:
            expected_sequence = self._script.canceled_sequence
            expected_time = self._script.canceled_at
        else:
            raise ValueValidationError(
                "partial-fill script accepts only OPENED, FILL, or CANCELED events"
            )
        if (
            event.sequence != expected_sequence
            or event.occurred_at != expected_time
            or event.quantity != expected_quantity
            or event.price != expected_price
            or event.fee != expected_fee
        ):
            raise ValueValidationError(
                "partial-fill event does not match the bound execution script"
            )
        validate_execution_event_identity(event, self._script.script_id)
        applied = apply_partial_fill_canceled_event(
            self._decision.order,
            self._decision.reservation,
            self._position,
            event,
            instrument_id=proposal.instrument_id,
            rule_version_id=proposal.rule_version_id,
            maximum_loss_per_unit=proposal.maximum_loss_per_unit,
            fee_per_unit=proposal.fee_per_unit,
        )

        if event.event_type is ExecutionEventType.FILL:
            if event.price is None:
                raise ValueValidationError("partial fill is missing its exact price")
            purchase_amount = Money(event.quantity.value * event.price.value)
            purchase_journal = JournalFactory.purchase(
                event.event_id,
                purchase_amount,
                event.occurred_at,
            )
            fee_journal = JournalFactory.fee(
                event.event_id,
                event.fee,
                event.occurred_at,
            )
            self._ledger.post_many((purchase_journal, fee_journal))

        self._event_identities[event.event_id] = event
        self._events.append(event)
        self._events.sort(key=lambda item: (item.sequence, item.event_id))
        self._reservation_lifecycle.append(applied.reservation)
        self._position = applied.position
        self._decision = DecisionArtifacts(
            campaign=self._decision.campaign,
            information_set=self._decision.information_set,
            strategy_evaluation=self._decision.strategy_evaluation,
            allocation=self._decision.allocation,
            risk_decision=self._decision.risk_decision,
            reservation=applied.reservation,
            order=applied.order,
        )
        return PartialExecutionCheckpoint(
            self._decision,
            tuple(self._events),
            tuple(self._reservation_lifecycle),
            self._position,
            self._ledger.metrics(),
        )

    def apply_unfilled_execution(self, event: ExecutionEvent) -> OrderCheckpoint:
        if (
            self._decision is None
            or not isinstance(self._decision.order, ExpiringLimitOrder)
            or not isinstance(self._script, UnfilledExpiryExecutionScript)
            or self._position is not None
        ):
            raise RuntimeError("unfilled order authorization must exist before execution")
        existing = self._event_identities.get(event.event_id)
        if existing is not None:
            if existing != event:
                raise SimulationConflictError("execution event identity has conflicting content")
            return OrderCheckpoint(
                self._decision,
                tuple(self._events),
                tuple(self._reservation_lifecycle),
                self._ledger.metrics(),
            )
        if event.event_type is ExecutionEventType.OPENED:
            if (
                event.sequence != self._script.opened_sequence
                or event.occurred_at != self._script.opened_at
            ):
                raise ValueValidationError("OPENED event does not match the bound execution script")
        elif event.event_type is ExecutionEventType.EXPIRED:
            if (
                event.sequence != self._script.expired_sequence
                or event.occurred_at != self._script.expires_at
            ):
                raise ValueValidationError(
                    "EXPIRED event does not match the bound execution script"
                )
        else:
            raise ValueValidationError(
                "unfilled execution script accepts only OPENED or EXPIRED events"
            )
        validate_execution_event_identity(event, self._script.script_id)
        applied = apply_unfilled_order_event(
            self._decision.order,
            self._decision.reservation,
            event,
        )
        self._event_identities[event.event_id] = event
        self._events.append(event)
        self._events.sort(key=lambda item: (item.sequence, item.event_id))
        self._reservation_lifecycle.append(applied.reservation)
        self._decision = DecisionArtifacts(
            campaign=self._decision.campaign,
            information_set=self._decision.information_set,
            strategy_evaluation=self._decision.strategy_evaluation,
            allocation=self._decision.allocation,
            risk_decision=self._decision.risk_decision,
            reservation=applied.reservation,
            order=applied.order,
        )
        return OrderCheckpoint(
            self._decision,
            tuple(self._events),
            tuple(self._reservation_lifecycle),
            self._ledger.metrics(),
        )

    def apply_execution(self, event: ExecutionEvent) -> ExecutionCheckpoint:
        if self._decision is None or self._position is None:
            raise RuntimeError("decision and reservation must exist before execution")
        existing = self._event_identities.get(event.event_id)
        if existing is not None:
            if existing != event:
                raise SimulationConflictError("execution event identity has conflicting content")
            return ExecutionCheckpoint(
                self._decision,
                tuple(self._events),
                self._position,
                self._ledger.metrics(),
            )
        applied = apply_execution_event(
            self._decision.order,
            self._decision.reservation,
            self._position,
            event,
        )
        if event.event_type is ExecutionEventType.FILL:
            if event.price is None:
                raise ValueValidationError("fill event is missing its price")
            purchase = Money(event.quantity.value * event.price.value)
            self._ledger.post(JournalFactory.purchase(event.event_id, purchase, event.occurred_at))
            self._ledger.post(JournalFactory.fee(event.event_id, event.fee, event.occurred_at))
        self._event_identities[event.event_id] = event
        self._events.append(event)
        self._events.sort(key=lambda item: (item.sequence, item.event_id))
        self._position = applied.position
        self._decision = DecisionArtifacts(
            campaign=self._decision.campaign,
            information_set=self._decision.information_set,
            strategy_evaluation=self._decision.strategy_evaluation,
            allocation=self._decision.allocation,
            risk_decision=self._decision.risk_decision,
            reservation=applied.reservation,
            order=applied.order,
        )
        return ExecutionCheckpoint(
            self._decision,
            tuple(self._events),
            self._position,
            self._ledger.metrics(),
        )

    def apply_settlement(self, settlement: SettlementInputs) -> SettlementResult:
        if isinstance(self._script, PartialFillCanceledExecutionScript):
            return self._apply_partial_canceled_settlement(settlement)
        if self._decision is None or self._position is None:
            raise RuntimeError("execution must finish before settlement is loaded")
        if settlement.fixture_id != self._inputs.fixture_id:
            raise ValueValidationError("settlement fixture identifier mismatch")
        if isinstance(self._inputs.rule, VoidContractRuleVersion):
            raise ValueValidationError("VOID contract rules require their classified fixture path")
        if type(settlement.fact) is not SettlementFact:
            raise ValueValidationError(
                "classified or rule-bound settlement facts require their explicit fixture path"
            )
        identity = settlement.fact.source_id
        existing = self._settlement_identities.get(identity)
        if existing is not None:
            existing_input, existing_application = existing
            if existing_input != settlement:
                raise SimulationConflictError("settlement identity has conflicting content")
            return existing_application.result
        prior_cost = self._position.cost_basis
        applied = settle_position(
            self._position,
            self._decision.reservation,
            self._inputs.instrument,
            self._inputs.rule,
            settlement.fact,
        )
        self._ledger.post(
            JournalFactory.settlement(
                settlement.fact.source_id,
                applied.result.settlement_cash,
                prior_cost,
                settlement.fact.available_at,
            )
        )
        self._settlement_identities[identity] = (settlement, applied)
        self._settlement = applied.result
        self._position = applied.position
        self._decision = DecisionArtifacts(
            campaign=self._decision.campaign,
            information_set=self._decision.information_set,
            strategy_evaluation=self._decision.strategy_evaluation,
            allocation=self._decision.allocation,
            risk_decision=self._decision.risk_decision,
            reservation=applied.reservation,
            order=replace(self._decision.order, reserved_maximum_loss=Money(0)),
        )
        return applied.result

    def _apply_partial_canceled_settlement(
        self,
        settlement: SettlementInputs,
    ) -> SettlementResult:
        if self._inputs.fixture_id not in _PARTIAL_FILL_CANCELED_SETTLED_FIXTURE_IDS:
            raise RuntimeError("a canceled partial-fill run cannot load settlement")
        existing = self._partial_settlement_application
        if existing is not None:
            existing_input, existing_application = existing
            if existing_input != settlement:
                raise SimulationConflictError("settlement identity has conflicting content")
            return existing_application.result
        if (
            self._decision is None
            or not isinstance(self._decision.order, ExpiringLimitOrder)
            or not isinstance(self._position, FilledPositionState)
            or self._pre_settlement_position != self._position
            or self._pre_settlement_metrics is None
            or self._partial_risk_capacities is None
            or len(self._events) != 3
            or len(self._reservation_lifecycle) != 4
        ):
            raise RuntimeError("partial-fill cancellation must complete before settlement")
        if settlement.fixture_id != self._inputs.fixture_id:
            raise ValueValidationError("settlement fixture identifier mismatch")
        if self._inputs.fixture_id == _PARTIAL_FILL_CANCELED_SETTLED_NO_FIXTURE_ID:
            if not isinstance(settlement.fact, RuleBoundSettlementFact):
                raise ValueValidationError(
                    "partial canceled NO settlement requires exact rule provenance"
                )
        elif not isinstance(settlement.fact, ClassifiedRuleBoundSettlementFact):
            raise ValueValidationError(
                "partial canceled VOID settlement requires classified rule provenance"
            )
        if settlement.fact.fixture_id != settlement.fixture_id:
            raise ValueValidationError("settlement fact fixture identifier mismatch")
        if settlement.fact.source_id != _PARTIAL_SETTLEMENT_SOURCE_IDS[self._inputs.fixture_id]:
            raise ValueValidationError("settlement source identity mismatch")
        proposal = self._decision.strategy_evaluation.proposal
        if not isinstance(proposal, RestingLimitProposal):
            raise RuntimeError("partial canceled settlement is missing its exact proposal")
        if self._settlement is not None:
            raise SimulationConflictError("position already has a different settlement")
        if (
            settlement.fact.source_event_at > self._metadata.logical_time
            or settlement.fact.available_at > self._metadata.logical_time
        ):
            raise ValueValidationError("settlement occurs after the terminal logical time")

        applied: AppliedPartialPositionSettlement | AppliedClassifiedPartialPositionSettlement
        if isinstance(settlement.fact, ClassifiedRuleBoundSettlementFact):
            if not isinstance(self._inputs.rule, VoidContractRuleVersion):
                raise ValueValidationError("VOID settlement requires an exact rule-bound VOID term")
            applied = settle_canceled_filled_void_position(
                self._position,
                self._decision.reservation,
                self._decision.order,
                self._inputs.instrument,
                self._inputs.rule,
                settlement.fact,
                execution_completed_at=self._events[-1].occurred_at,
            )
        elif isinstance(settlement.fact, RuleBoundSettlementFact):
            applied = settle_canceled_filled_position(
                self._position,
                self._decision.reservation,
                self._decision.order,
                self._inputs.instrument,
                self._inputs.rule,
                settlement.fact,
                execution_completed_at=self._events[-1].occurred_at,
            )
        else:
            raise RuntimeError("settled partial-fill fact type was not validated")
        settlement_journal = JournalFactory.settlement(
            settlement.fact.source_id,
            applied.result.settlement_cash,
            self._position.cost_basis,
            settlement.fact.available_at,
        )
        staged_ledger = Ledger()
        for journal in self._ledger.journals:
            staged_ledger.post(journal)
        staged_ledger.post(settlement_journal)
        final_metrics = staged_ledger.metrics()
        capacity_before, capacity_open, capacity_after_fill, capacity_after_cancel = (
            self._partial_risk_capacities
        )
        initial, opened, after_fill, released = self._reservation_lifecycle
        opened_event, fill_event, canceled_event = self._events
        final_capacity = Money(
            CampaignSnapshot(
                campaign=self._decision.campaign,
                ledger_equity=final_metrics.ledger_equity,
                settled_cash=final_metrics.cash,
                active_reservations=Money(0),
            ).risk_capacity
        )
        void_candidate: PartialFillCanceledSettledVoidResult | None = None
        no_candidate: PartialFillCanceledSettledNoResult | None = None
        if isinstance(applied, AppliedClassifiedPartialPositionSettlement) and isinstance(
            settlement.fact,
            ClassifiedRuleBoundSettlementFact,
        ):
            if not isinstance(self._inputs.rule, VoidContractRuleVersion):
                raise RuntimeError("classified settlement lost its exact VOID rule term")
            void_candidate = PartialFillCanceledSettledVoidResult(
                run_id=self._run_id(),
                fixture_id=self._inputs.fixture_id,
                fixture_manifest_hash=self._metadata.fixture_manifest_hash,
                engine_version=self._engine_version,
                seed=self._seed,
                logical_time=self._metadata.logical_time,
                campaign=self._decision.campaign,
                information_set=self._decision.information_set,
                strategy_evaluation=self._decision.strategy_evaluation,
                proposal=proposal,
                allocation=self._decision.allocation,
                risk_decision=self._decision.risk_decision,
                reservation_lifecycle=(initial, opened, after_fill, released),
                order=self._decision.order,
                execution_events=(opened_event, fill_event, canceled_event),
                pre_settlement_position=self._position,
                contract_rule=self._inputs.rule,
                settlement_file_hash=settlement.settlement_file_hash,
                settlement_fact=settlement.fact,
                settlement=applied.result,
                settled_position=applied.position,
                risk_capacity_before_order=capacity_before,
                risk_capacity_while_fully_reserved=capacity_open,
                risk_capacity_after_partial_fill=capacity_after_fill,
                risk_capacity_after_cancellation=capacity_after_cancel,
                risk_capacity_after_settlement=final_capacity,
                pre_settlement_metrics=self._pre_settlement_metrics,
                journals=staged_ledger.journals,
                metrics=final_metrics,
            )
        elif isinstance(applied, AppliedPartialPositionSettlement) and isinstance(
            settlement.fact,
            RuleBoundSettlementFact,
        ):
            no_candidate = PartialFillCanceledSettledNoResult(
                run_id=self._run_id(),
                fixture_id=self._inputs.fixture_id,
                fixture_manifest_hash=self._metadata.fixture_manifest_hash,
                engine_version=self._engine_version,
                seed=self._seed,
                logical_time=self._metadata.logical_time,
                campaign=self._decision.campaign,
                information_set=self._decision.information_set,
                strategy_evaluation=self._decision.strategy_evaluation,
                proposal=proposal,
                allocation=self._decision.allocation,
                risk_decision=self._decision.risk_decision,
                reservation_lifecycle=(initial, opened, after_fill, released),
                order=self._decision.order,
                execution_events=(opened_event, fill_event, canceled_event),
                pre_settlement_position=self._position,
                contract_rule=self._inputs.rule,
                settlement_file_hash=settlement.settlement_file_hash,
                settlement_fact=settlement.fact,
                settlement=applied.result,
                settled_position=applied.position,
                risk_capacity_before_order=capacity_before,
                risk_capacity_while_fully_reserved=capacity_open,
                risk_capacity_after_partial_fill=capacity_after_fill,
                risk_capacity_after_cancellation=capacity_after_cancel,
                risk_capacity_after_settlement=final_capacity,
                pre_settlement_metrics=self._pre_settlement_metrics,
                journals=staged_ledger.journals,
                metrics=final_metrics,
            )
        else:
            raise RuntimeError("settled partial-fill application type is inconsistent")

        self._ledger.post(settlement_journal)
        self._partial_settlement_application = (settlement, applied)
        self._settlement = applied.result
        self._position = applied.position
        if void_candidate is not None:
            self._partial_fill_canceled_settled_void_result = void_candidate
        elif no_candidate is not None:
            self._partial_fill_canceled_settled_no_result = no_candidate
        else:
            raise RuntimeError("settled partial-fill result cache is missing")
        return applied.result

    def finalize(self) -> RunResult:
        if self._no_proposal_result is not None:
            return self._no_proposal_result
        if self._policy_rejected_result is not None:
            return self._policy_rejected_result
        if self._invariant_rejected_result is not None:
            return self._invariant_rejected_result
        if self._unfilled_expired_result is not None:
            return self._unfilled_expired_result
        if self._partial_fill_canceled_result is not None:
            return self._partial_fill_canceled_result
        if self._partial_fill_canceled_settled_no_result is not None:
            return self._partial_fill_canceled_settled_no_result
        if self._partial_fill_canceled_settled_void_result is not None:
            return self._partial_fill_canceled_settled_void_result
        if isinstance(self._script, PartialFillCanceledExecutionScript):
            if self._inputs.fixture_id == _PARTIAL_FILL_CANCELED_SETTLED_NO_FIXTURE_ID:
                raise RuntimeError(
                    "partial-fill cancellation cannot finalize before exact NO settlement"
                )
            if self._inputs.fixture_id == _PARTIAL_FILL_CANCELED_SETTLED_VOID_FIXTURE_ID:
                raise RuntimeError(
                    "partial-fill cancellation cannot finalize before exact VOID settlement"
                )
            raise RuntimeError("partial-fill cancellation cannot finalize while incomplete")
        if self._decision is None or self._position is None or self._settlement is None:
            raise RuntimeError("simulation cannot finalize before execution and settlement")
        metrics = self._ledger.metrics()
        return SimulationResult(
            run_id=self._run_id(),
            fixture_id=self._inputs.fixture_id,
            fixture_manifest_hash=self._metadata.fixture_manifest_hash,
            engine_version=self._engine_version,
            seed=self._seed,
            logical_time=self._metadata.logical_time,
            campaign=self._decision.campaign,
            information_set=self._decision.information_set,
            strategy_evaluation=self._decision.strategy_evaluation,
            allocation=self._decision.allocation,
            risk_decision=self._decision.risk_decision,
            reservation=self._decision.reservation,
            order=self._decision.order,
            execution_events=tuple(self._events),
            position=self._position,
            settlement=self._settlement,
            journals=self._ledger.journals,
            metrics=metrics,
        )


def run_simulation(
    *,
    decision_inputs: DecisionInputs,
    execution_script: ExecutionScript,
    run_metadata: RunMetadata,
    settlement_loader: Callable[[], SettlementInputs],
    capital: Money,
    seed: int,
    engine_version: str,
    simulator: SyntheticExecutionEmitter,
) -> RunResult:
    """Finalize terminal outcomes; load settlement only for the settled-trade path."""

    simulation = GoldenSimulation(
        decision_inputs=decision_inputs,
        execution_script=execution_script,
        run_metadata=run_metadata,
        capital=capital,
        seed=seed,
        engine_version=engine_version,
        simulator=simulator,
    )
    decision_result = simulation.run_decision_and_execution()
    if isinstance(
        decision_result,
        (
            NoProposalResult,
            PolicyRejectedResult,
            InvariantRejectedResult,
            UnfilledExpiredResult,
            PartialFillCanceledResult,
            PartialFillCanceledSettledNoResult,
            PartialFillCanceledSettledVoidResult,
        ),
    ):
        return decision_result
    simulation.apply_settlement(settlement_loader())
    return simulation.finalize()
