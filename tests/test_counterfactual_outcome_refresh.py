"""Offline (no Postgres) proofs for ``RefreshCounterfactualOutcomeAggregates``'s
per-aggregate identity-conflict isolation: Counterfactual Outcome Refresh
Recovery v1.

Mirrors ``tests/test_kalshi_baseline_quote_sync.py``'s own established
in-memory-fake pattern for the identical reason: the isolation/classification
logic this milestone adds is pure control flow over already-fetched,
already-immutable evidence -- no database required to prove it. Real
end-to-end proof (real Postgres, real recurring-task executor) lives in
``tests/persistence/test_postgres_counterfactual_outcome_recurring.py``;
these two files intentionally exercise the identical contract from two
different angles (in-memory/Postgres parity), never a competing one.

Root cause this reproduces (confirmed against real production evidence,
2026-09-02): ``COUNTERFACTUAL_OUTCOME_REFRESH``'s V1 pass persisted 153
cohort aggregates for that day's ``run_id``, then an unrelated crash inside
the same-cycle V2 pass (a schema/migration-timing ``ProgrammingError``,
already independently resolved) left no terminal task result, forcing a
second same-day dispatch of this task. Between the two attempts, this
task's own declared same-cycle dependency (``COUNTERFACTUAL_FOLLOWUP_
REFRESH``, which uses intraday continuation) matured more follow-up
evidence for 8 already-persisted cohorts -- same decision population
(``total_observations_considered`` unchanged), more of them now mature.
Recomputing those 8 cohorts under the identical, day-scoped ``run_id``
therefore produced a different ``canonical_hash`` for an already-persisted
``aggregate_id``, and the unguarded ``save_aggregate`` call in this
service's persist loop let that single conflict abort the entire refresh,
failing the whole task with ``error_code=CounterfactualOutcomeError``."""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta

import pytest

from apex.application.counterfactual_outcome import (
    CounterfactualOutcomeAggregate,
    RefreshCounterfactualOutcomeAggregates,
)
from apex.domain.counterfactual_classification import ReasonFamily
from apex.domain.counterfactual_followup import (
    DecisionFollowUpObservation,
    FollowUpEvidenceQualityAssessment,
)
from apex.domain.counterfactual_ledger import CounterfactualDecision
from apex.domain.counterfactual_outcome import (
    COUNTERFACTUAL_OUTCOME_IDENTITY_CONFLICT,
    CounterfactualOutcomeError,
    CounterfactualOutcomeQualificationState,
)
from apex.domain.kalshi_baseline_quote import KalshiBaselineQuoteEvidence
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import content_digest
from tests.test_counterfactual_outcome import _assessment, _classification, _decision, _observation

NOW = datetime(2026, 9, 2, tzinfo=UTC)
METHOD_ID = "CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1"


def _projection(identity: str) -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=f"cor-proj-{identity}",
        venue=SharedVenue.CRYPTO,
        source_record_type="FixtureOpportunity",
        source_record_id=f"cor-source-{identity}",
        source_canonical_hash=content_digest((identity,)),
        strategy_or_method_id=METHOD_ID,
        instrument_id="BTC",
        product_or_market_id="BTC-USD",
        direction="HYPOTHETICAL_LONG_SPOT_RESEARCH",
        generated_at=NOW,
        valid_until=NOW + timedelta(days=60),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=60,
        freshness_limit_seconds=3600,
        expected_gross_return=None,
        expected_net_return=None,
        comparable_value="0.10",
        comparable_value_identity=None,
        uncertainty="0.10",
        maximum_loss="50",
        modeled_fees="1",
        modeled_slippage="1",
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital="100",
        maximum_sensible_research_allocation="200",
        source_maximum_allocation_fraction="0.5",
        expected_holding_days=1,
        estimated_lockup_days=1,
        supporting_evidence_ids=(f"cor-evidence-{identity}",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(),
    )


