"""Idempotent orchestration for retained Total Market context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.shared_opportunity import (
    EligibilityDecision,
    HypotheticalAllocationProposal,
    OpportunityExplanation,
    PriorityResult,
    SharedOpportunityProjection,
    SharedOpportunitySet,
)
from apex.domain.total_market_context import (
    ContextComponent,
    ContextEnrichedOpportunitySet,
    ContextSourceBundle,
    OpportunityContextAttachment,
    TotalMarketContextError,
    TotalMarketContextSnapshot,
    UnavailableContextComponent,
    attach_context,
    build_context_enriched_set,
    build_context_snapshot,
)
from apex.domain.values import content_digest


class TotalMarketContextSource(Protocol):
    def load(self, *, source_batch_id: str, as_of: datetime) -> ContextSourceBundle: ...


class TotalMarketContextRepository(Protocol):
    def context_snapshot(self, snapshot_id: str) -> TotalMarketContextSnapshot | None: ...
    def context_components(self, snapshot_id: str) -> tuple[ContextComponent, ...]: ...
    def save_context_snapshot(
        self,
        *,
        idempotency_key: str,
        snapshot: TotalMarketContextSnapshot,
        components: tuple[ContextComponent, ...],
    ) -> bool: ...
    def shared_opportunity_set(self, opportunity_set_id: str) -> SharedOpportunitySet | None: ...
    def shared_projections(
        self, opportunity_set_id: str
    ) -> tuple[SharedOpportunityProjection, ...]: ...
    def shared_eligibility(self, opportunity_set_id: str) -> tuple[EligibilityDecision, ...]: ...
    def shared_priorities(self, opportunity_set_id: str) -> tuple[PriorityResult, ...]: ...
    def shared_explanations(
        self, opportunity_set_id: str
    ) -> tuple[OpportunityExplanation, ...]: ...
    def shared_allocation(
        self, opportunity_set_id: str
    ) -> HypotheticalAllocationProposal | None: ...
    def save_context_enriched_set(
        self,
        *,
        idempotency_key: str,
        enriched_set: ContextEnrichedOpportunitySet,
        attachments: tuple[OpportunityContextAttachment, ...],
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ContextSnapshotResult:
    snapshot: TotalMarketContextSnapshot
    components: tuple[ContextComponent, ...]
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class ContextEnrichmentResult:
    enriched_set: ContextEnrichedOpportunitySet
    attachments: tuple[OpportunityContextAttachment, ...]
    idempotent_replay: bool


class MaterializeTotalMarketContextSnapshot:
    def __init__(self, repository: TotalMarketContextRepository, source: TotalMarketContextSource):
        self._repository = repository
        self._source = source

    def execute(
        self,
        *,
        snapshot_id: str,
        source_batch_id: str,
        as_of: datetime,
        idempotency_key: str,
    ) -> ContextSnapshotResult:
        bundle = self._source.load(source_batch_id=source_batch_id, as_of=as_of)
        components = tuple(bundle.components)
        unavailable = tuple(bundle.unavailable_components)
        if not all(isinstance(item, ContextComponent) for item in components) or not all(
            isinstance(item, UnavailableContextComponent) for item in unavailable
        ):
            raise TotalMarketContextError("TOTAL_MARKET_SOURCE_BUNDLE_INVALID")
        snapshot = build_context_snapshot(
            snapshot_id=snapshot_id,
            created_at=as_of,
            source_batch_id=source_batch_id,
            components=components,
            unavailable_components=unavailable,
        )
        replay = self._repository.save_context_snapshot(
            idempotency_key=idempotency_key,
            snapshot=snapshot,
            components=components,
        )
        return ContextSnapshotResult(snapshot, components, replay)


class AttachTotalMarketContext:
    def __init__(self, repository: TotalMarketContextRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        enriched_set_id: str,
        opportunity_set_id: str,
        context_snapshot_id: str,
        attached_at: datetime,
        idempotency_key: str,
    ) -> ContextEnrichmentResult:
        opportunity_set = self._repository.shared_opportunity_set(opportunity_set_id)
        if opportunity_set is None:
            raise TotalMarketContextError("TOTAL_MARKET_SHARED_SET_NOT_FOUND")
        snapshot = self._repository.context_snapshot(context_snapshot_id)
        if snapshot is None:
            raise TotalMarketContextError("TOTAL_MARKET_CONTEXT_SNAPSHOT_NOT_FOUND")
        components = self._repository.context_components(context_snapshot_id)
        projections = self._repository.shared_projections(opportunity_set_id)
        decisions = self._repository.shared_eligibility(opportunity_set_id)
        priorities = self._repository.shared_priorities(opportunity_set_id)
        source_explanations = self._repository.shared_explanations(opportunity_set_id)
        allocation = self._repository.shared_allocation(opportunity_set_id)
        if tuple(item.projection_id for item in projections) != opportunity_set.projection_ids:
            raise TotalMarketContextError("TOTAL_MARKET_SHARED_PROJECTION_LINEAGE_INCOMPLETE")
        if len(source_explanations) != len(projections):
            raise TotalMarketContextError("TOTAL_MARKET_SOURCE_EXPLANATION_LINEAGE_INCOMPLETE")
        attachments = tuple(
            attach_context(
                enriched_set_id=enriched_set_id,
                projection=projection,
                snapshot=snapshot,
                components=components,
                attached_at=attached_at,
            )
            for projection in projections
        )
        eligibility_hash = content_digest(tuple(item.canonical_hash for item in decisions))
        priority_hash = content_digest(tuple(item.canonical_hash for item in priorities))
        enriched = build_context_enriched_set(
            enriched_set_id=enriched_set_id,
            original_opportunity_set_id=opportunity_set.opportunity_set_id,
            original_opportunity_set_hash=opportunity_set.canonical_hash,
            original_allocation_id=None if allocation is None else allocation.proposal_id,
            original_allocation_hash=None if allocation is None else allocation.canonical_hash,
            context_snapshot_id=snapshot.snapshot_id,
            attachments=attachments,
            eligibility_hash=eligibility_hash,
            priority_hash=priority_hash,
            created_at=attached_at,
        )
        replay = self._repository.save_context_enriched_set(
            idempotency_key=idempotency_key,
            enriched_set=enriched,
            attachments=attachments,
        )
        return ContextEnrichmentResult(enriched, attachments, replay)
