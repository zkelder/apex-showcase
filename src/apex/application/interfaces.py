"""Typed application inputs and immutable golden-run output."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.campaign import (
    Campaign,
    CampaignPolicy,
    CampaignSnapshot,
    active_campaign_risk,
)
from apex.domain.decisions import (
    INVARIANT_MARKET_DATA_STALE,
    POLICY_MAX_CAPITAL_LOCKUP_EXCEEDED,
    AllocationDecision,
    OpportunityProposal,
    RestingLimitProposal,
    RiskDecision,
    StaleMarketRiskDecision,
    StrategyEvaluation,
    StrategyOutcome,
)
from apex.domain.execution import (
    ClassifiedPartialPositionSettlementResult,
    ClassifiedRuleBoundSettlementFact,
    ClassifiedSettledFilledPositionState,
    ExecutionEvent,
    ExecutionEventType,
    ExpiringLimitOrder,
    FilledPositionState,
    Order,
    OrderState,
    PartialPositionSettlementResult,
    PositionState,
    ReservationState,
    RiskReservation,
    RuleBoundSettlementFact,
    SettledFilledPositionState,
    SettlementFact,
    SettlementOutcome,
    SettlementResult,
    classified_rule_bound_position_settlement_id,
    rule_bound_position_settlement_id,
)
from apex.domain.ledger import (
    AccountCode,
    Journal,
    JournalFactory,
    JournalType,
    Ledger,
    LedgerMetrics,
)
from apex.domain.market import (
    ContractRuleVersion,
    InformationSet,
    Instrument,
    MarketSnapshot,
    SourceRecord,
    VoidContractRuleVersion,
)
from apex.domain.values import (
    ContentHash,
    InformationCutoff,
    Money,
    Price,
    ProbabilityVector,
)


@dataclass(frozen=True, slots=True)
class ReferenceEstimateInput:
    estimate_id: str
    source_id: str
    probability_vector: ProbabilityVector
    published_at: datetime
    expires_at: datetime
    method_version: str


@dataclass(frozen=True, slots=True)
class SyntheticExecutionScript:
    script_id: str
    kind: str
    opened_at: datetime
    filled_at: datetime
    opened_sequence: int
    fill_sequence: int
    fill_price: str
    fee_per_contract: str


@dataclass(frozen=True, slots=True)
class UnfilledExpiryExecutionScript:
    script_id: str
    kind: str
    side: str
    executable_yes_ask: str
    executable_no_ask: str
    visible_yes_quantity: str
    opened_at: datetime
    expires_at: datetime
    opened_sequence: int
    expired_sequence: int


@dataclass(frozen=True, slots=True)
class PartialFillCanceledExecutionScript:
    script_id: str
    kind: str
    side: str
    executable_yes_ask: str
    executable_no_ask: str
    visible_yes_quantity: str
    ordered_quantity: str
    fill_quantity: str
    remaining_quantity: str
    opened_at: datetime
    filled_at: datetime
    canceled_at: datetime
    opened_sequence: int
    fill_sequence: int
    canceled_sequence: int
    fill_price: str
    fee_per_contract: str


type ExecutionScript = (
    SyntheticExecutionScript | UnfilledExpiryExecutionScript | PartialFillCanceledExecutionScript
)


@dataclass(frozen=True, slots=True)
class DecisionInputs:
    fixture_id: str
    schema_version: str
    decision_data_hash: ContentHash
    cutoff: InformationCutoff
    campaign_id: str
    authorized_loss_fraction: str
    policy: CampaignPolicy
    instrument: Instrument
    rule: ContractRuleVersion
    source_records: tuple[SourceRecord, ...]
    market: MarketSnapshot
    reference_estimate: ReferenceEstimateInput
    strategy_version: str
    fee_model_version: str
    strategy_decision_at: datetime
    fee_per_contract: str
    resting_limit_price: Price | None
    order_expires_at: datetime | None
    risk_authorization_at: datetime
    market_observation_source_id: str


@dataclass(frozen=True, slots=True)
class RunMetadata:
    fixture_manifest_hash: ContentHash
    decision_file_hash: ContentHash
    logical_time: datetime


@dataclass(frozen=True, slots=True)
class SettlementInputs:
    fixture_id: str
    settlement_file_hash: ContentHash
    fact: SettlementFact


@dataclass(frozen=True, slots=True)
class DecisionArtifacts:
    campaign: Campaign
    information_set: InformationSet
    strategy_evaluation: StrategyEvaluation
    allocation: AllocationDecision
    risk_decision: RiskDecision
    reservation: RiskReservation
    order: Order


class RunOutcome(StrEnum):
    NO_PROPOSAL = "NO_PROPOSAL"
    POLICY_REJECTED = "POLICY_REJECTED"
    INVARIANT_REJECTED = "INVARIANT_REJECTED"
    UNFILLED_EXPIRED = "UNFILLED_EXPIRED"
    PARTIAL_FILL_CANCELED = "PARTIAL_FILL_CANCELED"
    PARTIAL_FILL_CANCELED_SETTLED_NO = "PARTIAL_FILL_CANCELED_SETTLED_NO"
    PARTIAL_FILL_CANCELED_SETTLED_VOID = "PARTIAL_FILL_CANCELED_SETTLED_VOID"
    SETTLED_TRADE = "SETTLED_TRADE"


@dataclass(frozen=True, slots=True)
class SimulationResult:
    run_id: str
    fixture_id: str
    fixture_manifest_hash: ContentHash
    engine_version: str
    seed: int
    logical_time: datetime
    campaign: Campaign
    information_set: InformationSet
    strategy_evaluation: StrategyEvaluation
    allocation: AllocationDecision
    risk_decision: RiskDecision
    reservation: RiskReservation
    order: Order
    execution_events: tuple[ExecutionEvent, ...]
    position: PositionState
    settlement: SettlementResult
    journals: tuple[Journal, ...]
    metrics: LedgerMetrics
    run_outcome: RunOutcome = field(default=RunOutcome.SETTLED_TRADE, init=False)
    runtime_capability: str = "SIMULATION_ONLY"
    execution_evidence: str = "SYNTHETIC"
    edge_evidence: str = "NONE"
    calibration_evidence: str = "NONE"


@dataclass(frozen=True, slots=True)
class NoProposalResult:
    run_id: str
    fixture_id: str
    fixture_manifest_hash: ContentHash
    engine_version: str
    seed: int
    logical_time: datetime
    campaign: Campaign
    information_set: InformationSet
    strategy_evaluation: StrategyEvaluation
    journals: tuple[Journal, ...]
    metrics: LedgerMetrics
    run_outcome: RunOutcome = field(default=RunOutcome.NO_PROPOSAL, init=False)
    runtime_capability: str = "SIMULATION_ONLY"
    execution_evidence: str = "SYNTHETIC"
    edge_evidence: str = "NONE"
    calibration_evidence: str = "NONE"


@dataclass(frozen=True, slots=True)
class PolicyRejectedResult:
    run_id: str
    fixture_id: str
    fixture_manifest_hash: ContentHash
    engine_version: str
    seed: int
    logical_time: datetime
    campaign: Campaign
    information_set: InformationSet
    strategy_evaluation: StrategyEvaluation
    proposal: OpportunityProposal
    allocation: AllocationDecision
    policy_reason_codes: tuple[str, ...]
    policy_explanation: str
    journals: tuple[Journal, ...]
    metrics: LedgerMetrics
    run_outcome: RunOutcome = field(default=RunOutcome.POLICY_REJECTED, init=False)
    runtime_capability: str = "SIMULATION_ONLY"
    execution_evidence: str = "SYNTHETIC"
    edge_evidence: str = "NONE"
    calibration_evidence: str = "NONE"

    def __post_init__(self) -> None:
        if self.strategy_evaluation.outcome is not StrategyOutcome.PROPOSAL:
            raise ValueError("policy rejection requires a proposal evaluation")
        if self.strategy_evaluation.proposal != self.proposal:
            raise ValueError("policy-rejected proposal must match its evaluation")
        if self.allocation.proposal_id != self.proposal.proposal_id:
            raise ValueError("policy-rejected allocation must reference its proposal")
        if self.allocation.accepted:
            raise ValueError("policy-rejected allocation cannot be accepted")
        if self.allocation.quantity.value != 0 or self.allocation.maximum_loss.amount != 0:
            raise ValueError("policy-rejected allocation must carry zero exposure")
        expected_codes = (POLICY_MAX_CAPITAL_LOCKUP_EXCEEDED,)
        if self.allocation.reason_codes != expected_codes:
            raise ValueError("allocation does not contain the recognized policy rejection")
        if self.policy_reason_codes != self.allocation.reason_codes:
            raise ValueError("result policy reasons must match allocation reasons")
        if not self.policy_explanation:
            raise ValueError("policy rejection requires a human-readable explanation")


@dataclass(frozen=True, slots=True)
class InvariantRejectedResult:
    run_id: str
    fixture_id: str
    fixture_manifest_hash: ContentHash
    engine_version: str
    seed: int
    logical_time: datetime
    campaign: Campaign
    information_set: InformationSet
    strategy_evaluation: StrategyEvaluation
    proposal: OpportunityProposal
    allocation: AllocationDecision
    risk_decision: StaleMarketRiskDecision
    invariant_reason_codes: tuple[str, ...]
    invariant_explanation: str
    journals: tuple[Journal, ...]
    metrics: LedgerMetrics
    run_outcome: RunOutcome = field(default=RunOutcome.INVARIANT_REJECTED, init=False)
    runtime_capability: str = "SIMULATION_ONLY"
    execution_evidence: str = "SYNTHETIC"
    edge_evidence: str = "NONE"
    calibration_evidence: str = "NONE"

    def __post_init__(self) -> None:
        if self.strategy_evaluation.outcome is not StrategyOutcome.PROPOSAL:
            raise ValueError("invariant rejection requires a proposal evaluation")
        if self.strategy_evaluation.proposal != self.proposal:
            raise ValueError("invariant-rejected proposal must match its evaluation")
        if self.allocation.proposal_id != self.proposal.proposal_id:
            raise ValueError("invariant-rejected allocation must reference its proposal")
        if not self.allocation.accepted:
            raise ValueError("invariant rejection requires an accepted allocation")
        if self.allocation.quantity.value <= 0 or self.allocation.maximum_loss.amount <= 0:
            raise ValueError("invariant-rejected allocation must retain positive exposure")
        if self.risk_decision.accepted:
            raise ValueError("invariant-rejected risk decision cannot be accepted")
        if self.risk_decision.allocation_id != self.allocation.allocation_id:
            raise ValueError("risk decision must reference the retained allocation")
        expected_codes = (INVARIANT_MARKET_DATA_STALE,)
        if self.risk_decision.reason_codes != expected_codes:
            raise ValueError("risk decision does not contain the recognized invariant rejection")
        if self.invariant_reason_codes != self.risk_decision.reason_codes:
            raise ValueError("result invariant reasons must match risk-decision reasons")
        if self.risk_decision.information_set_id != self.information_set.information_set_id:
            raise ValueError("freshness evidence references a different information set")
        if self.risk_decision.information_set_hash != self.information_set.content_hash:
            raise ValueError("freshness evidence contains a different information-set hash")
        matching_observations = tuple(
            member
            for member in self.information_set.ordered_members
            if member.source_id == self.risk_decision.observation_source_id
        )
        if (
            len(matching_observations) != 1
            or matching_observations[0].available_at != self.risk_decision.observation_available_at
        ):
            raise ValueError("freshness evidence does not match an exact information-set member")
        if self.invariant_explanation != self.risk_decision.invariant_explanation:
            raise ValueError("result invariant explanation must match risk-decision evidence")
        if not self.invariant_explanation:
            raise ValueError("invariant rejection requires a human-readable explanation")
        if (
            len(self.journals) != 1
            or self.journals[0].journal_type is not JournalType.CONTRIBUTION
            or not self.journals[0].balanced
        ):
            raise ValueError("invariant rejection requires one balanced contribution journal")
        if (
            self.metrics.contributions != self.campaign.contribution
            or self.metrics.cash != self.campaign.contribution
            or self.metrics.ledger_equity != self.campaign.contribution
            or self.metrics.position_cost.amount != Decimal(0)
            or self.metrics.realized_trading_pnl_before_fees != Decimal(0)
            or self.metrics.fees.amount != Decimal(0)
            or self.metrics.net_trading_pnl != Decimal(0)
        ):
            raise ValueError("invariant rejection cannot contain trading financial state")


@dataclass(frozen=True, slots=True)
class UnfilledExpiredResult:
    run_id: str
    fixture_id: str
    fixture_manifest_hash: ContentHash
    engine_version: str
    seed: int
    logical_time: datetime
    campaign: Campaign
    information_set: InformationSet
    strategy_evaluation: StrategyEvaluation
    proposal: RestingLimitProposal
    allocation: AllocationDecision
    risk_decision: RiskDecision
    reservation_lifecycle: tuple[RiskReservation, RiskReservation, RiskReservation]
    order: ExpiringLimitOrder
    execution_events: tuple[ExecutionEvent, ExecutionEvent]
    risk_capacity_before_order: Money
    risk_capacity_while_open: Money
    risk_capacity_after_expiration: Money
    journals: tuple[Journal, ...]
    metrics: LedgerMetrics
    run_outcome: RunOutcome = field(default=RunOutcome.UNFILLED_EXPIRED, init=False)
    runtime_capability: str = "SIMULATION_ONLY"
    execution_evidence: str = "SYNTHETIC"
    edge_evidence: str = "NONE"
    calibration_evidence: str = "NONE"

    @property
    def initial_reservation(self) -> RiskReservation:
        return self.reservation_lifecycle[0]

    @property
    def final_reservation(self) -> RiskReservation:
        return self.reservation_lifecycle[2]

    @property
    def released_reservation_amount(self) -> Money:
        return Money(
            self.initial_reservation.maximum_loss.amount
            - self.final_reservation.maximum_loss.amount
        )

    def __post_init__(self) -> None:
        if self.strategy_evaluation.outcome is not StrategyOutcome.PROPOSAL:
            raise ValueError("unfilled expiration requires a proposal evaluation")
        if self.strategy_evaluation.proposal != self.proposal:
            raise ValueError("unfilled-expired proposal must match its evaluation")
        if self.allocation.proposal_id != self.proposal.proposal_id:
            raise ValueError("unfilled-expired allocation must reference its proposal")
        if not self.allocation.accepted or self.allocation.quantity.value <= 0:
            raise ValueError("unfilled expiration requires an accepted positive allocation")
        if not self.risk_decision.accepted:
            raise ValueError("unfilled expiration requires an accepted risk decision")
        if self.risk_decision.allocation_id != self.allocation.allocation_id:
            raise ValueError("risk decision must reference the retained allocation")
        if self.risk_decision.reason_codes != ("ALL_INVARIANTS_PASSED",):
            raise ValueError("unfilled-expired risk decision must pass every invariant")
        initial, opened, released = self.reservation_lifecycle
        if tuple(item.state for item in self.reservation_lifecycle) != (
            ReservationState.HELD,
            ReservationState.OPEN,
            ReservationState.RELEASED,
        ):
            raise ValueError("reservation lifecycle must be HELD, OPEN, RELEASED")
        reservation_identity = (
            initial.reservation_id,
            initial.campaign_id,
            initial.risk_decision_id,
            initial.order_id,
            initial.exact_order_fingerprint,
        )
        if any(
            (
                item.reservation_id,
                item.campaign_id,
                item.risk_decision_id,
                item.order_id,
                item.exact_order_fingerprint,
            )
            != reservation_identity
            for item in (opened, released)
        ):
            raise ValueError("reservation lifecycle identities must remain immutable")
        if (
            initial.maximum_loss != self.allocation.maximum_loss
            or opened.maximum_loss != initial.maximum_loss
            or released.maximum_loss.amount != Decimal(0)
        ):
            raise ValueError("reservation lifecycle amounts are inconsistent")
        if self.order.order_id != initial.order_id:
            raise ValueError("reservation lifecycle belongs to a different order")
        if (
            initial.campaign_id != self.campaign.campaign_id
            or initial.risk_decision_id != self.risk_decision.risk_decision_id
            or initial.exact_order_fingerprint != self.risk_decision.exact_order_fingerprint
            or self.order.risk_decision_id != self.risk_decision.risk_decision_id
            or self.order.proposal_id != self.proposal.proposal_id
            or self.order.exact_order_fingerprint != self.risk_decision.exact_order_fingerprint
        ):
            raise ValueError("order and reservation must match the accepted authority")
        if self.order.state is not OrderState.EXPIRED:
            raise ValueError("unfilled-expired order must be EXPIRED")
        if self.order.quantity != self.allocation.quantity:
            raise ValueError("unfilled-expired order quantity must match its allocation")
        if (
            self.order.filled_quantity.value != 0
            or self.order.remaining_quantity != self.order.quantity
            or self.order.reserved_maximum_loss.amount != 0
        ):
            raise ValueError("unfilled-expired order cannot retain fill or reservation state")
        if (
            self.order.limit_price != self.proposal.limit_price
            or self.order.executable_ask != self.proposal.executable_ask
            or self.order.expires_at != self.proposal.order_expires_at
        ):
            raise ValueError("order terms must match the resting-limit proposal")
        expected_maximum_loss = (
            self.proposal.maximum_loss_per_unit.amount * self.order.quantity.value
        )
        if self.allocation.maximum_loss.amount != expected_maximum_loss:
            raise ValueError("reservation does not equal exact order maximum loss")
        opened_event, expired_event = self.execution_events
        if (
            opened_event.event_type is not ExecutionEventType.OPENED
            or expired_event.event_type is not ExecutionEventType.EXPIRED
            or opened_event.sequence != 1
            or expired_event.sequence != opened_event.sequence + 1
            or opened_event.order_id != self.order.order_id
            or expired_event.order_id != self.order.order_id
            or opened_event.occurred_at >= expired_event.occurred_at
            or expired_event.occurred_at != self.order.expires_at
            or self.order.last_execution_sequence != expired_event.sequence
        ):
            raise ValueError("execution lifecycle must be contiguous OPENED then EXPIRED")
        if any(
            event.quantity.value != 0 or event.price is not None or event.fee.amount != 0
            for event in self.execution_events
        ):
            raise ValueError("unfilled-expired events cannot contain fill economics")
        if (
            self.risk_capacity_while_open.amount
            != self.risk_capacity_before_order.amount - initial.maximum_loss.amount
            or self.risk_capacity_after_expiration != self.risk_capacity_before_order
        ):
            raise ValueError("reservation lifecycle does not restore campaign risk capacity")
        if self.logical_time != self.order.expires_at:
            raise ValueError("terminal logical time must equal order expiration")
        if (
            len(self.journals) != 1
            or self.journals[0].journal_type is not JournalType.CONTRIBUTION
            or not self.journals[0].balanced
        ):
            raise ValueError("unfilled expiration requires one balanced contribution journal")
        if (
            self.metrics.contributions != self.campaign.contribution
            or self.metrics.cash != self.campaign.contribution
            or self.metrics.ledger_equity != self.campaign.contribution
            or self.metrics.position_cost.amount != Decimal(0)
            or self.metrics.realized_trading_pnl_before_fees != Decimal(0)
            or self.metrics.fees.amount != Decimal(0)
            or self.metrics.net_trading_pnl != Decimal(0)
        ):
            raise ValueError("unfilled expiration cannot contain trading financial state")


@dataclass(frozen=True, slots=True)
class PartialFillCanceledResult:
    run_id: str
    fixture_id: str
    fixture_manifest_hash: ContentHash
    engine_version: str
    seed: int
    logical_time: datetime
    campaign: Campaign
    information_set: InformationSet
    strategy_evaluation: StrategyEvaluation
    proposal: RestingLimitProposal
    allocation: AllocationDecision
    risk_decision: RiskDecision
    reservation_lifecycle: tuple[
        RiskReservation,
        RiskReservation,
        RiskReservation,
        RiskReservation,
    ]
    order: ExpiringLimitOrder
    execution_events: tuple[ExecutionEvent, ExecutionEvent, ExecutionEvent]
    position: FilledPositionState
    risk_capacity_before_order: Money
    risk_capacity_while_fully_reserved: Money
    risk_capacity_after_partial_fill: Money
    risk_capacity_after_cancellation: Money
    journals: tuple[Journal, ...]
    metrics: LedgerMetrics
    run_outcome: RunOutcome = field(default=RunOutcome.PARTIAL_FILL_CANCELED, init=False)
    runtime_capability: str = "SIMULATION_ONLY"
    execution_evidence: str = "SYNTHETIC"
    edge_evidence: str = "NONE"
    calibration_evidence: str = "NONE"

    @property
    def initial_reservation(self) -> RiskReservation:
        return self.reservation_lifecycle[0]

    @property
    def reservation_after_open(self) -> RiskReservation:
        return self.reservation_lifecycle[1]

    @property
    def reservation_after_partial_fill(self) -> RiskReservation:
        return self.reservation_lifecycle[2]

    @property
    def final_reservation(self) -> RiskReservation:
        return self.reservation_lifecycle[3]

    @property
    def released_at_cancellation(self) -> Money:
        return Money(
            self.reservation_after_partial_fill.maximum_loss.amount
            - self.final_reservation.maximum_loss.amount
        )

    @property
    def active_risk_after_partial_fill(self) -> Money:
        return active_campaign_risk(
            self.position.maximum_remaining_loss,
            self.reservation_after_partial_fill.maximum_loss,
        )

    @property
    def final_active_campaign_risk(self) -> Money:
        return active_campaign_risk(
            self.position.maximum_remaining_loss,
            self.final_reservation.maximum_loss,
        )

    def __post_init__(self) -> None:
        if (
            self.strategy_evaluation.outcome is not StrategyOutcome.PROPOSAL
            or self.strategy_evaluation.proposal != self.proposal
        ):
            raise ValueError("partial-fill cancellation requires its proposal evaluation")
        if (
            not self.allocation.accepted
            or self.allocation.proposal_id != self.proposal.proposal_id
            or self.allocation.quantity.value <= 0
        ):
            raise ValueError("partial-fill cancellation requires an accepted allocation")
        if (
            not self.risk_decision.accepted
            or self.risk_decision.allocation_id != self.allocation.allocation_id
            or self.risk_decision.reason_codes != ("ALL_INVARIANTS_PASSED",)
        ):
            raise ValueError("partial-fill cancellation requires accepted risk authority")

        initial, opened, after_fill, released = self.reservation_lifecycle
        if tuple(item.state for item in self.reservation_lifecycle) != (
            ReservationState.HELD,
            ReservationState.OPEN,
            ReservationState.OPEN,
            ReservationState.RELEASED,
        ):
            raise ValueError("reservation lifecycle must be HELD, OPEN, OPEN, RELEASED")
        reservation_identity = (
            initial.reservation_id,
            initial.campaign_id,
            initial.risk_decision_id,
            initial.order_id,
            initial.exact_order_fingerprint,
        )
        if any(
            (
                item.reservation_id,
                item.campaign_id,
                item.risk_decision_id,
                item.order_id,
                item.exact_order_fingerprint,
            )
            != reservation_identity
            for item in (opened, after_fill, released)
        ):
            raise ValueError("partial-fill reservation identities must remain immutable")
        if (
            initial.campaign_id != self.campaign.campaign_id
            or initial.risk_decision_id != self.risk_decision.risk_decision_id
            or initial.exact_order_fingerprint != self.risk_decision.exact_order_fingerprint
            or initial.order_id != self.order.order_id
            or self.order.risk_decision_id != self.risk_decision.risk_decision_id
            or self.order.proposal_id != self.proposal.proposal_id
            or self.order.exact_order_fingerprint != self.risk_decision.exact_order_fingerprint
        ):
            raise ValueError("partial-fill order and reservation authority do not match")

        if (
            self.order.state is not OrderState.CANCELED
            or self.order.quantity != self.allocation.quantity
            or self.order.filled_quantity.value <= 0
            or self.order.filled_quantity.value >= self.order.quantity.value
            or self.order.remaining_quantity.value <= 0
            or self.order.reserved_maximum_loss.amount != 0
            or self.order.last_execution_sequence != 3
        ):
            raise ValueError("partial-fill canceled order state is inconsistent")
        if (
            self.order.limit_price != self.proposal.limit_price
            or self.order.executable_ask != self.proposal.executable_ask
            or self.order.expires_at != self.proposal.order_expires_at
        ):
            raise ValueError("partial-fill order terms do not match the proposal")

        maximum_loss_per_unit = self.proposal.maximum_loss_per_unit.amount
        expected_initial_risk = maximum_loss_per_unit * self.order.quantity.value
        expected_filled_risk = maximum_loss_per_unit * self.order.filled_quantity.value
        expected_unfilled_risk = maximum_loss_per_unit * self.order.remaining_quantity.value
        if (
            initial.maximum_loss != self.allocation.maximum_loss
            or initial.maximum_loss.amount != expected_initial_risk
            or opened.maximum_loss != initial.maximum_loss
            or after_fill.maximum_loss.amount != expected_unfilled_risk
            or released.maximum_loss.amount != 0
            or self.position.maximum_remaining_loss.amount != expected_filled_risk
            or self.active_risk_after_partial_fill != initial.maximum_loss
            or self.released_at_cancellation != after_fill.maximum_loss
            or self.final_active_campaign_risk != self.position.maximum_remaining_loss
        ):
            raise ValueError("partial-fill risk conversion has a gap or double count")

        opened_event, fill_event, canceled_event = self.execution_events
        if (
            tuple(event.event_type for event in self.execution_events)
            != (
                ExecutionEventType.OPENED,
                ExecutionEventType.FILL,
                ExecutionEventType.CANCELED,
            )
            or tuple(event.sequence for event in self.execution_events) != (1, 2, 3)
            or any(event.order_id != self.order.order_id for event in self.execution_events)
            or not opened_event.occurred_at
            < fill_event.occurred_at
            < canceled_event.occurred_at
            < self.order.expires_at
            or self.logical_time != canceled_event.occurred_at
            or len({event.event_id for event in self.execution_events}) != 3
        ):
            raise ValueError("partial-fill execution lifecycle is inconsistent")
        if (
            opened_event.quantity.value != 0
            or opened_event.price is not None
            or opened_event.fee.amount != 0
            or fill_event.quantity != self.order.filled_quantity
            or fill_event.price is None
            or fill_event.price.value > self.order.limit_price.value
            or fill_event.fee.amount
            != self.proposal.fee_per_unit.amount * fill_event.quantity.value
            or canceled_event.quantity.value != 0
            or canceled_event.price is not None
            or canceled_event.fee.amount != 0
        ):
            raise ValueError("partial-fill event economics are inconsistent")

        expected_position_cost = fill_event.quantity.value * fill_event.price.value
        if (
            self.position.instrument_id != self.proposal.instrument_id
            or self.position.rule_version_id != self.proposal.rule_version_id
            or self.position.side is not self.proposal.side
            or self.position.open_quantity != fill_event.quantity
            or self.position.cost_basis.amount != expected_position_cost
            or self.position.settlement_state.value != "OPEN"
            or self.position.originating_order_id != self.order.order_id
            or self.position.originating_fill_event_id != fill_event.event_id
        ):
            raise ValueError("partial-fill position state or provenance is inconsistent")

        if (
            self.risk_capacity_while_fully_reserved.amount
            != self.risk_capacity_before_order.amount - initial.maximum_loss.amount
            or self.risk_capacity_after_partial_fill.amount
            != self.risk_capacity_before_order.amount - self.active_risk_after_partial_fill.amount
            or self.risk_capacity_after_cancellation.amount
            != self.risk_capacity_before_order.amount - self.final_active_campaign_risk.amount
        ):
            raise ValueError("partial-fill campaign risk capacity is inconsistent")

        journal_types = {journal.journal_type for journal in self.journals}
        if (
            len(self.journals) != 3
            or journal_types != {JournalType.CONTRIBUTION, JournalType.PURCHASE, JournalType.FEE}
            or not all(journal.balanced for journal in self.journals)
        ):
            raise ValueError("partial fill requires contribution, purchase, and fee journals")
        execution_journals = tuple(
            journal
            for journal in self.journals
            if journal.journal_type in {JournalType.PURCHASE, JournalType.FEE}
        )
        if any(journal.source_id != fill_event.event_id for journal in execution_journals):
            raise ValueError("partial-fill journals must use the exact fill event identity")
        reconstructed_ledger = Ledger()
        for journal in self.journals:
            reconstructed_ledger.post(journal)
        if reconstructed_ledger.metrics() != self.metrics:
            raise ValueError("partial-fill metrics must be derived from retained journals")

        expected_fee = fill_event.fee.amount
        expected_cash = self.campaign.contribution.amount - expected_position_cost - expected_fee
        if (
            self.metrics.contributions != self.campaign.contribution
            or self.metrics.cash.amount != expected_cash
            or self.metrics.position_cost.amount != expected_position_cost
            or self.metrics.ledger_equity.amount != self.campaign.contribution.amount - expected_fee
            or self.metrics.realized_trading_pnl_before_fees != Decimal(0)
            or self.metrics.fees.amount != expected_fee
            or self.metrics.net_trading_pnl != -expected_fee
        ):
            raise ValueError("partial-fill ledger metrics are inconsistent")


@dataclass(frozen=True, slots=True)
class PartialFillCanceledSettledNoResult:
    run_id: str
    fixture_id: str
    fixture_manifest_hash: ContentHash
    engine_version: str
    seed: int
    logical_time: datetime
    campaign: Campaign
    information_set: InformationSet
    strategy_evaluation: StrategyEvaluation
    proposal: RestingLimitProposal
    allocation: AllocationDecision
    risk_decision: RiskDecision
    reservation_lifecycle: tuple[
        RiskReservation,
        RiskReservation,
        RiskReservation,
        RiskReservation,
    ]
    order: ExpiringLimitOrder
    execution_events: tuple[ExecutionEvent, ExecutionEvent, ExecutionEvent]
    pre_settlement_position: FilledPositionState
    contract_rule: ContractRuleVersion
    settlement_file_hash: ContentHash
    settlement_fact: RuleBoundSettlementFact
    settlement: PartialPositionSettlementResult
    settled_position: SettledFilledPositionState
    risk_capacity_before_order: Money
    risk_capacity_while_fully_reserved: Money
    risk_capacity_after_partial_fill: Money
    risk_capacity_after_cancellation: Money
    risk_capacity_after_settlement: Money
    pre_settlement_metrics: LedgerMetrics
    journals: tuple[Journal, ...]
    metrics: LedgerMetrics
    run_outcome: RunOutcome = field(
        default=RunOutcome.PARTIAL_FILL_CANCELED_SETTLED_NO,
        init=False,
    )
    runtime_capability: str = "SIMULATION_ONLY"
    execution_evidence: str = "SYNTHETIC"
    edge_evidence: str = "NONE"
    calibration_evidence: str = "NONE"

    @property
    def initial_reservation(self) -> RiskReservation:
        return self.reservation_lifecycle[0]

    @property
    def reservation_after_partial_fill(self) -> RiskReservation:
        return self.reservation_lifecycle[2]

    @property
    def final_reservation(self) -> RiskReservation:
        return self.reservation_lifecycle[3]

    @property
    def released_at_cancellation(self) -> Money:
        return Money(
            self.reservation_after_partial_fill.maximum_loss.amount
            - self.final_reservation.maximum_loss.amount
        )

    @property
    def active_risk_after_partial_fill(self) -> Money:
        return active_campaign_risk(
            self.pre_settlement_position.maximum_remaining_loss,
            self.reservation_after_partial_fill.maximum_loss,
        )

    @property
    def active_risk_after_cancellation(self) -> Money:
        return active_campaign_risk(
            self.pre_settlement_position.maximum_remaining_loss,
            self.final_reservation.maximum_loss,
        )

    @property
    def position_risk_released_at_settlement(self) -> Money:
        return self.settlement.position_risk_release

    @property
    def final_active_campaign_risk(self) -> Money:
        return active_campaign_risk(
            self.settled_position.maximum_remaining_loss,
            self.final_reservation.maximum_loss,
        )

    def __post_init__(self) -> None:
        pre_settlement_journals = tuple(
            journal
            for journal in self.journals
            if journal.journal_type is not JournalType.SETTLEMENT
        )
        PartialFillCanceledResult(
            run_id=self.run_id,
            fixture_id=self.fixture_id,
            fixture_manifest_hash=self.fixture_manifest_hash,
            engine_version=self.engine_version,
            seed=self.seed,
            logical_time=self.execution_events[-1].occurred_at,
            campaign=self.campaign,
            information_set=self.information_set,
            strategy_evaluation=self.strategy_evaluation,
            proposal=self.proposal,
            allocation=self.allocation,
            risk_decision=self.risk_decision,
            reservation_lifecycle=self.reservation_lifecycle,
            order=self.order,
            execution_events=self.execution_events,
            position=self.pre_settlement_position,
            risk_capacity_before_order=self.risk_capacity_before_order,
            risk_capacity_while_fully_reserved=self.risk_capacity_while_fully_reserved,
            risk_capacity_after_partial_fill=self.risk_capacity_after_partial_fill,
            risk_capacity_after_cancellation=self.risk_capacity_after_cancellation,
            journals=pre_settlement_journals,
            metrics=self.pre_settlement_metrics,
        )

        fact = self.settlement_fact
        if self.contract_rule != self.information_set.rule:
            raise ValueError("settlement contract rule must match the information set exactly")
        if (
            fact.fixture_id != self.fixture_id
            or fact.instrument_id != self.information_set.instrument.instrument_id
            or fact.rule_version_id != self.contract_rule.rule_version_id
            or fact.rule_content_hash != self.contract_rule.content_hash
            or fact.resolution_source != self.contract_rule.resolution_source
        ):
            raise ValueError("settlement fact does not match the exact contract provenance")
        if (
            fact.settlement_value != self.information_set.instrument.no_settlement_value
            or fact.settlement_value != Decimal(0)
        ):
            raise ValueError("partial canceled settled-NO result requires an exact zero payout")
        if (
            fact.source_event_at < self.contract_rule.determination_at
            or fact.available_at < self.contract_rule.determination_at
            or fact.available_at <= self.execution_events[-1].occurred_at
            or self.logical_time < fact.available_at
        ):
            raise ValueError("settlement timing is inconsistent with execution and determination")

        expected_loss = -self.pre_settlement_position.cost_basis.amount
        expected_settlement_id = rule_bound_position_settlement_id(
            self.pre_settlement_position,
            fact,
        )
        if (
            self.settlement.settlement_id != expected_settlement_id
            or self.settlement.source_id != fact.source_id
            or self.settlement.filled_quantity != self.pre_settlement_position.open_quantity
            or self.settlement.settlement_cash.amount != 0
            or self.settlement.realized_pnl != expected_loss
            or self.settlement.reservation_release.amount != 0
            or self.settlement.position_risk_release
            != self.pre_settlement_position.maximum_remaining_loss
        ):
            raise ValueError("NO settlement result does not derecognize the exact filled position")
        if (
            self.settled_position.instrument_id != self.pre_settlement_position.instrument_id
            or self.settled_position.rule_version_id != self.pre_settlement_position.rule_version_id
            or self.settled_position.side is not self.pre_settlement_position.side
            or self.settled_position.settled_quantity != self.pre_settlement_position.open_quantity
            or self.settled_position.settlement_value != fact.settlement_value
            or self.settled_position.settlement_id != self.settlement.settlement_id
            or self.settled_position.settlement_source_id != fact.source_id
            or self.settled_position.originating_order_id
            != self.pre_settlement_position.originating_order_id
            or self.settled_position.originating_fill_event_id
            != self.pre_settlement_position.originating_fill_event_id
            or self.settled_position.open_quantity.value != 0
            or self.settled_position.cost_basis.amount != 0
            or self.settled_position.maximum_remaining_loss.amount != 0
        ):
            raise ValueError("settled position does not preserve and close exact fill provenance")
        if (
            self.final_reservation.state is not ReservationState.RELEASED
            or self.final_reservation.maximum_loss.amount != 0
            or self.position_risk_released_at_settlement
            != self.pre_settlement_position.maximum_remaining_loss
            or self.final_active_campaign_risk.amount != 0
        ):
            raise ValueError("settlement did not release all and only filled-position risk")

        settlement_journals = tuple(
            journal for journal in self.journals if journal.journal_type is JournalType.SETTLEMENT
        )
        if (
            len(self.journals) != 4
            or len(settlement_journals) != 1
            or len(pre_settlement_journals) != 3
            or not all(journal.balanced for journal in self.journals)
        ):
            raise ValueError("partial canceled NO settlement requires four exact balanced journals")
        settlement_journal = settlement_journals[0]
        if (
            settlement_journal.source_id != fact.source_id
            or settlement_journal.occurred_at != fact.available_at
            or {posting.account_code for posting in settlement_journal.postings}
            != {AccountCode.POSITION_COST, AccountCode.REALIZED_PNL}
            or any(
                posting.amount != self.pre_settlement_position.cost_basis
                for posting in settlement_journal.postings
            )
        ):
            raise ValueError("NO settlement journal does not record the exact contract loss")

        reconstructed_ledger = Ledger()
        for journal in self.journals:
            reconstructed_ledger.post(journal)
        if reconstructed_ledger.metrics() != self.metrics:
            raise ValueError("settled-NO metrics must be derived from retained journals")
        expected_final_equity = (
            self.campaign.contribution.amount
            - self.pre_settlement_position.cost_basis.amount
            - self.pre_settlement_metrics.fees.amount
        )
        if (
            self.metrics.contributions != self.campaign.contribution
            or self.metrics.cash != self.pre_settlement_metrics.cash
            or self.metrics.position_cost.amount != 0
            or self.metrics.ledger_equity.amount != expected_final_equity
            or self.metrics.realized_trading_pnl_before_fees != expected_loss
            or self.metrics.fees != self.pre_settlement_metrics.fees
            or self.metrics.net_trading_pnl
            != expected_loss - self.pre_settlement_metrics.fees.amount
            or self.metrics.ledger_equity.amount
            != self.metrics.contributions.amount + self.metrics.net_trading_pnl
        ):
            raise ValueError("settled-NO final financial metrics are inconsistent")

        expected_final_capacity = Money(
            CampaignSnapshot(
                campaign=self.campaign,
                ledger_equity=self.metrics.ledger_equity,
                settled_cash=self.metrics.cash,
                active_reservations=self.final_active_campaign_risk,
            ).risk_capacity
        )
        if self.risk_capacity_after_settlement != expected_final_capacity:
            raise ValueError("settled-NO final risk capacity is not ledger-derived")


@dataclass(frozen=True, slots=True)
class PartialFillCanceledSettledVoidResult:
    run_id: str
    fixture_id: str
    fixture_manifest_hash: ContentHash
    engine_version: str
    seed: int
    logical_time: datetime
    campaign: Campaign
    information_set: InformationSet
    strategy_evaluation: StrategyEvaluation
    proposal: RestingLimitProposal
    allocation: AllocationDecision
    risk_decision: RiskDecision
    reservation_lifecycle: tuple[
        RiskReservation,
        RiskReservation,
        RiskReservation,
        RiskReservation,
    ]
    order: ExpiringLimitOrder
    execution_events: tuple[ExecutionEvent, ExecutionEvent, ExecutionEvent]
    pre_settlement_position: FilledPositionState
    contract_rule: VoidContractRuleVersion
    settlement_file_hash: ContentHash
    settlement_fact: ClassifiedRuleBoundSettlementFact
    settlement: ClassifiedPartialPositionSettlementResult
    settled_position: ClassifiedSettledFilledPositionState
    risk_capacity_before_order: Money
    risk_capacity_while_fully_reserved: Money
    risk_capacity_after_partial_fill: Money
    risk_capacity_after_cancellation: Money
    risk_capacity_after_settlement: Money
    pre_settlement_metrics: LedgerMetrics
    journals: tuple[Journal, ...]
    metrics: LedgerMetrics
    run_outcome: RunOutcome = field(
        default=RunOutcome.PARTIAL_FILL_CANCELED_SETTLED_VOID,
        init=False,
    )
    runtime_capability: str = "SIMULATION_ONLY"
    execution_evidence: str = "SYNTHETIC"
    edge_evidence: str = "NONE"
    calibration_evidence: str = "NONE"

    @property
    def initial_reservation(self) -> RiskReservation:
        return self.reservation_lifecycle[0]

    @property
    def reservation_after_partial_fill(self) -> RiskReservation:
        return self.reservation_lifecycle[2]

    @property
    def final_reservation(self) -> RiskReservation:
        return self.reservation_lifecycle[3]

    @property
    def released_at_cancellation(self) -> Money:
        return Money(
            self.reservation_after_partial_fill.maximum_loss.amount
            - self.final_reservation.maximum_loss.amount
        )

    @property
    def active_risk_after_partial_fill(self) -> Money:
        return active_campaign_risk(
            self.pre_settlement_position.maximum_remaining_loss,
            self.reservation_after_partial_fill.maximum_loss,
        )

    @property
    def active_risk_after_cancellation(self) -> Money:
        return active_campaign_risk(
            self.pre_settlement_position.maximum_remaining_loss,
            self.final_reservation.maximum_loss,
        )

    @property
    def position_risk_released_at_settlement(self) -> Money:
        return self.settlement.position_risk_release

    @property
    def final_active_campaign_risk(self) -> Money:
        return active_campaign_risk(
            self.settled_position.maximum_remaining_loss,
            self.final_reservation.maximum_loss,
        )

    def __post_init__(self) -> None:
        pre_settlement_journals = tuple(
            journal
            for journal in self.journals
            if journal.journal_type is not JournalType.SETTLEMENT
        )
        PartialFillCanceledResult(
            run_id=self.run_id,
            fixture_id=self.fixture_id,
            fixture_manifest_hash=self.fixture_manifest_hash,
            engine_version=self.engine_version,
            seed=self.seed,
            logical_time=self.execution_events[-1].occurred_at,
            campaign=self.campaign,
            information_set=self.information_set,
            strategy_evaluation=self.strategy_evaluation,
            proposal=self.proposal,
            allocation=self.allocation,
            risk_decision=self.risk_decision,
            reservation_lifecycle=self.reservation_lifecycle,
            order=self.order,
            execution_events=self.execution_events,
            position=self.pre_settlement_position,
            risk_capacity_before_order=self.risk_capacity_before_order,
            risk_capacity_while_fully_reserved=self.risk_capacity_while_fully_reserved,
            risk_capacity_after_partial_fill=self.risk_capacity_after_partial_fill,
            risk_capacity_after_cancellation=self.risk_capacity_after_cancellation,
            journals=pre_settlement_journals,
            metrics=self.pre_settlement_metrics,
        )

        fact = self.settlement_fact
        if self.fixture_id != "partial-fill-canceled-settled-void":
            raise ValueError("settled-VOID result requires its registered fixture identity")
        if self.contract_rule != self.information_set.rule:
            raise ValueError("VOID settlement contract rule must match the information set exactly")
        rule_term = self.contract_rule.void_settlement_term
        if (
            fact.fixture_id != self.fixture_id
            or fact.source_id != "settlement-partial-fill-canceled-void"
            or fact.instrument_id != self.information_set.instrument.instrument_id
            or fact.rule_version_id != self.contract_rule.rule_version_id
            or fact.rule_content_hash != self.contract_rule.content_hash
            or fact.resolution_source != self.contract_rule.resolution_source
        ):
            raise ValueError("VOID settlement fact does not match the exact contract provenance")
        if (
            fact.outcome_classification is not SettlementOutcome.VOID
            or fact.outcome_classification is not rule_term.outcome_classification
            or fact.settlement_value != rule_term.settlement_value
            or fact.settlement_value_text != rule_term.settlement_value_text
            or self.information_set.instrument.void_settlement_value != rule_term.settlement_value
            or not Decimal(0) < fact.settlement_value <= Decimal(1)
        ):
            raise ValueError("partial canceled VOID settlement requires its exact bounded value")
        if (
            fact.source_event_at < self.contract_rule.determination_at
            or fact.available_at < self.contract_rule.determination_at
            or fact.available_at <= self.execution_events[-1].occurred_at
            or self.logical_time < fact.available_at
        ):
            raise ValueError("VOID settlement timing is inconsistent with execution and rule")

        expected_cash = Money(
            self.pre_settlement_position.open_quantity.value * fact.settlement_value
        )
        expected_realized = expected_cash.amount - self.pre_settlement_position.cost_basis.amount
        expected_settlement_id = classified_rule_bound_position_settlement_id(
            self.pre_settlement_position,
            fact,
        )
        if (
            self.settlement.settlement_id != expected_settlement_id
            or self.settlement.source_id != fact.source_id
            or self.settlement.filled_quantity != self.pre_settlement_position.open_quantity
            or self.settlement.settlement_cash != expected_cash
            or self.settlement.realized_pnl != expected_realized
            or self.settlement.reservation_release.amount != 0
            or self.settlement.position_risk_release
            != self.pre_settlement_position.maximum_remaining_loss
            or self.settlement.outcome_classification is not SettlementOutcome.VOID
        ):
            raise ValueError("VOID settlement does not derecognize the exact filled position")
        if (
            self.settled_position.instrument_id != self.pre_settlement_position.instrument_id
            or self.settled_position.rule_version_id != self.pre_settlement_position.rule_version_id
            or self.settled_position.side is not self.pre_settlement_position.side
            or self.settled_position.settled_quantity != self.pre_settlement_position.open_quantity
            or self.settled_position.outcome_classification is not SettlementOutcome.VOID
            or self.settled_position.settlement_value != fact.settlement_value
            or self.settled_position.settlement_cash != expected_cash
            or self.settled_position.realized_contract_pnl != expected_realized
            or self.settled_position.settlement_id != self.settlement.settlement_id
            or self.settled_position.settlement_source_id != fact.source_id
            or self.settled_position.originating_order_id
            != self.pre_settlement_position.originating_order_id
            or self.settled_position.originating_fill_event_id
            != self.pre_settlement_position.originating_fill_event_id
            or self.settled_position.open_quantity.value != 0
            or self.settled_position.cost_basis.amount != 0
            or self.settled_position.maximum_remaining_loss.amount != 0
        ):
            raise ValueError("VOID-settled position does not preserve exact fill provenance")
        if (
            self.final_reservation.state is not ReservationState.RELEASED
            or self.final_reservation.maximum_loss.amount != 0
            or self.position_risk_released_at_settlement
            != self.pre_settlement_position.maximum_remaining_loss
            or self.final_active_campaign_risk.amount != 0
        ):
            raise ValueError("VOID settlement did not release all and only position risk")

        settlement_journals = tuple(
            journal for journal in self.journals if journal.journal_type is JournalType.SETTLEMENT
        )
        if (
            len(self.journals) != 4
            or len(settlement_journals) != 1
            or len(pre_settlement_journals) != 3
            or not all(journal.balanced for journal in self.journals)
        ):
            raise ValueError("partial canceled VOID settlement requires four balanced journals")
        expected_settlement_journal = JournalFactory.settlement(
            fact.source_id,
            expected_cash,
            self.pre_settlement_position.cost_basis,
            fact.available_at,
        )
        if settlement_journals[0] != expected_settlement_journal:
            raise ValueError("VOID settlement journal does not record the exact contract gain")

        reconstructed_ledger = Ledger()
        for journal in self.journals:
            reconstructed_ledger.post(journal)
        if reconstructed_ledger.metrics() != self.metrics:
            raise ValueError("settled-VOID metrics must be derived from retained journals")
        expected_final_cash = self.pre_settlement_metrics.cash.amount + expected_cash.amount
        expected_final_equity = (
            self.pre_settlement_metrics.ledger_equity.amount
            - self.pre_settlement_position.cost_basis.amount
            + expected_cash.amount
        )
        if (
            self.metrics.contributions != self.campaign.contribution
            or self.metrics.cash.amount != expected_final_cash
            or self.metrics.position_cost.amount != 0
            or self.metrics.ledger_equity.amount != expected_final_equity
            or self.metrics.realized_trading_pnl_before_fees != expected_realized
            or self.metrics.fees != self.pre_settlement_metrics.fees
            or self.metrics.net_trading_pnl
            != expected_realized - self.pre_settlement_metrics.fees.amount
            or self.metrics.ledger_equity.amount
            != self.metrics.contributions.amount + self.metrics.net_trading_pnl
        ):
            raise ValueError("settled-VOID final financial metrics are inconsistent")

        expected_final_capacity = Money(
            CampaignSnapshot(
                campaign=self.campaign,
                ledger_equity=self.metrics.ledger_equity,
                settled_cash=self.metrics.cash,
                active_reservations=self.final_active_campaign_risk,
            ).risk_capacity
        )
        if self.risk_capacity_after_settlement != expected_final_capacity:
            raise ValueError("settled-VOID final risk capacity is not ledger-derived")


type RunResult = (
    SimulationResult
    | NoProposalResult
    | PolicyRejectedResult
    | InvariantRejectedResult
    | UnfilledExpiredResult
    | PartialFillCanceledResult
    | PartialFillCanceledSettledNoResult
    | PartialFillCanceledSettledVoidResult
)


@dataclass(frozen=True, slots=True)
class ContributionInput:
    source_id: str
    amount: Money