class _FakeSource:
    """Minimal, in-memory ``CounterfactualOutcomeEvidenceSource``. Mutable
    lists so a test can append more evidence between two ``execute()``
    calls -- exactly mirroring ``COUNTERFACTUAL_FOLLOWUP_REFRESH``'s real
    intraday continuation advancing the retained corpus between two
    same-day dispatch attempts of this task."""

    def __init__(self, *, projection: SharedOpportunityProjection) -> None:
        self.decisions: list[CounterfactualDecision] = []
        self.classifications: list = []
        self.observations: list[DecisionFollowUpObservation] = []
        self.assessments: list[FollowUpEvidenceQualityAssessment] = []
        self._projections = {projection.projection_id: projection}

    def all_decisions(self) -> tuple[CounterfactualDecision, ...]:
        return tuple(self.decisions)

    def all_classifications(self) -> tuple:
        return tuple(self.classifications)

    def all_observations(self) -> tuple[DecisionFollowUpObservation, ...]:
        return tuple(self.observations)

    def all_assessments(self) -> tuple[FollowUpEvidenceQualityAssessment, ...]:
        return tuple(self.assessments)

    def all_kalshi_baseline_quotes(self) -> tuple[KalshiBaselineQuoteEvidence, ...]:
        return ()

    def projections_by_id(
        self, projection_ids: Collection[str]
    ) -> Mapping[str, SharedOpportunityProjection]:
        return {pid: self._projections[pid] for pid in projection_ids if pid in self._projections}


class _FakeRepository:
    """In-memory ``CounterfactualOutcomeRepository``, byte-for-byte the same
    idempotent-replay / identity-conflict contract as
    ``PostgresCounterfactualOutcomeRepository.save_aggregate`` (never a
    simplified stand-in)."""

    def __init__(self) -> None:
        self.by_id: dict[str, CounterfactualOutcomeAggregate] = {}

    def aggregate(self, aggregate_id: str) -> CounterfactualOutcomeAggregate | None:
        return self.by_id.get(aggregate_id)

    def save_aggregate(self, *, aggregate: CounterfactualOutcomeAggregate) -> bool:
        existing = self.by_id.get(aggregate.aggregate_id)
        if existing is not None:
            if existing.canonical_hash != aggregate.canonical_hash:
                raise CounterfactualOutcomeError(COUNTERFACTUAL_OUTCOME_IDENTITY_CONFLICT)
            return True
        self.by_id[aggregate.aggregate_id] = aggregate
        return False


class _AlwaysPersistenceConflictRepository:
    """Simulates a genuine infrastructure failure (the real Postgres
    adapter's own ``COUNTERFACTUAL_OUTCOME_PERSISTENCE_CONFLICT``, wrapping
    an actual ``IntegrityError``) -- must never be isolated, unlike the
    narrow identity-conflict-from-evidence-maturation case."""

    def aggregate(self, aggregate_id: str) -> CounterfactualOutcomeAggregate | None:
        return None

    def save_aggregate(self, *, aggregate: CounterfactualOutcomeAggregate) -> bool:
        raise CounterfactualOutcomeError("COUNTERFACTUAL_OUTCOME_PERSISTENCE_CONFLICT")


def _two_decision_cohort(
    source: _FakeSource, *, projection_id: str, second_decision_matures: bool
) -> None:
    """Seeds a single cohort with two decisions sharing the identical
    (venue, strategy_or_method_id, reason_family) partition -- both
    classified from the start (so both are always counted in
    ``total_observations_considered``), but only the second decision's
    follow-up observation is conditionally present, exactly reproducing the
    production shape: cohort membership stable, maturity growing."""

    decision_1 = _decision(decision_id="cor-decision-1", projection_id=projection_id)
    decision_2 = _decision(decision_id="cor-decision-2", projection_id=projection_id)
    source.decisions = [decision_1, decision_2]
    source.classifications = [
        _classification(
            decision_id=decision_1.decision_id, reason_families=(ReasonFamily.ECONOMICS,)
        ),
        _classification(
            decision_id=decision_2.decision_id, reason_families=(ReasonFamily.ECONOMICS,)
        ),
    ]
    observation_1 = _observation(decision_id=decision_1.decision_id)
    source.observations = [observation_1]
    source.assessments = [_assessment(observation=observation_1)]
    if second_decision_matures:
        observation_2 = _observation(decision_id=decision_2.decision_id)
        source.observations.append(observation_2)
        source.assessments.append(_assessment(observation=observation_2))


