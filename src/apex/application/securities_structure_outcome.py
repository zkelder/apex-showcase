"""Application orchestration for Securities Structure Forward Performance v1.

Reads currently-retained counterfactual/follow-up evidence through the
identical, already-established, read-only Protocol seam ``application.
counterfactual_outcome.CounterfactualOutcomeEvidenceSource`` (no new source
Protocol needed: every read this service requires -- decisions,
observations, assessments, and projections resolved by id -- that Protocol
already exposes), delegates all aggregation logic to the pure ``domain.
securities_structure_outcome.build_securities_structure_outcome_aggregates``
policy, and persists each resulting aggregate idempotently. Performs no
provider I/O of its own -- every read the source Protocol exposes is
already-retained, local evidence; this service creates no new investment
judgment and mutates no historical row."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.application.counterfactual_outcome import CounterfactualOutcomeEvidenceSource
from apex.domain.securities_structure_outcome import (
    CURRENT_SECURITIES_STRUCTURE_EVIDENCE_QUALITY_POLICY_VERSION,
    SECURITIES_STRUCTURE_OUTCOME_IDENTITY_CONFLICT,
    SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
    SecuritiesStructureOutcomeAggregate,
    SecuritiesStructureOutcomeError,
    SecuritiesStructureOutcomeQualificationState,
    build_securities_structure_outcome_aggregates,
)
from apex.domain.shared_opportunity import SharedVenue


class SecuritiesStructureOutcomeRepository(Protocol):
    def save_aggregate(self, *, aggregate: SecuritiesStructureOutcomeAggregate) -> bool: ...

    def aggregate(self, aggregate_id: str) -> SecuritiesStructureOutcomeAggregate | None:
        """Read back one already-retained aggregate by id. Used only to
        classify a ``save_aggregate`` identity conflict -- see
        ``_is_benign_evidence_maturation`` in ``application.
        counterfactual_outcome`` (this module mirrors that one exactly)."""
        ...


@dataclass(frozen=True, slots=True)
class SecuritiesStructureOutcomeRefreshResult:
    run_id: str
    generated_at: datetime
    considered_count: int
    newly_written_count: int
    idempotent_replay_count: int
    qualified_count: int
    insufficient_sample_count: int
    stale_identity_superseded_count: int
    aggregate_ids: tuple[str, ...]


def _is_benign_evidence_maturation(
    *,
    existing: SecuritiesStructureOutcomeAggregate | None,
    fresh: SecuritiesStructureOutcomeAggregate,
) -> bool:
    """Mirrors ``application.counterfactual_outcome._is_benign_evidence_
    maturation`` exactly, for the identical reason: this sibling pass
    shares the same same-``run_id``-retry-with-maturing-evidence exposure
    (production-confirmed 2026-09-02: 2 of that day's 8 affected cohorts
    were SECURITIES ones, from this exact pass). See that function's own
    docstring for the full reasoning; deliberately not re-derived here."""

    if existing is None:
        return False
    return (
        existing.cohort_id == fresh.cohort_id
        and existing.run_id == fresh.run_id
        and existing.total_decisions_considered == fresh.total_decisions_considered
        and existing.mature_observation_count <= fresh.mature_observation_count
        and existing.eligible_observation_count <= fresh.eligible_observation_count
        and set(existing.contributing_decision_ids) <= set(fresh.contributing_decision_ids)
    )


class RefreshSecuritiesStructureOutcomeAggregates:
    """Recompute every non-empty SECURITIES structure forward-performance
    cohort from currently-retained evidence and persist the result
    idempotently. Safe to call repeatedly with the same ``run_id`` (pure
    replay, no new rows) and safe to call again later with a new ``run_id``
    as more evidence accumulates (adds new, independent rows; never
    mutates or removes an earlier run's). Mirrors ``application.
    counterfactual_outcome.RefreshCounterfactualOutcomeAggregates``
    exactly."""

    def __init__(
        self,
        source: CounterfactualOutcomeEvidenceSource,
        repository: SecuritiesStructureOutcomeRepository,
    ) -> None:
        self._source = source
        self._repository = repository

    def execute(
        self,
        *,
        run_id: str,
        generated_at: datetime,
        evidence_quality_policy_version: str = (
            CURRENT_SECURITIES_STRUCTURE_EVIDENCE_QUALITY_POLICY_VERSION
        ),
        outcome_policy_version: str = SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
    ) -> SecuritiesStructureOutcomeRefreshResult:
        decisions = self._source.all_decisions()
        observations = self._source.all_observations()
        assessments = self._source.all_assessments()
        projection_ids = {decision.subject_projection_id for decision in decisions}
        projections = self._source.projections_by_id(projection_ids)
        strategy_or_method_id_by_projection = {
            projection_id: projection.strategy_or_method_id
            for projection_id, projection in projections.items()
        }
        # ``direction`` is ``SharedOpportunityProjection``'s one venue-
        # neutral structural-family field (for SECURITIES, stamped
        # verbatim from ``OptionStructureCandidate.structure_type.value``
        # by ``adapters.shared_opportunity_sources._securities_structure_
        # projection`` -- see that module). Resolved for every projection,
        # not only SECURITIES ones: the pure domain builder already filters
        # to SECURITIES/ADVANCED/eligibility-origin decisions before this
        # mapping is ever consulted, so no filtering is needed here either.
        structure_type_by_projection = {
            projection_id: projection.direction
            for projection_id, projection in projections.items()
            if projection.venue is SharedVenue.SECURITIES
        }
        aggregates = build_securities_structure_outcome_aggregates(
            decisions=decisions,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection=strategy_or_method_id_by_projection,
            structure_type_by_projection=structure_type_by_projection,
            run_id=run_id,
            generated_at=generated_at,
            evidence_quality_policy_version=evidence_quality_policy_version,
            outcome_policy_version=outcome_policy_version,
        )
        aggregate_ids: list[str] = []
        replay_count = 0
        qualified = 0
        insufficient = 0
        stale_identity_superseded = 0
        for aggregate in aggregates:
            try:
                replay = self._repository.save_aggregate(aggregate=aggregate)
            except SecuritiesStructureOutcomeError as error:
                # Counterfactual Outcome Refresh Recovery v1: mirrors
                # ``application.counterfactual_outcome.
                # RefreshCounterfactualOutcomeAggregates.execute``'s
                # identical, identically-narrow isolation exactly -- see
                # that method's own comment for the full reasoning. Only
                # the one, already-understood identity-conflict shape that
                # same-day evidence maturation between two dispatch
                # attempts genuinely produces is isolated; a payload-shape
                # defect, a genuine persistence/infrastructure failure, or
                # an identity conflict NOT explained by monotonic evidence
                # growth still propagates and fails this pass genuinely.
                if str(error) != SECURITIES_STRUCTURE_OUTCOME_IDENTITY_CONFLICT or (
                    not _is_benign_evidence_maturation(
                        existing=self._repository.aggregate(aggregate.aggregate_id), fresh=aggregate
                    )
                ):
                    raise
                stale_identity_superseded += 1
                continue
            aggregate_ids.append(aggregate.aggregate_id)
            replay_count += 1 if replay else 0
            qualified_state = SecuritiesStructureOutcomeQualificationState.QUALIFIED
            if aggregate.qualification_state is qualified_state:
                qualified += 1
            else:
                insufficient += 1
        return SecuritiesStructureOutcomeRefreshResult(
            run_id=run_id,
            generated_at=generated_at,
            considered_count=len(aggregates),
            newly_written_count=len(aggregates) - replay_count - stale_identity_superseded,
            idempotent_replay_count=replay_count,
            qualified_count=qualified,
            insufficient_sample_count=insufficient,
            stale_identity_superseded_count=stale_identity_superseded,
            aggregate_ids=tuple(aggregate_ids),
        )
