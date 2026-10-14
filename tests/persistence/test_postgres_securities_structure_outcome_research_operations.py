"""PostgreSQL persistence proofs for Securities Structure Forward
Performance v1's research-agenda feedback block (mirrors ``COUNTERFACTUAL_
OUTCOME_PATTERN``'s own Phase 6 in ``adapters.research_operations_sources``,
one section below it): a QUALIFIED securities-structure aggregate produces
exactly one, stable, idempotent agenda item tagged ``ResearchVenue.
SECURITIES``; an INSUFFICIENT_SAMPLE aggregate produces none."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_outcome_postgres import PostgresCounterfactualOutcomeRepository
from apex.adapters.research_operations_executor import (
    GovernedResearchExecutor,
    RetainedOutcomeAggregateExperimentExecutor,
)
from apex.adapters.research_operations_postgres import PostgresResearchOperationsRepository
from apex.adapters.research_operations_sources import RetainedResearchAgendaSource
from apex.adapters.securities_structure_outcome_postgres import (
    PostgresSecuritiesStructureOutcomeRepository,
)
from apex.application.research_operations import (
    EmptyResearchKnowledgeSource,
    EnqueueResearchExperiment,
    ExecuteApprovedResearchQueueItem,
    GenerateResearchProposals,
    RefreshResearchAgenda,
    ResearchProposalGenerationContext,
    ReviewResearchProposal,
    duplicate_decision,
)
from apex.domain.counterfactual_followup import (
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    FOLLOWUP_POLICY_V2,
    FollowUpBaselineKind,
    FollowUpHorizonKind,
)
from apex.domain.research_operations import (
    ResearchExperimentQueueStatus,
    ResearchProposalClass,
    ResearchProposalState,
    ResearchRecommendationState,
    ResearchVenue,
)
from apex.domain.securities_structure_outcome import (
    SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
    SecuritiesStructureOutcomeAggregate,
    SecuritiesStructureOutcomeQualificationState,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 24, tzinfo=UTC)
_METHOD_ID = "SECURITIES_DEFINED_RISK_OPTIONS_STRUCTURE_V1"
_STRUCTURE_TYPE = "CALL_CREDIT_VERTICAL"


def _aggregate(
    *,
    aggregate_id: str,
    cohort_id: str,
    run_id: str,
    qualification_state: SecuritiesStructureOutcomeQualificationState,
    eligible_observation_count: int = 30,
    evidence_quality_policy_version: str = FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
) -> SecuritiesStructureOutcomeAggregate:
    qualified = qualification_state is SecuritiesStructureOutcomeQualificationState.QUALIFIED
    return SecuritiesStructureOutcomeAggregate(
        aggregate_id=aggregate_id,
        cohort_id=cohort_id,
        outcome_policy_version=SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
        run_id=run_id,
        strategy_or_method_id=_METHOD_ID,
        structure_type=_STRUCTURE_TYPE,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        followup_policy_version=FOLLOWUP_POLICY_V2,
        evidence_quality_policy_version=evidence_quality_policy_version,
        generated_at=NOW,
        total_decisions_considered=eligible_observation_count,
        mature_observation_count=eligible_observation_count,
        eligible_observation_count=eligible_observation_count,
        evidence_ineligible_observation_count=0,
        unassessed_observation_count=0,
        unavailable_observation_count=0,
        positive_outcome_count=eligible_observation_count,
        negative_outcome_count=0,
        zero_outcome_count=0,
        mean_subsequent_return=Decimal("0.08") if qualified else None,
        median_subsequent_return=Decimal("0.08") if qualified else None,
        positive_rate=Decimal("1") if qualified else None,
        qualification_state=qualification_state,
        contributing_decision_ids=tuple(f"decision-{i}" for i in range(eligible_observation_count)),
        explanation=f"test aggregate {qualification_state.value}",
        canonical_hash=f"{aggregate_id}-hash".ljust(64, "0")[:64],
        authority="NONE",
    )


def test_qualified_aggregate_produces_exactly_one_stable_agenda_item(
    postgres_engine: Engine,
) -> None:
    outcome_repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="sso-agg-qualified-day1",
            cohort_id="sso-cohort-qualified",
            run_id="sso-run-day1",
            qualification_state=SecuritiesStructureOutcomeQualificationState.QUALIFIED,
        )
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    facts = source.facts(as_of=NOW)
    matching = [item for item in facts if item.fact_type == "SECURITIES_STRUCTURE_OUTCOME_PATTERN"]
    assert len(matching) == 1
    fact = matching[0]
    assert fact.venue is ResearchVenue.SECURITIES
    # ``ResearchAgendaSourceFact.create`` sorts ``market_scope`` internally
    # (alphabetical, not insertion order) -- "CALL_..." < "SECURITIES_...".
    assert fact.market_scope == tuple(sorted((_METHOD_ID, _STRUCTURE_TYPE)))
    assert fact.state == "QUALIFIED"

    repository = PostgresResearchOperationsRepository(postgres_engine)
    refresh = RefreshResearchAgenda(repository).execute(facts=facts)
    matching_items = [
        item
        for item in refresh.items
        if item.source_fact_id.startswith("securities-structure-outcome-")
    ]
    assert len(matching_items) == 1
    assert matching_items[0].suggested_next_step == (
        "REVIEW_SECURITIES_STRUCTURE_OUTCOME_FOR_COMPARATIVE_RESEARCH_CONCLUSIONS"
    )
    assert matching_items[0].venue is ResearchVenue.SECURITIES


def test_qualified_aggregate_yields_a_correctly_classed_proposal_not_evidence_completion(
    postgres_engine: Engine,
) -> None:
    """Extends the proof above one stage further, through real proposal
    generation: for Research Proposal Coverage & Fairness v1 -- before this
    milestone, ``_agenda_template`` had no branch recognizing
    ``SECURITIES_STRUCTURE_OUTCOME_PATTERN`` facts, so this exact agenda
    item would have been misclassified into the generic, lowest-scoring
    ``EVIDENCE_COMPLETION`` fallback (semantically wrong -- that template
    means "evidence is missing", not "a mature qualified pattern exists")
    instead of the correctly-classed ``REPLICATION`` proposal proven here."""

    outcome_repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="sso-agg-proposal-qualified",
            cohort_id="sso-cohort-proposal",
            run_id="sso-run-proposal-day1",
            qualification_state=SecuritiesStructureOutcomeQualificationState.QUALIFIED,
        )
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    repository = PostgresResearchOperationsRepository(postgres_engine)
    # Scoped to exactly the SECURITIES_STRUCTURE_OUTCOME_PATTERN fact, like
    # the sibling tests above -- a freshly migrated database's other,
    # ambient fact sources (e.g. strategy-library health facts) are real
    # but are not what this test is proving, and including them would make
    # this test about the (separately covered, unit-tested) fairness
    # mechanism instead of the template classification.
    matching_facts = tuple(
        value
        for value in source.facts(as_of=NOW)
        if value.fact_type == "SECURITIES_STRUCTURE_OUTCOME_PATTERN"
    )
    agenda = RefreshResearchAgenda(repository).execute(facts=matching_facts).items

    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-securities-proposal-fixture",
        generated_at=NOW,
        actor="system:fixture",
        context=ResearchProposalGenerationContext(
            retained_data_ids=tuple(value.source_fact_id for value in agenda),
            executable_experiment_ids=(),
            provider_capability_ids=(),
            remaining_experiment_budget=1,
        ),
        maximum_proposals=1,
    )

    assert len(generated.proposals) == 1
    proposal = generated.proposals[0]
    assert proposal.venue is ResearchVenue.SECURITIES
    assert "SECURITIES_STRUCTURE_OUTCOME_POLICY_REVIEW" in proposal.proposed_family
    priority = generated.priorities[0]
    assert priority.proposal_class is ResearchProposalClass.REPLICATION


def test_v1_evidence_quality_cohort_is_withheld_from_new_sourcing(
    postgres_engine: Engine,
) -> None:
    """Securities Follow-Up Temporal Integrity v1: a cohort built over the
    superseded V1 verdict (which admits terminal prices from a later
    session) originates no new research work; its row stays retained."""

    outcome_repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="sso-agg-v1-qualified",
            cohort_id="sso-cohort-v1",
            run_id="sso-run-v1",
            qualification_state=SecuritiesStructureOutcomeQualificationState.QUALIFIED,
            evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        )
    )
    facts = RetainedResearchAgendaSource(postgres_engine).facts(as_of=NOW)
    assert not [item for item in facts if item.fact_type == "SECURITIES_STRUCTURE_OUTCOME_PATTERN"]
    assert outcome_repository.aggregate("sso-agg-v1-qualified") is not None


def test_insufficient_sample_never_generates_a_fact(postgres_engine: Engine) -> None:
    outcome_repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="sso-agg-insufficient",
            cohort_id="sso-cohort-insufficient",
            run_id="sso-run-day1",
            qualification_state=SecuritiesStructureOutcomeQualificationState.INSUFFICIENT_SAMPLE,
            eligible_observation_count=5,
        )
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    facts = source.facts(as_of=NOW)
    matching = [item for item in facts if item.fact_type == "SECURITIES_STRUCTURE_OUTCOME_PATTERN"]
    assert matching == []


def test_replay_is_idempotent_no_duplicate_agenda_item(postgres_engine: Engine) -> None:
    outcome_repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="sso-agg-replay-day1",
            cohort_id="sso-cohort-replay",
            run_id="sso-run-day1",
            qualification_state=SecuritiesStructureOutcomeQualificationState.QUALIFIED,
        )
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    repository = PostgresResearchOperationsRepository(postgres_engine)

    first = RefreshResearchAgenda(repository).execute(facts=source.facts(as_of=NOW))
    second = RefreshResearchAgenda(repository).execute(facts=source.facts(as_of=NOW))
    matching_first = [
        i for i in first.items if i.source_fact_id.startswith("securities-structure-outcome-")
    ]
    matching_second_replayed = [
        i for i in second.replayed_item_ids if i.startswith("agenda-securities-structure-outcome-")
    ]
    assert len(matching_first) == 1
    assert len(matching_second_replayed) == 1
    assert matching_second_replayed[0] == f"agenda-{matching_first[0].source_fact_id}"


def test_qualified_aggregate_reaches_a_real_recommendation_end_to_end(
    postgres_engine: Engine,
) -> None:
    """Research Experiment Executability v1, against real Postgres: extends
    ``test_qualified_aggregate_yields_a_correctly_classed_proposal_not_
    evidence_completion`` above one stage further -- through feasibility,
    review, approval, the experiment queue, and a genuine
    ``RetainedOutcomeAggregateExperimentExecutor`` execution that reads this
    exact aggregate back out of the same ``PostgresSecuritiesStructure
    OutcomeRepository`` used to save it -- to a real recommendation. Before
    this milestone, no proposal for this fact type had ever even been
    generated in production (see Research Proposal Coverage & Fairness
    v1); this proves that once one is, it does not stall at feasibility the
    same way every ``COUNTERFACTUAL_OUTCOME_POLICY_REVIEW`` proposal
    already had (see the sibling persistence test for that venue)."""

    outcome_repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="sso-agg-end-to-end-day1",
            cohort_id="sso-cohort-end-to-end",
            run_id="sso-run-end-to-end-day1",
            qualification_state=SecuritiesStructureOutcomeQualificationState.QUALIFIED,
        )
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    repository = PostgresResearchOperationsRepository(postgres_engine)
    matching_facts = tuple(
        value
        for value in source.facts(as_of=NOW)
        if value.fact_type == "SECURITIES_STRUCTURE_OUTCOME_PATTERN"
    )
    agenda = RefreshResearchAgenda(repository).execute(facts=matching_facts).items

    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-securities-end-to-end",
        generated_at=NOW,
        actor="system:fixture",
        context=ResearchProposalGenerationContext(
            retained_data_ids=tuple(value.source_fact_id for value in agenda),
            executable_experiment_ids=(),  # no research_experiment_family match, deliberately
            provider_capability_ids=(),
            remaining_experiment_budget=1,
        ),
        maximum_proposals=1,
    )
    assert len(generated.proposals) == 1
    value = generated.proposals[0]
    assert generated.feasibilities[0].status.value == "READY_FOR_REVIEW"

    knowledge = EmptyResearchKnowledgeSource()
    ready_decision = duplicate_decision(repository, value, knowledge)
    ReviewResearchProposal(repository, knowledge).execute(
        proposal_id=value.proposal_id,
        target_state=ResearchProposalState.READY_FOR_REVIEW,
        actor="operator:zach",
        reason="TEST_READY_FOR_REVIEW",
        confirmation=f"READY_FOR_REVIEW:{value.proposal_id}:{ready_decision.canonical_hash}",
        reviewed_at=NOW,
    )
    approved_decision = duplicate_decision(repository, value, knowledge)
    ReviewResearchProposal(repository, knowledge).execute(
        proposal_id=value.proposal_id,
        target_state=ResearchProposalState.APPROVED_FOR_EXPERIMENT,
        actor="operator:zach",
        reason="TEST_APPROVED_FOR_EXPERIMENT",
        confirmation=(
            f"APPROVED_FOR_EXPERIMENT:{value.proposal_id}:{approved_decision.canonical_hash}"
        ),
        reviewed_at=NOW,
        idempotency_key="approval-key-securities-end-to-end",
    )
    enqueued = EnqueueResearchExperiment(repository).execute(
        queue_id="queue-securities-end-to-end",
        proposal_id=value.proposal_id,
        run_id="run-securities-end-to-end",
        actor="operator:zach",
        idempotency_key="enqueue-key-securities-end-to-end",
        enqueued_at=NOW,
    )
    assert enqueued.event.to_status is ResearchExperimentQueueStatus.QUEUED

    executor = GovernedResearchExecutor(
        ledger=_UnreachableExecutor(),
        operational=_UnreachableExecutor(),
        outcome_aggregate=RetainedOutcomeAggregateExperimentExecutor(
            counterfactual_outcome=PostgresCounterfactualOutcomeRepository(postgres_engine),
            securities_structure_outcome=outcome_repository,
        ),
    )
    result = ExecuteApprovedResearchQueueItem(repository, executor).execute(
        queue_id="queue-securities-end-to-end",
        actor="operator:zach",
        idempotency_key="execute-key-securities-end-to-end",
        executed_at=NOW,
    )

    assert result.event.to_status is ResearchExperimentQueueStatus.COMPLETED
    # The fixture's aggregate has ``mean_subsequent_return=Decimal("0.08")``
    # (positive) -- honestly REVISE, never PAPER_CANDIDATE, matching the
    # executor's own documented "never overclaims" contract.
    assert result.governed_result.recommendation.state is ResearchRecommendationState.REVISE
    assert result.governed_result.outcome.expected_net_economics == Decimal("0.08")
    assert result.governed_result.recommendation.paper_authorization_created is False
    assert result.governed_result.recommendation.authority == "NONE"
    assert result.governed_result.recommendation.live_authority == "NONE"


class _UnreachableExecutor:
    """Proves the generic ledger/operational executors are never reached
    for a ``SECURITIES_STRUCTURE_OUTCOME_POLICY_REVIEW`` proposal."""

    def execute(self, **_kwargs: object) -> None:
        raise AssertionError("this executor must never be called for this proposal family")