# ---------------------------------------------------------------------------
# The production defect, reproduced exactly
# ---------------------------------------------------------------------------


def test_second_dispatch_with_matured_evidence_is_isolated_not_a_task_failure() -> None:
    projection = _projection("refresh-1")
    source = _FakeSource(projection=projection)
    _two_decision_cohort(
        source, projection_id=projection.projection_id, second_decision_matures=False
    )
    repository = _FakeRepository()
    service = RefreshCounterfactualOutcomeAggregates(source, repository)

    first = service.execute(run_id="cycle-1-counterfactual-outcome", generated_at=NOW)
    assert first.newly_written_count == 1
    assert first.stale_identity_superseded_count == 0
    [aggregate_id] = first.aggregate_ids
    persisted_after_first = repository.aggregate(aggregate_id)
    assert persisted_after_first is not None
    assert persisted_after_first.total_observations_considered == 2
    assert persisted_after_first.mature_observation_count == 1

    # COUNTERFACTUAL_FOLLOWUP_REFRESH's own intraday continuation matures
    # the second decision's evidence in between two dispatch attempts of
    # the identical, day-scoped run_id.
    _two_decision_cohort(
        source, projection_id=projection.projection_id, second_decision_matures=True
    )

    second = service.execute(
        run_id="cycle-1-counterfactual-outcome", generated_at=NOW + timedelta(minutes=5)
    )
    assert second.stale_identity_superseded_count == 1
    assert second.newly_written_count == 0
    assert second.idempotent_replay_count == 0
    assert second.aggregate_ids == ()  # nothing genuinely (re)written this call

    # The already-persisted row is untouched -- append-only, never mutated
    # or duplicated in place.
    assert list(repository.by_id) == [aggregate_id]
    unchanged = repository.aggregate(aggregate_id)
    assert unchanged == persisted_after_first
    assert unchanged.mature_observation_count == 1

    # Deterministic on repeated replay: a third dispatch against the same
    # (still-grown) evidence isolates again, identically -- never raises,
    # never duplicates, never flips to a different outcome.
    third = service.execute(
        run_id="cycle-1-counterfactual-outcome", generated_at=NOW + timedelta(minutes=10)
    )
    assert third.stale_identity_superseded_count == 1
    assert list(repository.by_id) == [aggregate_id]


def test_unrelated_cohort_still_persists_in_the_same_call_as_an_isolated_conflict() -> None:
    """Per-item isolation, single-call shape: one cohort hitting the
    benign-conflict path must never block a *different*, otherwise-valid
    cohort's write attempted in the very same ``execute()`` call --
    matching the milestone's own explicit per-item-isolation requirement."""

    projection = _projection("refresh-mixed")
    source = _FakeSource(projection=projection)
    _two_decision_cohort(
        source, projection_id=projection.projection_id, second_decision_matures=False
    )
    repository = _FakeRepository()
    service = RefreshCounterfactualOutcomeAggregates(source, repository)
    first = service.execute(run_id="cycle-mixed-counterfactual-outcome", generated_at=NOW)
    assert first.newly_written_count == 1

    # Mature the ECONOMICS cohort (forces its conflict on replay) AND add a
    # brand-new, unrelated cohort (a third decision under a different
    # reason family) that has never been persisted before.
    _two_decision_cohort(
        source, projection_id=projection.projection_id, second_decision_matures=True
    )
    decision_3 = _decision(decision_id="cor-decision-3", projection_id=projection.projection_id)
    source.decisions.append(decision_3)
    source.classifications.append(
        _classification(
            decision_id=decision_3.decision_id, reason_families=(ReasonFamily.LIQUIDITY,)
        )
    )
    observation_3 = _observation(decision_id=decision_3.decision_id)
    source.observations.append(observation_3)
    source.assessments.append(_assessment(observation=observation_3))

    second = service.execute(
        run_id="cycle-mixed-counterfactual-outcome", generated_at=NOW + timedelta(minutes=5)
    )
    assert second.stale_identity_superseded_count == 1  # the ECONOMICS cohort, isolated
    assert second.newly_written_count == 1  # the new LIQUIDITY cohort, genuinely written
    assert len(second.aggregate_ids) == 1
    persisted_new = repository.aggregate(second.aggregate_ids[0])
    assert persisted_new is not None
    assert persisted_new.reason_family is ReasonFamily.LIQUIDITY


