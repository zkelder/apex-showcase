from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apex.adapters.research_operations_executor import FirstGeneratedOperationalMethodExecutor
from apex.application.first_generated_research_method import (
    SelectApproveAndImplementFirstGeneratedMethod,
)
from apex.application.research_operations import (
    EnqueueResearchExperiment,
    ExecuteApprovedResearchQueueItem,
    GenerateResearchProposals,
    InMemoryResearchOperationsRepository,
    RefreshResearchAgenda,
    ResearchProposalGenerationContext,
    proposal_state,
)
from apex.domain.first_generated_research_method import CurrentOpportunityOperationalEvidence
from apex.domain.research_operations import (
    ResearchAgendaSourceFact,
    ResearchFeasibilityState,
    ResearchOperationsError,
    ResearchProposalState,
    ResearchRecommendationState,
    ResearchVenue,
)

NOW = datetime(2026, 8, 3, 16, 50, tzinfo=UTC)


class _Evidence:
    def evidence(self) -> CurrentOpportunityOperationalEvidence:
        return CurrentOpportunityOperationalEvidence(
            source_projection_ids=("source-1", "source-2"),
            source_projection_hashes=("a" * 64, "b" * 64),
            shared_set_id="shared-1",
            shared_set_hash="c" * 64,
            context_set_id="context-1",
            context_set_hash="d" * 64,
            allocation_id="allocation-1",
            allocation_hash="e" * 64,
            source_opportunity_count=2,
            shared_eligible_count=0,
            allocated_line_count=0,
            paper_ready_proposal_ids=(),
            blockers=("SOURCE_POLICY_REJECTED",),
        )


def _repository() -> tuple[InMemoryResearchOperationsRepository, str]:
    repository = InMemoryResearchOperationsRepository()
    facts = (
        ResearchAgendaSourceFact.create(
            fact_id="negative_expected_economics-current-v1",
            fact_type="NEGATIVE_EXPECTED_ECONOMICS",
            venue=ResearchVenue.CRYPTO,
            market_scope=("CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1",),
            state="BLOCKED",
            summary="Negative current economics.",
            evidence_ids=("negative-current-v1",),
            evidence_hashes=("1" * 64,),
            observed_at=NOW,
        ),
        ResearchAgendaSourceFact.create(
            fact_id="zero_paper_ready-current-v1",
            fact_type="ZERO_PAPER_READY",
            venue=ResearchVenue.CROSS_VENUE,
            market_scope=("CURRENT_CROSS_VENUE_ALLOCATION",),
            state="BLOCKED",
            summary="No PAPER-ready proposal.",
            evidence_ids=("paper-current-v1",),
            evidence_hashes=("2" * 64,),
            observed_at=NOW,
        ),
        ResearchAgendaSourceFact.create(
            fact_id="zero_shared_eligible-current-v1",
            fact_type="ZERO_SHARED_ELIGIBLE",
            venue=ResearchVenue.CROSS_VENUE,
            market_scope=("CRYPTO", "KALSHI"),
            state="BLOCKED",
            summary="No Shared-eligible projection.",
            evidence_ids=("shared-current-v1",),
            evidence_hashes=("3" * 64,),
            observed_at=NOW,
        ),
    )
    agenda = RefreshResearchAgenda(repository).execute(facts=facts).items
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generated-current-v1",
        generated_at=NOW,
        actor="system:test",
        context=ResearchProposalGenerationContext(
            retained_data_ids=tuple(value.source_fact_id for value in agenda),
            executable_experiment_ids=("CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1", "CRYPTO"),
            provider_capability_ids=(),
            remaining_experiment_budget=1,
        ),
    )
    assert len(generated.proposals) == 3
    return repository, generated.generator_run.generator_run_id


def test_selects_rank_two_and_retains_exact_governance() -> None:
    repository, generator_id = _repository()
    result = SelectApproveAndImplementFirstGeneratedMethod(repository).execute(
        generator_run_id=generator_id,
        selection_id="selection-v1",
        implementation_id="implementation-v1",
        actor="operator:test",
        selected_at=NOW,
    )

    assert result.selection.selected_rank == 2
    assert result.selection.provider_request_limit == 0
    assert len(result.selection.deferred) == 2
    assert result.feasibility.status is ResearchFeasibilityState.READY_FOR_REVIEW
    assert result.feasibility.supersedes_feasibility_id is not None
    assert len(repository.proposal_feasibilities()) == 4
    assert tuple(
        proposal_state(repository, item.proposal_id) for item in result.selection.deferred
    ) == (ResearchProposalState.DEFERRED, ResearchProposalState.DEFERRED)
    assert repository.experiment_approval(result.selection.selected_proposal_id) is not None
    assert (
        sum(
            repository.experiment_approval(value.proposal_id) is not None
            for value in repository.proposals()
        )
        == 1
    )


def test_executes_once_and_exact_replay_is_budget_free() -> None:
    repository, generator_id = _repository()
    selection = SelectApproveAndImplementFirstGeneratedMethod(repository).execute(
        generator_run_id=generator_id,
        selection_id="selection-v1",
        implementation_id="implementation-v1",
        actor="operator:test",
        selected_at=NOW,
    )
    queue = EnqueueResearchExperiment(repository).execute(
        queue_id="queue-v1",
        proposal_id=selection.selection.selected_proposal_id,
        run_id="run-v1",
        actor="operator:test",
        idempotency_key="enqueue-v1",
        enqueued_at=NOW,
    )
    runner = ExecuteApprovedResearchQueueItem(
        repository, FirstGeneratedOperationalMethodExecutor(repository, _Evidence())
    )
    first = runner.execute(
        queue_id=queue.item.queue_id,
        actor="operator:test",
        idempotency_key="execute-v1",
        executed_at=NOW,
    )
    replay = runner.execute(
        queue_id=queue.item.queue_id,
        actor="operator:test",
        idempotency_key="replay-v1",
        executed_at=NOW,
    )

    assert first.governed_result.recommendation.state is (
        ResearchRecommendationState.INSUFFICIENT_EVIDENCE
    )
    assert first.governed_result.outcome.expected_net_economics is None
    assert replay.idempotent_replay
    assert replay.governed_result.budget.experiment_runs == 1
    assert replay.governed_result.budget.replays == 1
    assert replay.governed_result.budget.consumed_runs == 1
    method_run = repository.first_method_runs()[0]
    assert method_run.provider_request_count == 0
    assert method_run.paper_ready_proposal_ids == ()
    assert "ZERO_SHARED_ELIGIBLE" in method_run.blockers
    assert "ZERO_PAPER_READY" in method_run.blockers


def test_fail_closed_on_selection_identity_conflict() -> None:
    repository, generator_id = _repository()
    service = SelectApproveAndImplementFirstGeneratedMethod(repository)
    service.execute(
        generator_run_id=generator_id,
        selection_id="selection-v1",
        implementation_id="implementation-v1",
        actor="operator:test",
        selected_at=NOW,
    )
    with pytest.raises(ResearchOperationsError, match="SINGLE_SELECTION_CONFLICT"):
        service.execute(
            generator_run_id=generator_id,
            selection_id="selection-v2",
            implementation_id="implementation-v2",
            actor="operator:test",
            selected_at=NOW,
        )
