"""Offline (no Postgres) proof that ``RefreshSecuritiesStructureOutcomeAggregates``
carries the identical per-aggregate identity-conflict isolation as its sibling
``RefreshCounterfactualOutcomeAggregates`` (Counterfactual Outcome Refresh
Recovery v1). Single, focused test: full offline coverage of the shared
mechanism already lives in ``tests/test_counterfactual_outcome_refresh.py``;
this file only proves the SECURITIES pass -- a separate module with its own
repository Protocol and its own ``total_decisions_considered`` field name --
was not missed when the fix was mirrored across both."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta

from apex.application.securities_structure_outcome import (
    RefreshSecuritiesStructureOutcomeAggregates,
)
from apex.domain.securities_structure_outcome import (
    SECURITIES_STRUCTURE_OUTCOME_IDENTITY_CONFLICT,
    SecuritiesStructureOutcomeAggregate,
    SecuritiesStructureOutcomeError,
)
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import content_digest
from tests.test_securities_structure_outcome import _assessment, _decision, _observation

NOW = datetime(2026, 9, 2, tzinfo=UTC)
METHOD_ID = "SECURITIES_DEFINED_RISK_OPTIONS_STRUCTURE_V1"
STRUCTURE_TYPE = "CALL_CREDIT_VERTICAL"


def _projection(identity: str) -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=f"sso-proj-{identity}",
        venue=SharedVenue.SECURITIES,
        source_record_type="OptionStructureCandidate",
        source_record_id=f"sso-source-{identity}",
        source_canonical_hash=content_digest((identity,)),
        strategy_or_method_id=METHOD_ID,
        instrument_id="XLK",
        product_or_market_id="structure-candidate-1",
        direction=STRUCTURE_TYPE,
        generated_at=NOW,
        valid_until=NOW + timedelta(days=60),
        evidence_class="GENUINE_PUBLIC",
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
        expected_holding_days=30,
        estimated_lockup_days=30,
        supporting_evidence_ids=(f"sso-evidence-{identity}",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(),
    )


class _FakeSource:
    def __init__(self, *, projection: SharedOpportunityProjection) -> None:
        self.decisions: list = []
        self.observations: list = []
        self.assessments: list = []
        self._projections = {projection.projection_id: projection}

    def all_decisions(self) -> tuple:
        return tuple(self.decisions)

    def all_classifications(self) -> tuple:
        return ()  # unused by the SECURITIES pass

    def all_observations(self) -> tuple:
        return tuple(self.observations)

    def all_assessments(self) -> tuple:
        return tuple(self.assessments)

    def all_kalshi_baseline_quotes(self) -> tuple:
        return ()

    def projections_by_id(
        self, projection_ids: Collection[str]
    ) -> Mapping[str, SharedOpportunityProjection]:
        return {pid: self._projections[pid] for pid in projection_ids if pid in self._projections}


class _FakeRepository:
    def __init__(self) -> None:
        self.by_id: dict[str, SecuritiesStructureOutcomeAggregate] = {}

    def aggregate(self, aggregate_id: str) -> SecuritiesStructureOutcomeAggregate | None:
        return self.by_id.get(aggregate_id)

    def save_aggregate(self, *, aggregate: SecuritiesStructureOutcomeAggregate) -> bool:
        existing = self.by_id.get(aggregate.aggregate_id)
        if existing is not None:
            if existing.canonical_hash != aggregate.canonical_hash:
                raise SecuritiesStructureOutcomeError(
                    SECURITIES_STRUCTURE_OUTCOME_IDENTITY_CONFLICT
                )
            return True
        self.by_id[aggregate.aggregate_id] = aggregate
        return False


def _seed(source: _FakeSource, *, projection_id: str, second_matures: bool) -> None:
    decision_1 = _decision(decision_id="sso-decision-1", projection_id=projection_id)
    decision_2 = _decision(decision_id="sso-decision-2", projection_id=projection_id)
    source.decisions = [decision_1, decision_2]
    observation_1 = _observation(decision_id=decision_1.decision_id)
    source.observations = [observation_1]
    source.assessments = [_assessment(observation=observation_1)]
    if second_matures:
        observation_2 = _observation(decision_id=decision_2.decision_id)
        source.observations.append(observation_2)
        source.assessments.append(_assessment(observation=observation_2))


def test_second_dispatch_with_matured_evidence_is_isolated_not_a_task_failure() -> None:
    projection = _projection("refresh-1")
    source = _FakeSource(projection=projection)
    _seed(source, projection_id=projection.projection_id, second_matures=False)
    repository = _FakeRepository()
    service = RefreshSecuritiesStructureOutcomeAggregates(source, repository)

    first = service.execute(run_id="cycle-1-securities-structure-outcome", generated_at=NOW)
    assert first.newly_written_count == 1
    assert first.stale_identity_superseded_count == 0
    [aggregate_id] = first.aggregate_ids
    persisted_after_first = repository.aggregate(aggregate_id)
    assert persisted_after_first is not None
    assert persisted_after_first.total_decisions_considered == 2
    assert persisted_after_first.mature_observation_count == 1

    _seed(source, projection_id=projection.projection_id, second_matures=True)
    second = service.execute(
        run_id="cycle-1-securities-structure-outcome", generated_at=NOW + timedelta(minutes=5)
    )
    assert second.stale_identity_superseded_count == 1
    assert second.newly_written_count == 0
    assert second.aggregate_ids == ()

    assert list(repository.by_id) == [aggregate_id]
    unchanged = repository.aggregate(aggregate_id)
    assert unchanged == persisted_after_first
