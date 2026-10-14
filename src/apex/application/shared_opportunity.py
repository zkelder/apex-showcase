"""Application services for exact-ID shared Opportunity materialization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.shared_opportunity import (
    EligibilityDecision,
    HypotheticalAllocationProposal,
    OpportunityExplanation,
    PriorityResult,
    SharedOpportunityError,
    SharedOpportunityProjection,
    SharedOpportunitySet,
    build_opportunity_set,
    propose_hypothetical_allocation,
)


class SharedOpportunityRepository(Protocol):
    def projection(self, projection_id: str) -> SharedOpportunityProjection | None: ...

    def opportunity_set(self, opportunity_set_id: str) -> SharedOpportunitySet | None: ...

    def priorities(self, opportunity_set_id: str) -> tuple[PriorityResult, ...]: ...

    def save_opportunity_bundle(
        self,
        *,
        idempotency_key: str,
        opportunity_set: SharedOpportunitySet,
        projections: tuple[SharedOpportunityProjection, ...],
        decisions: tuple[EligibilityDecision, ...],
        priorities: tuple[PriorityResult, ...],
        explanations: tuple[OpportunityExplanation, ...],
    ) -> bool: ...

    def allocation(self, proposal_id: str) -> HypotheticalAllocationProposal | None: ...

    def save_allocation(
        self, *, idempotency_key: str, proposal: HypotheticalAllocationProposal
    ) -> bool: ...


class SharedOpportunitySource(Protocol):
    def crypto_projections(
        self, *, pulse_id: str, as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]: ...

    def kalshi_projections(
        self, *, evaluation_ids: tuple[str, ...], as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]: ...

    def securities_projections(self, *, as_of: datetime) -> tuple[SharedOpportunityProjection, ...]:
        """Securities Research -> Shared Opportunity Recurring Bridge v1. No
        explicit id parameter (unlike ``crypto_pulse_id``/``kalshi_
        evaluation_ids``): the source resolves "the current trading
        session's retained structures" itself from already-retained
        evidence -- there is no equivalent of a crypto pulse id or a Kalshi
        evaluation id to resolve upstream in the recurring cycle, since this
        producer never depends on any *other* task's output identity (see
        ``adapters.shared_opportunity_sources.RetainedSharedOpportunitySource.
        securities_projections`` for the full contract)."""
        ...


@dataclass(frozen=True, slots=True)
class OpportunitySetMaterialization:
    opportunity_set: SharedOpportunitySet
    projections: tuple[SharedOpportunityProjection, ...]
    decisions: tuple[EligibilityDecision, ...]
    priorities: tuple[PriorityResult, ...]
    explanations: tuple[OpportunityExplanation, ...]
    idempotent_replay: bool


class MaterializeSharedOpportunitySet:
    def __init__(
        self, repository: SharedOpportunityRepository, source: SharedOpportunitySource
    ) -> None:
        self._repository = repository
        self._source = source

    def execute(
        self,
        *,
        opportunity_set_id: str,
        crypto_pulse_id: str,
        kalshi_evaluation_ids: tuple[str, ...],
        as_of: datetime,
        idempotency_key: str,
    ) -> OpportunitySetMaterialization:
        if not idempotency_key.strip() or len(idempotency_key) > 160:
            raise SharedOpportunityError("SHARED_OPPORTUNITY_IDEMPOTENCY_KEY_INVALID")
        projections = (
            *self._source.crypto_projections(pulse_id=crypto_pulse_id, as_of=as_of),
            *self._source.kalshi_projections(evaluation_ids=kalshi_evaluation_ids, as_of=as_of),
            *self._source.securities_projections(as_of=as_of),
        )
        if len({item.projection_id for item in projections}) != len(projections):
            raise SharedOpportunityError("SHARED_OPPORTUNITY_PROJECTION_ID_CONFLICT")
        opportunity_set, decisions, priorities, explanations = build_opportunity_set(
            opportunity_set_id=opportunity_set_id,
            created_at=as_of,
            projections=tuple(projections),
        )
        replay = self._repository.save_opportunity_bundle(
            idempotency_key=idempotency_key,
            opportunity_set=opportunity_set,
            projections=tuple(projections),
            decisions=decisions,
            priorities=priorities,
            explanations=explanations,
        )
        return OpportunitySetMaterialization(
            opportunity_set,
            tuple(projections),
            decisions,
            priorities,
            explanations,
            replay,
        )


@dataclass(frozen=True, slots=True)
class AllocationMaterialization:
    proposal: HypotheticalAllocationProposal
    idempotent_replay: bool


class ProposeSharedResearchAllocation:
    def __init__(self, repository: SharedOpportunityRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        proposal_id: str,
        opportunity_set_id: str,
        idempotency_key: str,
    ) -> AllocationMaterialization:
        if not idempotency_key.strip() or len(idempotency_key) > 160:
            raise SharedOpportunityError("SHARED_ALLOCATION_IDEMPOTENCY_KEY_INVALID")
        opportunity_set = self._repository.opportunity_set(opportunity_set_id)
        if opportunity_set is None:
            raise SharedOpportunityError("SHARED_OPPORTUNITY_SET_NOT_FOUND")
        projections = tuple(
            value
            for projection_id in opportunity_set.projection_ids
            if (value := self._repository.projection(projection_id)) is not None
        )
        if len(projections) != len(opportunity_set.projection_ids):
            raise SharedOpportunityError("SHARED_OPPORTUNITY_SET_LINEAGE_MISSING")
        proposal = propose_hypothetical_allocation(
            proposal_id=proposal_id,
            opportunity_set=opportunity_set,
            projections=projections,
            priorities=self._repository.priorities(opportunity_set_id),
        )
        replay = self._repository.save_allocation(
            idempotency_key=idempotency_key, proposal=proposal
        )
        return AllocationMaterialization(proposal, replay)
