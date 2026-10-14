"""Application orchestration for Counterfactual Outcome Feedback v1.

Reads currently-retained counterfactual/follow-up evidence through a
narrow, read-only Protocol seam, delegates all aggregation logic to the
pure ``domain.counterfactual_outcome.build_counterfactual_outcome_
aggregates`` policy, and persists each resulting aggregate idempotently.
Performs no provider I/O of its own -- every read the source Protocol
exposes is already-retained, local evidence; this service creates no new
investment judgment and mutates no historical row."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.counterfactual_classification import (
    CURRENT_CLASSIFIER_VERSION,
    CounterfactualDecisionClassification,
)
from apex.domain.counterfactual_followup import (
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    DecisionFollowUpObservation,
    FollowUpEvidenceQualityAssessment,
)
from apex.domain.counterfactual_ledger import CounterfactualDecision
from apex.domain.counterfactual_outcome import (
    COUNTERFACTUAL_OUTCOME_IDENTITY_CONFLICT,
    COUNTERFACTUAL_OUTCOME_POLICY_V1,
    COUNTERFACTUAL_OUTCOME_POLICY_V2,
    CounterfactualOutcomeAggregate,
    CounterfactualOutcomeError,
    CounterfactualOutcomeQualificationState,
    SubjectProjectionFacts,
    build_counterfactual_outcome_aggregates,
)
from apex.domain.kalshi_baseline_quote import KalshiBaselineQuoteEvidence
from apex.domain.shared_opportunity import SharedOpportunityProjection


class CounterfactualOutcomeEvidenceSource(Protocol):
    def all_decisions(self) -> tuple[CounterfactualDecision, ...]: ...

    def all_classifications(self) -> tuple[CounterfactualDecisionClassification, ...]: ...

    def all_observations(self) -> tuple[DecisionFollowUpObservation, ...]: ...

    def all_assessments(self) -> tuple[FollowUpEvidenceQualityAssessment, ...]: ...

    def all_kalshi_baseline_quotes(self) -> tuple[KalshiBaselineQuoteEvidence, ...]:
        """Ignored by ``RefreshCounterfactualOutcomeAggregates`` under
        ``COUNTERFACTUAL_OUTCOME_POLICY_V1`` (its default); consumed only
        under ``COUNTERFACTUAL_OUTCOME_POLICY_V2``."""
        ...

    def projections_by_id(
        self, projection_ids: Collection[str]
    ) -> Mapping[str, SharedOpportunityProjection]: ...


class CounterfactualOutcomeRepository(Protocol):
    def save_aggregate(self, *, aggregate: CounterfactualOutcomeAggregate) -> bool: ...

    def aggregate(self, aggregate_id: str) -> CounterfactualOutcomeAggregate | None:
        """Read back one already-retained aggregate by id. Used by
        ``RefreshCounterfactualOutcomeAggregates`` only to classify a
        ``save_aggregate`` identity conflict (see
        ``_is_benign_evidence_maturation`` below) -- never to decide
        whether to write in the first place."""
        ...


@dataclass(frozen=True, slots=True)
class CounterfactualOutcomeRefreshResult:
    run_id: str
    generated_at: datetime
    considered_count: int
    newly_written_count: int
    idempotent_replay_count: int
    qualified_count: int
    insufficient_sample_count: int
    venue_unavailable_count: int
    stale_identity_superseded_count: int
    aggregate_ids: tuple[str, ...]


def _is_benign_evidence_maturation(
    *, existing: CounterfactualOutcomeAggregate | None, fresh: CounterfactualOutcomeAggregate
) -> bool:
    """Counterfactual Outcome Refresh Recovery v1. Whether a ``save_aggregate``
    identity conflict on ``fresh.aggregate_id`` is fully explained by this
    cohort's follow-up evidence maturing further between two dispatch
    attempts of the identical, day-scoped ``run_id`` -- expected whenever
    this task's own declared same-cycle dependency,
    ``COUNTERFACTUAL_FOLLOWUP_REFRESH``, keeps advancing (it uses intraday
    continuation) between an earlier attempt that already persisted this
    cohort and a later same-day retry of this task (e.g. recovering from an
    unrelated crash elsewhere in the same dispatch, downstream of the V2/
    SECURITIES passes this task also attempts). Confirmed as the real
    production mechanism, not a hypothetical: 8 of 153 cohorts hit exactly
    this shape on 2026-09-02 after an unrelated ``kalshi_baseline_quote.
    quote_kind`` schema-migration-timing crash forced a second same-day
    dispatch attempt while follow-up sync kept maturing more decisions in
    between.

    Deliberately narrow and never a blanket "any mismatch is fine": this
    only recognizes a strict, monotonic superset of the already-persisted
    row (identical cohort identity and decision population, follow-up
    maturity/eligibility only ever growing, no previously-contributing
    decision ever disappearing). Anything else -- a shrinking count, a
    changed population, a contributing decision going missing -- is left
    for the caller to raise on, exactly as before this milestone, since
    that shape is not explained by ordinary evidence maturation and may be
    a genuine non-determinism or data-integrity defect this check must
    never paper over."""

    if existing is None:
        return False
    # Counterfactual Outcome Evidence Specificity v3: a V3 aggregate's hash
    # can change with identical decision counts (a window's complement
    # growing as the follow-up backlog drains), so the monotonic-superset
    # test extends to the V3 block: raw observations, informative windows,
    # and accepted independent windows may only grow.  A V3/non-V3 mismatch
    # under one aggregate id is never benign.
    if (existing.specificity is None) != (fresh.specificity is None):
        return False
    if (
        existing.specificity is not None
        and fresh.specificity is not None
        and (
            existing.specificity.raw_return_observation_count
            > fresh.specificity.raw_return_observation_count
            or existing.specificity.descriptive_informative_window_count
            > fresh.specificity.descriptive_informative_window_count
            or existing.specificity.independent_window_count
            > fresh.specificity.independent_window_count
        )
    ):
        return False
    return (
        existing.cohort_id == fresh.cohort_id
        and existing.run_id == fresh.run_id
        and existing.total_observations_considered == fresh.total_observations_considered
        and existing.mature_observation_count <= fresh.mature_observation_count
        and existing.eligible_observation_count <= fresh.eligible_observation_count
        and set(existing.contributing_decision_ids) <= set(fresh.contributing_decision_ids)
    )


class RefreshCounterfactualOutcomeAggregates:
    """Recompute every non-empty counterfactual-outcome cohort from
    currently-retained evidence and persist the result idempotently.
    Safe to call repeatedly with the same ``run_id`` (pure replay, no new
    rows) and safe to call again later with a new ``run_id`` as more
    evidence accumulates (adds new, independent rows; never mutates or
    removes an earlier run's)."""

    def __init__(
        self,
        source: CounterfactualOutcomeEvidenceSource,
        repository: CounterfactualOutcomeRepository,
    ) -> None:
        self._source = source
        self._repository = repository

    def execute(
        self,
        *,
        run_id: str,
        generated_at: datetime,
        classifier_version: str = CURRENT_CLASSIFIER_VERSION,
        evidence_quality_policy_version: str = FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        outcome_policy_version: str = COUNTERFACTUAL_OUTCOME_POLICY_V1,
    ) -> CounterfactualOutcomeRefreshResult:
        decisions = self._source.all_decisions()
        classifications = self._source.all_classifications()
        observations = self._source.all_observations()
        assessments = self._source.all_assessments()
        projection_ids = {decision.subject_projection_id for decision in decisions}
        projections = self._source.projections_by_id(projection_ids)
        strategy_or_method_id_by_projection = {
            projection_id: projection.strategy_or_method_id
            for projection_id, projection in projections.items()
        }
        # Counterfactual Outcome Evidence Specificity v3: the three facts the
        # V3 build needs from each decision's own projection, read from the
        # identical lookup V1 already performs -- no second query.  V1/V2
        # ignore it entirely.
        subject_facts_by_projection = {
            projection_id: SubjectProjectionFacts(
                strategy_or_method_id=projection.strategy_or_method_id,
                product_or_market_id=projection.product_or_market_id,
                generated_at=projection.generated_at,
            )
            for projection_id, projection in projections.items()
        }
        # Only read under V2: V1 never consumes this evidence at all (see
        # ``build_counterfactual_outcome_aggregates``'s own docstring), so
        # reading it unconditionally would be a pure-waste query on every
        # V1 call site -- of which there are many, including every existing
        # one that predates this milestone.
        kalshi_baseline_quotes = (
            self._source.all_kalshi_baseline_quotes()
            if outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V2
            else ()
        )
        aggregates = build_counterfactual_outcome_aggregates(
            decisions=decisions,
            classifications=classifications,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection=strategy_or_method_id_by_projection,
            run_id=run_id,
            generated_at=generated_at,
            classifier_version=classifier_version,
            evidence_quality_policy_version=evidence_quality_policy_version,
            outcome_policy_version=outcome_policy_version,
            kalshi_baseline_quotes=kalshi_baseline_quotes,
            subject_facts_by_projection=subject_facts_by_projection,
        )
        aggregate_ids: list[str] = []
        replay_count = 0
        qualified = 0
        insufficient = 0
        unavailable = 0
        stale_identity_superseded = 0
        for aggregate in aggregates:
            try:
                replay = self._repository.save_aggregate(aggregate=aggregate)
            except CounterfactualOutcomeError as error:
                # Counterfactual Outcome Refresh Recovery v1: isolate only
                # the one, narrow, already-understood identity-conflict
                # shape that same-day evidence maturation between two
                # dispatch attempts of this task genuinely produces (see
                # ``_is_benign_evidence_maturation``'s own docstring) -- one
                # cohort hitting it must never abort every other cohort's
                # otherwise-unaffected replay/write this run, exactly the
                # structural gap the sibling Kalshi Baseline Temporal
                # Semantics & Sweep Isolation v1 milestone found and fixed
                # for its own sweep. Deliberately not a blanket
                # ``except Exception`` and not even every
                # ``CounterfactualOutcomeError``: a payload-shape defect
                # (``COUNTERFACTUAL_OUTCOME_RETAINED_PAYLOAD_INVALID``/
                # ``..._RETAINED_COLLECTION_INVALID``), a genuine
                # persistence/infrastructure failure
                # (``COUNTERFACTUAL_OUTCOME_PERSISTENCE_CONFLICT``, a real
                # ``IntegrityError``), or an identity conflict that is NOT
                # explained by monotonic evidence growth still propagates
                # immediately and fails this task genuinely, exactly as
                # before this milestone.
                if str(error) != COUNTERFACTUAL_OUTCOME_IDENTITY_CONFLICT or (
                    not _is_benign_evidence_maturation(
                        existing=self._repository.aggregate(aggregate.aggregate_id), fresh=aggregate
                    )
                ):
                    raise
                stale_identity_superseded += 1
                continue
            aggregate_ids.append(aggregate.aggregate_id)
            replay_count += 1 if replay else 0
            if aggregate.qualification_state is CounterfactualOutcomeQualificationState.QUALIFIED:
                qualified += 1
            elif (
                aggregate.qualification_state
                is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
            ):
                insufficient += 1
            else:
                unavailable += 1
        return CounterfactualOutcomeRefreshResult(
            run_id=run_id,
            generated_at=generated_at,
            considered_count=len(aggregates),
            newly_written_count=len(aggregates) - replay_count - stale_identity_superseded,
            idempotent_replay_count=replay_count,
            qualified_count=qualified,
            insufficient_sample_count=insufficient,
            venue_unavailable_count=unavailable,
            stale_identity_superseded_count=stale_identity_superseded,
            aggregate_ids=tuple(aggregate_ids),
        )