# ---------------------------------------------------------------------------
# The isolation must stay narrow -- never a blanket swallow
# ---------------------------------------------------------------------------


def test_non_monotonic_identity_conflict_still_raises() -> None:
    """A hash mismatch NOT explained by evidence only maturing further (a
    shrinking/regressing count, standing in for a genuine non-determinism
    or data-integrity defect) must still fail loudly -- exactly as before
    this milestone. Proves the isolation is not "any mismatch is fine"."""

    projection = _projection("refresh-regression")
    source = _FakeSource(projection=projection)
    _two_decision_cohort(
        source, projection_id=projection.projection_id, second_decision_matures=True
    )
    repository = _FakeRepository()
    service = RefreshCounterfactualOutcomeAggregates(source, repository)
    result = service.execute(run_id="cycle-regress-counterfactual-outcome", generated_at=NOW)
    [aggregate_id] = result.aggregate_ids
    genuine = repository.aggregate(aggregate_id)
    assert genuine is not None

    # Simulate a corrupted/regressed persisted row: higher mature count and
    # a contributing decision the next fresh recompute will not reproduce,
    # under a deliberately different canonical_hash so save_aggregate still
    # detects a conflict at all.
    repository.by_id[aggregate_id] = dataclasses.replace(
        genuine,
        mature_observation_count=genuine.mature_observation_count + 1,
        contributing_decision_ids=(
            *genuine.contributing_decision_ids,
            "decision-that-never-existed",
        ),
        canonical_hash="f" * 64,
    )

    with pytest.raises(CounterfactualOutcomeError, match=COUNTERFACTUAL_OUTCOME_IDENTITY_CONFLICT):
        service.execute(
            run_id="cycle-regress-counterfactual-outcome", generated_at=NOW + timedelta(minutes=5)
        )


def test_genuine_persistence_conflict_still_fails_the_task() -> None:
    """A real infrastructure failure (``COUNTERFACTUAL_OUTCOME_PERSISTENCE_
    CONFLICT``, wrapping an actual ``IntegrityError`` in the real adapter)
    must keep propagating -- it is meaningful signal the milestone
    explicitly requires stays fatal, never silently absorbed."""

    projection = _projection("refresh-infra")
    source = _FakeSource(projection=projection)
    _two_decision_cohort(
        source, projection_id=projection.projection_id, second_decision_matures=False
    )
    service = RefreshCounterfactualOutcomeAggregates(source, _AlwaysPersistenceConflictRepository())

    with pytest.raises(
        CounterfactualOutcomeError, match="COUNTERFACTUAL_OUTCOME_PERSISTENCE_CONFLICT"
    ):
        service.execute(run_id="cycle-infra-counterfactual-outcome", generated_at=NOW)


def test_qualification_state_unaffected_by_isolation() -> None:
    """The isolated cohort's *qualification state* is not silently altered
    -- the persisted (first-successful) row's own state is exactly what a
    fresh build of the identical original evidence would have produced;
    this milestone changes retry/persistence resilience only, never any
    research/eligibility threshold."""

    projection = _projection("refresh-qual")
    source = _FakeSource(projection=projection)
    _two_decision_cohort(
        source, projection_id=projection.projection_id, second_decision_matures=False
    )
    repository = _FakeRepository()
    service = RefreshCounterfactualOutcomeAggregates(source, repository)
    first = service.execute(run_id="cycle-qual-counterfactual-outcome", generated_at=NOW)
    [aggregate_id] = first.aggregate_ids
    persisted = repository.aggregate(aggregate_id)
    assert persisted is not None
    assert (
        persisted.qualification_state is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )
