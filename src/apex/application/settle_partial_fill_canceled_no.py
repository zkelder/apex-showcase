"""Durable rule-bound settlement for one canceled partial-fill Position."""

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
from apex.domain.execution import (
    AppliedClassifiedPartialPositionSettlement,
    AppliedPartialPositionSettlement,
    ClassifiedRuleBoundSettlementFact,
    ExpiringLimitOrder,
    FilledPositionState,
    RuleBoundSettlementFact,
    SettledFilledPositionState,
    SettlementResult,
    settle_canceled_filled_position,
    settle_canceled_filled_void_position,
)
from apex.domain.ledger import Journal, JournalFactory
from apex.domain.market import (
    ContractRuleVersion,
    Instrument,
    SettlementOutcome,
    VoidContractRuleVersion,
)
from apex.domain.values import ContentHash, ValueValidationError, deterministic_id

SettlementEvidence = RuleBoundSettlementFact | ClassifiedRuleBoundSettlementFact


class InjectedSettlementFault(RuntimeError):
    """A test-only direct fault interrupted one durable settlement transaction."""


class SettlementFaultPoint(StrEnum):
    AFTER_SETTLEMENT_FACT = "AFTER_SETTLEMENT_FACT"
    AFTER_SETTLEMENT_JOURNAL = "AFTER_SETTLEMENT_JOURNAL"
    AFTER_POSITION_SETTLEMENT = "AFTER_POSITION_SETTLEMENT"
    AFTER_POSITION_UPDATE = "AFTER_POSITION_UPDATE"
    BEFORE_COMMIT = "BEFORE_COMMIT"


@dataclass(frozen=True, slots=True)
class SettlePartialFillCanceledCommand:
    campaign_id: str
    order_id: str
    fact: SettlementEvidence
    recorded_at: datetime
    void_term_content_hash: ContentHash | None = None
    fault_at: SettlementFaultPoint | None = None


@dataclass(frozen=True, slots=True)
class DurablePositionSettlement:
    settlement_id: str
    fact: SettlementEvidence
    void_term_content_hash: ContentHash | None
    journal: Journal


@dataclass(frozen=True, slots=True)
class DurableSettlementState:
    campaign_id: str
    order: ExpiringLimitOrder
    reservation: DurableRiskReservationState
    position: FilledPositionState | SettledFilledPositionState
    instrument: Instrument
    rule: ContractRuleVersion
    execution_completed_at: datetime
    settlement: DurablePositionSettlement | None

    def __post_init__(self) -> None:
        if (
            self.execution_completed_at.tzinfo is None
            or self.execution_completed_at.utcoffset() is None
        ):
            raise ValueValidationError("execution completion time must be timezone-aware")
        object.__setattr__(
            self, "execution_completed_at", self.execution_completed_at.astimezone(UTC)
        )
        if self.position.instrument_id != self.instrument.instrument_id:
            raise ValueValidationError("durable position does not match persisted instrument")
        if self.position.rule_version_id != self.rule.rule_version_id:
            raise ValueValidationError("durable position does not match persisted rule")
        if isinstance(self.position, SettledFilledPositionState) != (self.settlement is not None):
            raise ValueValidationError("durable position settlement state is incomplete")


@dataclass(frozen=True, slots=True)
class DurableSettlementResult:
    state: DurableSettlementState
    settlement_journal: Journal
    idempotent_replay: bool


def settlement_fact_id(
    campaign_id: str,
    fact: SettlementEvidence,
    classification: SettlementOutcome,
    void_term_content_hash: ContentHash | None,
) -> str:
    """Bind immutable evidence identity to its exact settlement authority."""

    payload = {
        "campaign_id": campaign_id,
        "source_id": fact.source_id,
        "instrument_id": fact.instrument_id,
        "rule_version_id": fact.rule_version_id,
        "rule_content_hash": fact.rule_content_hash,
        "resolution_source": fact.resolution_source,
        "content_hash": fact.content_hash,
    }
    if classification is SettlementOutcome.VOID:
        payload["classification"] = classification
        payload["void_term_content_hash"] = void_term_content_hash
    return deterministic_id("settlement_fact", payload)


