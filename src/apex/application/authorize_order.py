"""Atomic durable authorization without adapter or SQLAlchemy dependencies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from apex.domain.campaign import Campaign, CampaignPolicy, CampaignSnapshot, OperationalInvariants
from apex.domain.decisions import AllocationDecision, OpportunityProposal, RiskDecision, RiskKernel
from apex.domain.execution import Order, RiskReservation, create_reserved_order
from apex.domain.ledger import Journal, JournalFactory
from apex.domain.market import InformationSet
from apex.domain.values import Money, content_digest


class PersistenceConflictError(RuntimeError):
    """A durable identity was reused with different authoritative content."""


class IdempotencyConflictError(PersistenceConflictError):
    """A client order identity was reused for a different request."""


class MissingPersistenceStateError(LookupError):
    """Required durable authorization evidence was not found."""


class InjectedAuthorizationFault(RuntimeError):
    """A test-only direct fault interrupted an authorization transaction."""


class AuthorizationFaultPoint(StrEnum):
    AFTER_RISK_DECISION = "AFTER_RISK_DECISION"
    AFTER_RESERVATION = "AFTER_RESERVATION"
    AFTER_ORDER = "AFTER_ORDER"
    BEFORE_COMMIT = "BEFORE_COMMIT"


@dataclass(frozen=True, slots=True)
class CampaignInitializationCommand:
    campaign_id: str
    contribution: Money
    authorized_loss: Money
    policy: CampaignPolicy
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class CampaignInitializationResult:
    campaign: Campaign
    contribution_journal: Journal
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class PersistAuthorizationInputsCommand:
    campaign_id: str
    proposal: OpportunityProposal
    allocation: AllocationDecision
    information_set: InformationSet
    market_observation_source_id: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class PersistedAuthorizationInputs:
    proposal_id: str
    allocation_id: str


@dataclass(frozen=True, slots=True)
class AuthorizeAndReserveCommand:
    campaign_id: str
    proposal_id: str
    allocation_id: str
    client_order_id: str
    authorization_at: datetime
    fault_at: AuthorizationFaultPoint | None = None


@dataclass(frozen=True, slots=True)
class CapacityObservation:
    contribution: Money
    settled_cash: Money
    ledger_equity: Money
    protected_equity_floor: Money
    active_order_reservations: Money
    active_position_risk: Money
    available_risk_capacity: Money


@dataclass(frozen=True, slots=True)
class AuthorizedOrderResult:
    risk_decision: RiskDecision
    reservation: RiskReservation
    order: Order
    capacity_before: CapacityObservation
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class PersistedRiskRejection:
    risk_decision: RiskDecision
    capacity_before: CapacityObservation
    idempotent_replay: bool


type AuthorizationResult = AuthorizedOrderResult | PersistedRiskRejection


class StoredProposalView(Protocol):
    proposal: OpportunityProposal
    information_set: InformationSet
    market_source_id: str
    canonical_payload_hash: str


class StoredAllocationView(Protocol):
    allocation: AllocationDecision
    campaign_id: str
    canonical_payload_hash: str


class StoredRiskDecisionView(Protocol):
    decision: RiskDecision
    campaign_id: str
    client_order_id: str
    authorization_at: datetime
    idempotency_payload_hash: str


class StoredOrderView(Protocol):
    order: Order
    campaign_id: str
    idempotency_payload_hash: str


class LedgerBalancesView(Protocol):
    settled_cash: Money
    contributions: Money
    ledger_equity: Money
    trading_pnl: Decimal


class CampaignRepository(Protocol):
    def lock(self, campaign_id: str) -> Campaign | None: ...

    def get(self, campaign_id: str) -> Campaign | None: ...

    def get_initialization_hash(self, campaign_id: str) -> str | None: ...

    def add(self, value: Campaign, initialization_hash: str, created_at: datetime) -> None: ...


class LedgerRepository(Protocol):
    def add_accounts(self, campaign_id: str) -> None: ...

    def post_contribution(self, campaign_id: str, journal: Journal) -> Journal: ...

    def balances(self, campaign_id: str) -> LedgerBalancesView: ...


class ProposalRepository(Protocol):
    def add(
        self,
        campaign_id: str,
        proposal: OpportunityProposal,
        information_set: InformationSet,
        market_source_id: str,
        created_at: datetime,
    ) -> None: ...

    def get(self, proposal_id: str) -> StoredProposalView | None: ...


class AllocationRepository(Protocol):
    def add(self, campaign_id: str, value: AllocationDecision, created_at: datetime) -> None: ...

    def get(self, allocation_id: str) -> StoredAllocationView | None: ...


class RiskDecisionRepository(Protocol):
    def get_by_client(
        self, campaign_id: str, client_order_id: str
    ) -> StoredRiskDecisionView | None: ...

    def add_rejection(
        self,
        campaign_id: str,
        client_order_id: str,
        value: RiskDecision,
        authorization_at: datetime,
        information_set: InformationSet,
        idempotency_payload_hash: str,
    ) -> None: ...


class ReservationRepository(Protocol):
    def active_total(self, campaign_id: str) -> Money: ...

    def get_by_risk(self, risk_decision_id: str) -> RiskReservation | None: ...


class PositionRepository(Protocol):
    def active_total(self, campaign_id: str) -> Money: ...


class OrderRepository(Protocol):
    def get_by_client(self, campaign_id: str, client_order_id: str) -> StoredOrderView | None: ...

    def fingerprints(self, campaign_id: str) -> tuple[str, ...]: ...


class AuthorizationUnitOfWork(Protocol):
    campaigns: CampaignRepository
    ledger: LedgerRepository
    proposals: ProposalRepository
    allocations: AllocationRepository
    risk_decisions: RiskDecisionRepository
    reservations: ReservationRepository
    positions: PositionRepository
    orders: OrderRepository

    def __enter__(self) -> AuthorizationUnitOfWork: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def stage_authorization_bundle(
        self,
        *,
        campaign_id: str,
        client_order_id: str,
        decision: RiskDecision,
        authorization_at: datetime,
        information_set: InformationSet,
        reservation: RiskReservation,
        authorized_order: Order,
        idempotency_payload_hash: str,
        fault: Callable[[str], None] | None = None,
    ) -> None: ...

    def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], AuthorizationUnitOfWork]


class DurableAuthorizationService:
    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def initialize_campaign(
        self, command: CampaignInitializationCommand
    ) -> CampaignInitializationResult:
        campaign_value = Campaign(
            campaign_id=command.campaign_id,
            contribution=command.contribution,
            authorized_loss=command.authorized_loss,
            policy=command.policy,
        )
        journal = JournalFactory.contribution(
            source_id=command.campaign_id,
            amount=command.contribution,
            occurred_at=command.occurred_at,
        )
        initialization_hash = content_digest(
            {
                "campaign": campaign_value,
                "contribution_journal": journal,
            }
        )
        with self._unit_of_work_factory() as unit_of_work:
            existing_hash = unit_of_work.campaigns.get_initialization_hash(command.campaign_id)
            if existing_hash is not None:
                if existing_hash != initialization_hash:
                    raise IdempotencyConflictError(
                        "campaign identity was initialized with different content"
                    )
                existing = unit_of_work.campaigns.get(command.campaign_id)
                if existing is None:
                    raise MissingPersistenceStateError("initialized campaign is missing")
                unit_of_work.commit()
                return CampaignInitializationResult(existing, journal, True)
            unit_of_work.campaigns.add(
                campaign_value,
                initialization_hash,
                command.occurred_at.astimezone(UTC),
            )
            unit_of_work.ledger.add_accounts(command.campaign_id)
            unit_of_work.ledger.post_contribution(command.campaign_id, journal)
            unit_of_work.commit()
            return CampaignInitializationResult(campaign_value, journal, False)

    def persist_authorization_inputs(
        self, command: PersistAuthorizationInputsCommand
    ) -> PersistedAuthorizationInputs:
        if command.proposal.proposal_id != command.allocation.proposal_id:
            raise PersistenceConflictError("allocation does not belong to the proposal")
        with self._unit_of_work_factory() as unit_of_work:
            if unit_of_work.campaigns.get(command.campaign_id) is None:
                raise MissingPersistenceStateError("campaign is not initialized")
            unit_of_work.proposals.add(
                command.campaign_id,
                command.proposal,
                command.information_set,
                command.market_observation_source_id,
                command.recorded_at.astimezone(UTC),
            )
            unit_of_work.allocations.add(
                command.campaign_id,
                command.allocation,
                command.recorded_at.astimezone(UTC),
            )
            unit_of_work.commit()
        return PersistedAuthorizationInputs(
            proposal_id=command.proposal.proposal_id,
            allocation_id=command.allocation.allocation_id,
        )

    @staticmethod
    def _capacity(
        campaign_value: Campaign,
        balances: LedgerBalancesView,
        active_reservations: Money,
        active_position_risk: Money,
    ) -> tuple[CampaignSnapshot, CapacityObservation]:
        combined_active_risk = Money(active_reservations.amount + active_position_risk.amount)
        snapshot = CampaignSnapshot(
            campaign=campaign_value,
            ledger_equity=balances.ledger_equity,
            settled_cash=balances.settled_cash,
            active_reservations=combined_active_risk,
        )
        observation = CapacityObservation(
            contribution=balances.contributions,
            settled_cash=balances.settled_cash,
            ledger_equity=balances.ledger_equity,
            protected_equity_floor=Money(snapshot.protected_equity_floor),
            active_order_reservations=active_reservations,
            active_position_risk=active_position_risk,
            available_risk_capacity=Money(snapshot.risk_capacity),
        )
        return snapshot, observation

    @staticmethod
    def _raise_at(command: AuthorizeAndReserveCommand, point: str) -> None:
        if command.fault_at is not None and command.fault_at.value == point:
            raise InjectedAuthorizationFault(f"authorization interrupted at {point}")

    def authorize_and_reserve(self, command: AuthorizeAndReserveCommand) -> AuthorizationResult:
        if not command.client_order_id:
            raise ValueError("client order identity must not be empty")
        if command.authorization_at.tzinfo is None or command.authorization_at.utcoffset() is None:
            raise ValueError("authorization time must be timezone-aware")
        authorization_at = command.authorization_at.astimezone(UTC)
        with self._unit_of_work_factory() as unit_of_work:
            campaign_value = unit_of_work.campaigns.lock(command.campaign_id)
            if campaign_value is None:
                raise MissingPersistenceStateError("campaign is not initialized")
            stored_proposal = unit_of_work.proposals.get(command.proposal_id)
            stored_allocation = unit_of_work.allocations.get(command.allocation_id)
            if stored_proposal is None or stored_allocation is None:
                raise MissingPersistenceStateError("authorization inputs are missing")
            if stored_allocation.campaign_id != command.campaign_id:
                raise PersistenceConflictError("allocation belongs to another campaign")
            if stored_allocation.allocation.proposal_id != stored_proposal.proposal.proposal_id:
                raise PersistenceConflictError("durable proposal and allocation do not match")
            request_hash = content_digest(
                {
                    "campaign_id": command.campaign_id,
                    "proposal_id": command.proposal_id,
                    "proposal_payload_hash": stored_proposal.canonical_payload_hash,
                    "allocation_id": command.allocation_id,
                    "allocation_payload_hash": stored_allocation.canonical_payload_hash,
                    "client_order_id": command.client_order_id,
                    "authorization_at": authorization_at,
                }
            )
            balances = unit_of_work.ledger.balances(command.campaign_id)
            snapshot, capacity = self._capacity(
                campaign_value,
                balances,
                unit_of_work.reservations.active_total(command.campaign_id),
                unit_of_work.positions.active_total(command.campaign_id),
            )
            existing_risk = unit_of_work.risk_decisions.get_by_client(
                command.campaign_id, command.client_order_id
            )
            if existing_risk is not None:
                if existing_risk.idempotency_payload_hash != request_hash:
                    raise IdempotencyConflictError(
                        "client order identity was reused for a different request"
                    )
                if not existing_risk.decision.accepted:
                    unit_of_work.commit()
                    return PersistedRiskRejection(existing_risk.decision, capacity, True)
                stored_order = unit_of_work.orders.get_by_client(
                    command.campaign_id, command.client_order_id
                )
                reservation = unit_of_work.reservations.get_by_risk(
                    existing_risk.decision.risk_decision_id
                )
                if stored_order is None or reservation is None:
                    raise MissingPersistenceStateError(
                        "accepted authorization bundle is incomplete"
                    )
                unit_of_work.commit()
                return AuthorizedOrderResult(
                    existing_risk.decision,
                    reservation,
                    stored_order.order,
                    capacity,
                    True,
                )
            decision = RiskKernel.evaluate(
                stored_proposal.proposal,
                stored_allocation.allocation,
                snapshot,
                stored_proposal.information_set,
                campaign_value.policy,
                OperationalInvariants(),
                authorization_at=authorization_at,
                market_observation_source_id=stored_proposal.market_source_id,
                existing_order_fingerprints=unit_of_work.orders.fingerprints(command.campaign_id),
            )
            if not decision.accepted:
                unit_of_work.risk_decisions.add_rejection(
                    command.campaign_id,
                    command.client_order_id,
                    decision,
                    authorization_at,
                    stored_proposal.information_set,
                    request_hash,
                )
                self._raise_at(command, AuthorizationFaultPoint.AFTER_RISK_DECISION.value)
                self._raise_at(command, AuthorizationFaultPoint.BEFORE_COMMIT.value)
                unit_of_work.commit()
                return PersistedRiskRejection(decision, capacity, False)
            reservation, authorized_order = create_reserved_order(
                campaign_id=command.campaign_id,
                proposal=stored_proposal.proposal,
                allocation=stored_allocation.allocation,
                risk_decision=decision,
            )
            authorized_order = replace(
                authorized_order,
                client_order_id=command.client_order_id,
            )
            unit_of_work.stage_authorization_bundle(
                campaign_id=command.campaign_id,
                client_order_id=command.client_order_id,
                decision=decision,
                authorization_at=authorization_at,
                information_set=stored_proposal.information_set,
                reservation=reservation,
                authorized_order=authorized_order,
                idempotency_payload_hash=request_hash,
                fault=lambda point: self._raise_at(command, point),
            )
            self._raise_at(command, AuthorizationFaultPoint.BEFORE_COMMIT.value)
            unit_of_work.commit()
            return AuthorizedOrderResult(
                decision,
                reservation,
                authorized_order,
                capacity,
                False,
            )
