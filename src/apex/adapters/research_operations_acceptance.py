"""Deterministic isolated acceptance for proposal generation and experiment queuing."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from apex.application.research_operations import (
    EnqueueResearchExperiment,
    ExecuteApprovedResearchQueueItem,
    GenerateResearchProposals,
    InMemoryResearchOperationsRepository,
    RefreshResearchAgenda,
    ResearchProposalGenerationContext,
    ReviewResearchProposal,
    duplicate_decision,
)
from apex.domain.research_operations import (
    PriorResearchKnowledge,
    ResearchAgendaSourceFact,
    ResearchExperimentOutcome,
    ResearchFeasibilityState,
    ResearchProposal,
    ResearchProposalState,
    ResearchRecommendationState,
    ResearchVenue,
    classify_research_proposal,
    recommend_research_outcome,
)

ACCEPTANCE_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)


class _FixtureExecutor:
    def execute(
        self, *, proposal: ResearchProposal, run_id: str, executed_at: datetime
    ) -> ResearchExperimentOutcome:
        return _outcome(
            proposal=proposal,
            run_id=run_id,
            completed_at=executed_at,
            net=Decimal("0.04"),
            uncertainty=True,
            liquidity=True,
            scopes=3,
            improved=True,
        )


def _outcome(
    *,
    proposal: ResearchProposal,
    run_id: str,
    completed_at: datetime,
    net: Decimal | None,
    uncertainty: bool,
    liquidity: bool,
    scopes: int,
    improved: bool,
) -> ResearchExperimentOutcome:
    return ResearchExperimentOutcome.create(
        run_id=run_id,
        proposal_id=proposal.proposal_id,
        existing_experiment_id="FIXTURE_EXISTING_EXPERIMENT",
        existing_trial_ids=("FIXTURE_TRIAL",),
        result_hash="f" * 64,
        completed=True,
        reproducible=True,
        expected_net_economics=net,
        uncertainty_sufficient=uncertainty,
        liquidity_sufficient=liquidity,
        downside_within_limit=True,
        robust_scope_count=scopes,
        comparison_improved=improved,
        warnings=("ISOLATED_FIXTURE_ONLY",),
        completed_at=completed_at,
    )


def _review(
    repository: InMemoryResearchOperationsRepository,
    proposal: ResearchProposal,
    target: ResearchProposalState,
    *,
    key: str | None = None,
) -> None:
    decision = duplicate_decision(repository, proposal, knowledge_source=_EmptyKnowledge())
    ReviewResearchProposal(repository).execute(
        proposal_id=proposal.proposal_id,
        target_state=target,
        actor="operator:isolated-acceptance",
        reason=f"ISOLATED_{target.value}",
        confirmation=f"{target.value}:{proposal.proposal_id}:{decision.canonical_hash}",
        reviewed_at=ACCEPTANCE_AT,
        idempotency_key=key,
    )


class _EmptyKnowledge:
    def knowledge(self) -> tuple[PriorResearchKnowledge, ...]:
        return ()


def run_isolated_research_queue_acceptance() -> dict[str, object]:
    """Prove the bounded loop without a database, provider, broker, or financial state."""

    repository = InMemoryResearchOperationsRepository()
    facts = (
        ResearchAgendaSourceFact.create(
            fact_id="negative_expected_economics-fixture",
            fact_type="NEGATIVE_EXPECTED_ECONOMICS",
            venue=ResearchVenue.CRYPTO,
            market_scope=("FIXTURE_EXISTING_EXPERIMENT",),
            state="BLOCKED",
            summary="Fixture economics are negative and require a material mechanism change.",
            evidence_ids=("fixture-negative",),
            evidence_hashes=("a" * 64,),
            observed_at=ACCEPTANCE_AT,
        ),
        ResearchAgendaSourceFact.create(
            fact_id="zero_paper_ready-fixture",
            fact_type="ZERO_PAPER_READY",
            venue=ResearchVenue.CROSS_VENUE,
            market_scope=("FIXTURE_OPERATIONAL_SCOPE",),
            state="BLOCKED",
            summary="Fixture operational integration is deliberately unsupported.",
            evidence_ids=("fixture-operational",),
            evidence_hashes=("b" * 64,),
            observed_at=ACCEPTANCE_AT,
        ),
    )
    agenda = RefreshResearchAgenda(repository).execute(facts=facts).items
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="isolated-generator-v1",
        generated_at=ACCEPTANCE_AT,
        actor="system:isolated-acceptance",
        context=ResearchProposalGenerationContext(
            retained_data_ids=tuple(value.source_fact_id for value in agenda),
            executable_experiment_ids=("FIXTURE_EXISTING_EXPERIMENT",),
            provider_capability_ids=(),
            remaining_experiment_budget=1,
        ),
        maximum_proposals=2,
    )
    executable = next(
        value
        for value in generated.proposals
        if (feasibility := repository.proposal_feasibility(value.proposal_id)) is not None
        and feasibility.status is ResearchFeasibilityState.READY_FOR_REVIEW
    )
    unsupported = next(
        value
        for value in generated.feasibilities
        if value.status is ResearchFeasibilityState.REQUIRES_IMPLEMENTATION
    )
    exact_prior = PriorResearchKnowledge(
        research_id="fixture-exact-prior",
        venue=executable.venue,
        proposed_family=executable.proposed_family,
        semantic_hash=executable.semantic_hash,
        experiment_configuration_hash=executable.experiment_configuration_hash,
        validation_windows=executable.validation_windows,
        rejected=True,
        canonical_hash="d" * 64,
    )
    duplicate = classify_research_proposal(executable, (exact_prior,))
    _review(repository, executable, ResearchProposalState.READY_FOR_REVIEW)
    _review(
        repository,
        executable,
        ResearchProposalState.APPROVED_FOR_EXPERIMENT,
        key="isolated-approval-key",
    )
    enqueued = EnqueueResearchExperiment(repository).execute(
        queue_id="isolated-queue-v1",
        proposal_id=executable.proposal_id,
        run_id="isolated-run-v1",
        actor="operator:isolated-acceptance",
        idempotency_key="isolated-enqueue-key",
        enqueued_at=ACCEPTANCE_AT,
    )
    executed = ExecuteApprovedResearchQueueItem(repository, _FixtureExecutor()).execute(
        queue_id=enqueued.item.queue_id,
        actor="operator:isolated-acceptance",
        idempotency_key="isolated-execute-key",
        executed_at=ACCEPTANCE_AT,
    )
    replay = ExecuteApprovedResearchQueueItem(repository, _FixtureExecutor()).execute(
        queue_id=enqueued.item.queue_id,
        actor="operator:isolated-acceptance",
        idempotency_key="isolated-replay-key",
        executed_at=ACCEPTANCE_AT,
    )
    branch_inputs = (
        ("REJECT", Decimal("-0.01"), True, True, 3, True),
        ("REVISE", Decimal("0.04"), True, False, 3, True),
        ("REPLICATE", Decimal("0.04"), True, True, 1, False),
        ("INSUFFICIENT_EVIDENCE", None, False, True, 3, True),
        ("PAPER_CANDIDATE", Decimal("0.04"), True, True, 3, True),
    )
    branches = tuple(
        recommend_research_outcome(
            _outcome(
                proposal=executable,
                run_id=f"fixture-{expected.lower()}",
                completed_at=ACCEPTANCE_AT,
                net=net,
                uncertainty=uncertainty,
                liquidity=liquidity,
                scopes=scopes,
                improved=improved,
            )
        ).state
        for expected, net, uncertainty, liquidity, scopes, improved in branch_inputs
    )
    expected_branches = tuple(ResearchRecommendationState(value[0]) for value in branch_inputs)
    return {
        "generator_id": generated.generator_run.generator_id,
        "policy_id": generated.generator_run.policy_id,
        "generated_proposal_ids": generated.generator_run.generated_proposal_ids,
        "exact_duplicate_classification": duplicate.classification,
        "exact_duplicate_approval_permitted": duplicate.approval_permitted,
        "unsupported_proposal_id": unsupported.proposal_id,
        "unsupported_status": unsupported.status,
        "approved_proposal_id": executable.proposal_id,
        "approval": repository.experiment_approval(executable.proposal_id),
        "queue_item": enqueued.item,
        "queue_completion": executed.event,
        "recommendation": executed.governed_result.recommendation,
        "budget": replay.governed_result.budget,
        "exact_replay": replay.idempotent_replay,
        "recommendation_branches": branches,
        "recommendation_branches_match": branches == expected_branches,
        "provider_requests": 0,
        "paper_authorizations": 0,
        "financial_mutations": 0,
        "live_authority": "NONE",
    }