class SettlementRepository(Protocol):
    def lock_settlement_state(
        self, campaign_id: str, order_id: str
    ) -> DurableSettlementState | None: ...


class SettlementUnitOfWork(Protocol):
    campaigns: CampaignRepository
    settlements: SettlementRepository

    def __enter__(self) -> SettlementUnitOfWork: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def stage_settlement(
        self,
        *,
        before: DurableSettlementState,
        fact: SettlementEvidence,
        classification: SettlementOutcome,
        void_term_content_hash: ContentHash | None,
        settled_position: SettledFilledPositionState,
        settlement_result: SettlementResult,
        settlement_journal: Journal,
        recorded_at: datetime,
        fault: Callable[[str], None] | None = None,
    ) -> DurableSettlementState: ...

    def commit(self) -> None: ...


type SettlementUnitOfWorkFactory = Callable[[], SettlementUnitOfWork]


class DurablePartialFillCanceledSettlementService:
    """Own the sole durable OPEN Position -> SETTLED NO or VOID transition."""

    def __init__(self, unit_of_work_factory: SettlementUnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    @staticmethod
    def _recorded_at(command: SettlePartialFillCanceledCommand) -> datetime:
        if not command.campaign_id or not command.order_id:
            raise ValueError("campaign and order identities must not be empty")
        if command.recorded_at.tzinfo is None or command.recorded_at.utcoffset() is None:
            raise ValueError("settlement recording time must be timezone-aware")
        recorded_at = command.recorded_at.astimezone(UTC)
        if recorded_at < command.fact.available_at:
            raise ValueError("settlement cannot be recorded before its evidence is available")
        return recorded_at

    @staticmethod
    def _raise_at(command: SettlePartialFillCanceledCommand, point: str) -> None:
        if command.fault_at is not None and command.fault_at.value == point:
            raise InjectedSettlementFault(f"settlement interrupted at {point}")

    @staticmethod
    def _validate_no_evidence(
        fact: RuleBoundSettlementFact,
        void_term_content_hash: ContentHash | None,
    ) -> SettlementOutcome:
        """A zero number is insufficient: the authenticated payload must say NO."""

        expected_raw_payload = (
            "resolution:NO;"
            f"settlement_value={fact.settlement_value_text};"
            f"instrument={fact.instrument_id};"
            f"rule={fact.rule_version_id};"
            f"rule_hash={fact.rule_content_hash.value};"
            f"source={fact.resolution_source}"
        )
        if fact.raw_payload != expected_raw_payload:
            raise ValueValidationError("settlement evidence is not the explicit NO classification")
        if fact.settlement_value != 0:
            raise ValueValidationError("durable NO settlement requires a zero settlement value")
        if void_term_content_hash is not None:
            raise ValueValidationError("NO settlement cannot carry a VOID rule term")
        return SettlementOutcome.NO

    @staticmethod
    def _validate_void_evidence(
        fact: ClassifiedRuleBoundSettlementFact,
        rule: ContractRuleVersion,
        void_term_content_hash: ContentHash | None,
    ) -> SettlementOutcome:
        if not isinstance(rule, VoidContractRuleVersion):
            raise ValueValidationError(
                "durable VOID settlement requires a persisted VOID contract rule"
            )
        term = rule.void_settlement_term
        if void_term_content_hash != term.content_hash:
            raise ValueValidationError(
                "VOID settlement term hash does not match the persisted rule"
            )
        if fact.outcome_classification is not SettlementOutcome.VOID:
            raise ValueValidationError("durable classified settlement must be VOID")
        return SettlementOutcome.VOID

    @classmethod
    def _classification(
        cls,
        fact: SettlementEvidence,
        rule: ContractRuleVersion,
        void_term_content_hash: ContentHash | None,
    ) -> SettlementOutcome:
        if isinstance(fact, RuleBoundSettlementFact):
            return cls._validate_no_evidence(fact, void_term_content_hash)
        return cls._validate_void_evidence(fact, rule, void_term_content_hash)

    @staticmethod
    def _journal_for(
        settlement_id: str,
        settlement_result: SettlementResult,
        position: FilledPositionState,
        occurred_at: datetime,
    ) -> Journal:
        return JournalFactory.settlement(
            settlement_id,
            settlement_result.settlement_cash,
            position.cost_basis,
            occurred_at,
        )

    @staticmethod
    def _apply(
        state: DurableSettlementState,
        fact: SettlementEvidence,
        classification: SettlementOutcome,
    ) -> AppliedPartialPositionSettlement | AppliedClassifiedPartialPositionSettlement:
        if not isinstance(state.position, FilledPositionState):
            raise AssertionError("settlement position was not validated as open")
        if classification is SettlementOutcome.NO:
            if not isinstance(fact, RuleBoundSettlementFact):
                raise AssertionError("NO settlement fact type was not validated")
            return settle_canceled_filled_position(
                state.position,
                state.reservation.transition_value(),
                state.order,
                state.instrument,
                state.rule,
                fact,
                execution_completed_at=state.execution_completed_at,
            )
        if not isinstance(fact, ClassifiedRuleBoundSettlementFact):
            raise AssertionError("VOID settlement fact type was not validated")
        if not isinstance(state.rule, VoidContractRuleVersion):
            raise AssertionError("VOID contract rule was not validated")
        return settle_canceled_filled_void_position(
            state.position,
            state.reservation.transition_value(),
            state.order,
            state.instrument,
            state.rule,
            fact,
            execution_completed_at=state.execution_completed_at,
        )

    def settle(self, command: SettlePartialFillCanceledCommand) -> DurableSettlementResult:
        recorded_at = self._recorded_at(command)
        with self._unit_of_work_factory() as unit_of_work:
            # All later locks are taken by the repository in order: Order,
            # RiskReservation, then Position.
            if unit_of_work.campaigns.lock(command.campaign_id) is None:
                raise MissingPersistenceStateError("campaign is not initialized")
            state = unit_of_work.settlements.lock_settlement_state(
                command.campaign_id, command.order_id
            )
            if state is None:
                raise MissingPersistenceStateError("durable partial-fill position was not found")
            classification = self._classification(
                command.fact,
                state.rule,
                command.void_term_content_hash,
            )
            if state.settlement is not None:
                if (
                    state.settlement.fact != command.fact
                    or state.settlement.void_term_content_hash != command.void_term_content_hash
                ):
                    raise IdempotencyConflictError(
                        "durable position was settled with different evidence"
                    )
                unit_of_work.commit()
                return DurableSettlementResult(state, state.settlement.journal, True)
            if not isinstance(state.position, FilledPositionState):
                raise PersistenceConflictError("durable position is not open for settlement")
            applied = self._apply(state, command.fact, classification)
            settlement_journal = self._journal_for(
                applied.result.settlement_id,
                applied.result,
                state.position,
                command.fact.available_at,
            )
            after = unit_of_work.stage_settlement(
                before=state,
                fact=command.fact,
                classification=classification,
                void_term_content_hash=command.void_term_content_hash,
                settled_position=applied.position,
                settlement_result=applied.result,
                settlement_journal=settlement_journal,
                recorded_at=recorded_at,
                fault=lambda point: self._raise_at(command, point),
            )
            self._raise_at(command, SettlementFaultPoint.BEFORE_COMMIT.value)
            unit_of_work.commit()
            return DurableSettlementResult(after, settlement_journal, False)


# The v0.5 names remain aliases so committed durable-NO callers retain their
# exact surface while both outcomes share the one transaction boundary.
InjectedNoSettlementFault = InjectedSettlementFault
NoSettlementFaultPoint = SettlementFaultPoint
SettlePartialFillCanceledNoCommand = SettlePartialFillCanceledCommand
DurableNoSettlementState = DurableSettlementState
DurableNoSettlementResult = DurableSettlementResult
DurablePartialFillCanceledNoSettlementService = DurablePartialFillCanceledSettlementService


def no_settlement_fact_id(campaign_id: str, fact: RuleBoundSettlementFact) -> str:
    """Preserve the committed NO fact identity for pre-0005 evidence."""

    return settlement_fact_id(campaign_id, fact, SettlementOutcome.NO, None)
