"""Read-only retained evidence source for the first generated method."""

from __future__ import annotations

from sqlalchemy.engine import Engine

from apex.adapters.cross_venue_allocation_postgres import (
    PostgresCrossVenueAllocationRepository,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.adapters.total_market_context_postgres import PostgresTotalMarketContextRepository
from apex.domain.first_generated_research_method import CurrentOpportunityOperationalEvidence
from apex.domain.research_operations import ResearchOperationsError


class PostgresCurrentOpportunityOperationalEvidenceSource:
    """Project only the latest exact retained allocation lineage; make no provider call."""

    def __init__(self, engine: Engine) -> None:
        self._shared = PostgresSharedOpportunityRepository(engine)
        self._context = PostgresTotalMarketContextRepository(engine)
        self._allocation = PostgresCrossVenueAllocationRepository(engine)

    def evidence(self) -> CurrentOpportunityOperationalEvidence:
        decisions = self._allocation.decisions()
        if not decisions:
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_ALLOCATION_EVIDENCE_MISSING")
        allocation = decisions[-1]
        shared = self._shared.opportunity_set(allocation.opportunity_set_id)
        enriched = self._context.enriched_set(allocation.enriched_set_id)
        if shared is None or enriched is None:
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_RETAINED_LINEAGE_INCOMPLETE")
        projections = tuple(
            projection
            for projection_id in shared.projection_ids
            if (projection := self._shared.projection(projection_id)) is not None
        )
        if len(projections) != len(shared.projection_ids):
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_SOURCE_PROJECTION_MISSING")
        eligibility = self._shared.eligibility_decisions(shared.opportunity_set_id)
        explanations = self._allocation.explanations(allocation.decision_id)
        blockers = tuple(
            sorted(
                {
                    *(reason for decision in eligibility for reason in decision.reasons),
                    *(
                        reason
                        for explanation in explanations
                        for reason in explanation.excluded_reasons
                    ),
                }
            )
        )
        return CurrentOpportunityOperationalEvidence(
            source_projection_ids=tuple(value.projection_id for value in projections),
            source_projection_hashes=tuple(value.canonical_hash for value in projections),
            shared_set_id=shared.opportunity_set_id,
            shared_set_hash=shared.canonical_hash,
            context_set_id=enriched.enriched_set_id,
            context_set_hash=enriched.canonical_hash,
            allocation_id=allocation.decision_id,
            allocation_hash=allocation.canonical_hash,
            source_opportunity_count=len(projections),
            shared_eligible_count=len(allocation.eligible_projection_ids),
            allocated_line_count=len(allocation.allocation_line_ids),
            paper_ready_proposal_ids=allocation.paper_trial_proposal_ids,
            blockers=blockers,
        )
