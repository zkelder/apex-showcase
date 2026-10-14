"""Exact-ID orchestration for immutable cross-venue allocation evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.cross_venue_allocation import (
    AllocationConstraintExplanation,
    CrossVenueAllocationDecision,
    CrossVenueAllocationError,
    CrossVenueAllocationLine,
    CrossVenueAllocationPolicy,
    PaperTrialProposal,
    VenuePaperReadiness,
    allocate_cross_venue,
)
from apex.domain.shared_opportunity import (
    EligibilityDecision,
    PriorityResult,
    SharedOpportunityProjection,
    SharedOpportunitySet,
)
from apex.domain.total_market_context import (
    ContextEnrichedOpportunitySet,
    TotalMarketContextSnapshot,
)


class CrossVenueAllocationRepository(Protocol):
    def policy(self, policy_id: str) -> CrossVenueAllocationPolicy | None: ...
    def decision(self, decision_id: str) -> CrossVenueAllocationDecision | None: ...
    def decision_for_enriched_set(
        self, enriched_set_id: str
    ) -> CrossVenueAllocationDecision | None: ...
    def lines(self, decision_id: str) -> tuple[CrossVenueAllocationLine, ...]: ...
    def explanations(self, decision_id: str) -> tuple[AllocationConstraintExplanation, ...]: ...
    def proposals(self, decision_id: str) -> tuple[PaperTrialProposal, ...]: ...
    def save_bundle(
        self,
        *,
        idempotency_key: str,
        policy: CrossVenueAllocationPolicy,
        decision: CrossVenueAllocationDecision,
        lines: tuple[CrossVenueAllocationLine, ...],
        explanations: tuple[AllocationConstraintExplanation, ...],
        proposals: tuple[PaperTrialProposal, ...],
    ) -> bool: ...


class CrossVenueAllocationSource(Protocol):
    def enriched_set(self, enriched_set_id: str) -> ContextEnrichedOpportunitySet | None: ...
    def context_snapshot(self, snapshot_id: str) -> TotalMarketContextSnapshot | None: ...
    def opportunity_set(self, opportunity_set_id: str) -> SharedOpportunitySet | None: ...
    def projections(self, opportunity_set_id: str) -> tuple[SharedOpportunityProjection, ...]: ...
    def eligibility(self, opportunity_set_id: str) -> tuple[EligibilityDecision, ...]: ...
    def priorities(self, opportunity_set_id: str) -> tuple[PriorityResult, ...]: ...
    def paper_readiness(self) -> tuple[VenuePaperReadiness, ...]: ...


@dataclass(frozen=True, slots=True)
class CrossVenueAllocationResult:
    policy: CrossVenueAllocationPolicy
    decision: CrossVenueAllocationDecision
    lines: tuple[CrossVenueAllocationLine, ...]
    explanations: tuple[AllocationConstraintExplanation, ...]
    proposals: tuple[PaperTrialProposal, ...]
    idempotent_replay: bool


class MaterializeCrossVenueAllocation:
    def __init__(
        self,
        repository: CrossVenueAllocationRepository,
        source: CrossVenueAllocationSource,
    ) -> None:
        self._repository = repository
        self._source = source

    def execute(
        self,
        *,
        decision_id: str,
        enriched_set_id: str,
        decided_at: datetime,
        idempotency_key: str,
    ) -> CrossVenueAllocationResult:
        if not idempotency_key.strip() or len(idempotency_key) > 160:
            raise CrossVenueAllocationError("CROSS_VENUE_IDEMPOTENCY_KEY_INVALID")
        enriched = self._source.enriched_set(enriched_set_id)
        if enriched is None:
            raise CrossVenueAllocationError("CROSS_VENUE_ENRICHED_SET_NOT_FOUND")
        context = self._source.context_snapshot(enriched.context_snapshot_id)
        if context is None:
            raise CrossVenueAllocationError("CROSS_VENUE_CONTEXT_NOT_FOUND")
        opportunity_set = self._source.opportunity_set(enriched.original_opportunity_set_id)
        if opportunity_set is None:
            raise CrossVenueAllocationError("CROSS_VENUE_OPPORTUNITY_SET_NOT_FOUND")
        decision, lines, explanations, proposals = allocate_cross_venue(
            decision_id=decision_id,
            opportunity_set=opportunity_set,
            enriched_set=enriched,
            context=context,
            projections=self._source.projections(opportunity_set.opportunity_set_id),
            eligibility=self._source.eligibility(opportunity_set.opportunity_set_id),
            priorities=self._source.priorities(opportunity_set.opportunity_set_id),
            paper_readiness=self._source.paper_readiness(),
            decided_at=decided_at,
        )
        policy = CrossVenueAllocationPolicy.v1()
        replay = self._repository.save_bundle(
            idempotency_key=idempotency_key,
            policy=policy,
            decision=decision,
            lines=lines,
            explanations=explanations,
            proposals=proposals,
        )
        return CrossVenueAllocationResult(policy, decision, lines, explanations, proposals, replay)
