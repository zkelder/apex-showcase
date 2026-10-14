from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.crypto_stateful_research_postgres import (
    PostgresCryptoStatefulResearchRepository,
)
from apex.adapters.research_operations_postgres import (
    PostgresResearchOperationsRepository,
)
from apex.application.research_operations import (
    CancelQueuedResearchExperiment,
    CreateResearchProposal,
    EmptyResearchKnowledgeSource,
    EnqueueResearchExperiment,
    GenerateResearchProposals,
    RefreshResearchAgenda,
    ResearchProposalGenerationContext,
    ReviewResearchProposal,
    duplicate_decision,
)
from apex.domain.crypto_stateful_research import (
    CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1,
    CryptoStatefulMethodSpecification,
    ResearchProposalRefinement,
)
from apex.domain.research_operations import (
    ResearchAgendaSourceFact,
    ResearchProposal,
    ResearchProposalOrigin,
    ResearchProposalState,
    ResearchVenue,
)

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _fact() -> ResearchAgendaSourceFact:
    return ResearchAgendaSourceFact.create(
        fact_id="persistence-zero-shared",
        fact_type="ZERO_SHARED_ELIGIBLE",
        venue=ResearchVenue.CROSS_VENUE,
        market_scope=("CRYPTO", "KALSHI"),
        state="BLOCKED",
        summary="The exact retained fixture Shared set contains zero eligible records.",
        evidence_ids=("shared-set-fixture",),
        evidence_hashes=("a" * 64,),
        observed_at=NOW,
    )


def _proposal() -> ResearchProposal:
    return ResearchProposal.create(
        proposal_id="persistence-proposal-1",
        version=1,
        agenda_item_id="agenda-persistence-zero-shared",
        venue=ResearchVenue.KALSHI,
        hypothesis="A disjoint retained replication can test the blocker.",
        expected_mechanism="Replay compatible retained relationship evidence.",
        instruments=("KALSHI_RETAINED_EVENT_SCOPE",),
        proposed_family="KALSHI_RETAINED_REPLICATION",
        definition_kind="EXISTING_RESEARCH_REPLICATION",
        strategy_definition=(("existing_experiment_id", "fixture-family"),),
        exact_change_from_prior="DISJOINT_REPLICATION_WINDOW",
        prior_research_ids=("prior-kalshi-method",),
        required_data_ids=("retained-rule-evidence", "retained-book-evidence"),
        experiment_configuration=(("existing_experiment_id", "fixture-family"),),
        validation_windows=(("replication", "fixture-disjoint-window"),),
        cost_assumptions=(("fee_policy", "retained-exact"),),
        success_criteria=("POSITIVE_CONSERVATIVE_NET_GAP",),
        failure_criteria=("NON_POSITIVE_CONSERVATIVE_NET_GAP",),
        maximum_experiment_runs=1,
        origin=ResearchProposalOrigin.OPERATOR,
        actor="operator:persistence-test",
        warnings=("FIXTURE_ONLY",),
        created_at=NOW,
    )


def _generator_fact() -> ResearchAgendaSourceFact:
    return ResearchAgendaSourceFact.create(
        fact_id="negative_expected_economics-persistence",
        fact_type="NEGATIVE_EXPECTED_ECONOMICS",
        venue=ResearchVenue.CRYPTO,
        market_scope=("PERSISTENCE_EXISTING_EXPERIMENT",),
        state="BLOCKED",
        summary="The persistence fixture requires a material bounded challenger.",
        evidence_ids=("persistence-negative",),
        evidence_hashes=("e" * 64,),
        observed_at=NOW,
    )


def test_postgres_research_operations_are_immutable_idempotent_and_authority_free(
    postgres_engine: Engine,
) -> None:
    repository = PostgresResearchOperationsRepository(postgres_engine)
    refresh = RefreshResearchAgenda(repository).execute(facts=(_fact(),))
    proposal, replay = CreateResearchProposal(repository).execute(_proposal())
    decision = duplicate_decision(repository, proposal, EmptyResearchKnowledgeSource())
    review, _, review_replay = ReviewResearchProposal(repository).execute(
        proposal_id=proposal.proposal_id,
        target_state=ResearchProposalState.READY_FOR_REVIEW,
        actor="operator:persistence-test",
        reason="READY_FOR_EXACT_REVIEW",
        confirmation=(f"READY_FOR_REVIEW:{proposal.proposal_id}:{decision.canonical_hash}"),
        reviewed_at=NOW,
    )

    assert replay is False
    assert review_replay is False
    assert repository.agenda_item(refresh.items[0].agenda_item_id) == refresh.items[0]
    assert repository.proposal(proposal.proposal_id) == proposal
    assert repository.reviews(proposal.proposal_id) == (review,)
    assert proposal.authority == "NONE"
    assert proposal.paper_eligible is False
    assert proposal.live_eligible is False
    with (
        pytest.raises(Exception, match="immutable Apex row cannot be changed"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE governed_research_proposal SET authority = 'PAPER' "
                "WHERE proposal_id = :proposal_id"
            ),
            {"proposal_id": proposal.proposal_id},
        )


def test_0065_downgrade_refuses_retained_research_operations_evidence(
    postgres_engine: Engine,
) -> None:
    RefreshResearchAgenda(PostgresResearchOperationsRepository(postgres_engine)).execute(
        facts=(_fact(),)
    )
    config = Config(str(ROOT / "alembic.ini"))
    with postgres_engine.connect() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(Exception, match="cannot downgrade 0065"):
            command.downgrade(config, "0064_performance_attribution")
        connection.rollback()
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0065_research_operations"
        )


def test_0066_generator_approval_queue_and_downgrade_are_durable(
    postgres_engine: Engine,
) -> None:
    repository = PostgresResearchOperationsRepository(postgres_engine)
    agenda = RefreshResearchAgenda(repository).execute(facts=(_generator_fact(),)).items
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="persistence-generator-1",
        generated_at=NOW,
        actor="system:persistence-test",
        context=ResearchProposalGenerationContext(
            retained_data_ids=(agenda[0].source_fact_id,),
            executable_experiment_ids=(agenda[0].market_scope[0],),
            provider_capability_ids=(),
            remaining_experiment_budget=1,
        ),
        maximum_proposals=1,
    )
    proposal = generated.proposals[0]
    for state in (
        ResearchProposalState.READY_FOR_REVIEW,
        ResearchProposalState.APPROVED_FOR_EXPERIMENT,
    ):
        decision = duplicate_decision(repository, proposal, EmptyResearchKnowledgeSource())
        ReviewResearchProposal(repository).execute(
            proposal_id=proposal.proposal_id,
            target_state=state,
            actor="operator:persistence-test",
            reason=f"PERSISTENCE_{state.value}",
            confirmation=f"{state.value}:{proposal.proposal_id}:{decision.canonical_hash}",
            reviewed_at=NOW,
            idempotency_key=(
                "persistence-approval-key" if state.value.startswith("APPROVED") else None
            ),
        )
    queued = EnqueueResearchExperiment(repository).execute(
        queue_id="persistence-queue-1",
        proposal_id=proposal.proposal_id,
        run_id="persistence-run-1",
        actor="operator:persistence-test",
        idempotency_key="persistence-enqueue-key",
        enqueued_at=NOW,
    )
    canceled, _ = CancelQueuedResearchExperiment(repository).execute(
        queue_id=queued.item.queue_id,
        actor="operator:persistence-test",
        reason="PERSISTENCE_CANCEL",
        idempotency_key="persistence-cancel-key",
        canceled_at=NOW,
    )

    assert repository.generator_run("persistence-generator-1") == generated.generator_run
    assert repository.proposal_priority(proposal.proposal_id) == generated.priorities[0]
    assert repository.proposal_feasibility(proposal.proposal_id) == generated.feasibilities[0]
    assert repository.experiment_approval(proposal.proposal_id) is not None
    assert repository.queue_events(queued.item.queue_id)[-1] == canceled
    config = Config(str(ROOT / "alembic.ini"))
    with postgres_engine.connect() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(Exception, match="cannot downgrade 0066"):
            command.downgrade(config, "0065_research_operations")
        connection.rollback()


def test_0067_retains_feasibility_history_and_first_method_tables(
    postgres_engine: Engine,
) -> None:
    inspector = sa.inspect(postgres_engine)
    tables = set(inspector.get_table_names())

    assert {
        "first_generated_research_method_selection",
        "first_generated_research_method_implementation",
        "first_generated_research_method_run",
    }.issubset(tables)
    feasibility_uniques = {
        tuple(value["column_names"])
        for value in inspector.get_unique_constraints("research_proposal_feasibility")
    }
    assert ("proposal_id",) not in feasibility_uniques
    assert "ix_research_proposal_feasibility_latest" in {
        value["name"] for value in inspector.get_indexes("research_proposal_feasibility")
    }
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0118_evidence_gap_recovery"
        )


def test_0068_retains_generic_refinement_and_stateful_specification(
    postgres_engine: Engine,
) -> None:
    operations = PostgresResearchOperationsRepository(postgres_engine)
    stateful = PostgresCryptoStatefulResearchRepository(postgres_engine)
    RefreshResearchAgenda(operations).execute(facts=(_generator_fact(),))
    original = ResearchProposal.create(
        proposal_id="persistence-generated-crypto-v1",
        version=1,
        agenda_item_id="agenda-negative_expected_economics-persistence",
        venue=ResearchVenue.CRYPTO,
        hypothesis="A stateful challenger may improve retained decision evidence.",
        expected_mechanism="Underspecified stateful long-cash transitions.",
        instruments=("CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1",),
        proposed_family="CRYPTO_MATERIAL_STATEFUL_CHALLENGER",
        definition_kind="CRYPTO_GENERIC_LONG_CASH",
        strategy_definition=(("state", "STATEFUL_LONG_CASH"),),
        exact_change_from_prior="STATEFUL_TRANSITIONS",
        prior_research_ids=(),
        required_data_ids=("retained-history",),
        experiment_configuration=(("scope", "retained"),),
        validation_windows=(("walk_forward", "retained"),),
        cost_assumptions=(("fee", "retained"),),
        success_criteria=("COMPLETE",),
        failure_criteria=("FAILED",),
        maximum_experiment_runs=1,
        origin=ResearchProposalOrigin.SYSTEM_HEURISTIC,
        actor="system:persistence",
        warnings=("RESEARCH_ONLY",),
        created_at=NOW,
    )
    operations.save_proposal(original)
    refined = ResearchProposal.create(
        proposal_id="persistence-generated-crypto-v2",
        version=2,
        agenda_item_id=original.agenda_item_id,
        venue=ResearchVenue.CRYPTO,
        hypothesis="The exact fixed stateful challenger may improve decision evidence.",
        expected_mechanism="Fixed pullback entry and explicit stateful exits through cash.",
        instruments=original.instruments,
        proposed_family=original.proposed_family,
        definition_kind="CRYPTO_GENERIC_LONG_CASH",
        strategy_definition=(
            ("decision_cadence", "DAILY_COMPLETED"),
            ("execution", "NEXT_ELIGIBLE_DAILY_OPEN"),
            ("maximum_hold_days", "14"),
            ("maximum_positions", "1"),
            ("method_id", CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1),
            ("pullback_threshold", "-0.03"),
            ("return_7d", "7_COMPLETED_DAYS"),
            ("return_90d", "90_COMPLETED_DAYS"),
            ("rotation", "EXIT_TO_CASH_BEFORE_LATER_ENTRY"),
            ("volatility_30d", "30_COMPLETED_DAYS_ANNUALIZED"),
        ),
        exact_change_from_prior="MATERIAL_FIXED_STATEFUL_REFINEMENT",
        prior_research_ids=(original.proposal_id,),
        required_data_ids=("retained-open-close-history",),
        experiment_configuration=(("executor", "stateful"),),
        validation_windows=(("walk_forward", "retained"),),
        cost_assumptions=(("fee_bps", "60"),),
        success_criteria=("COMPLETE",),
        failure_criteria=("FAILED",),
        maximum_experiment_runs=1,
        origin=ResearchProposalOrigin.OPERATOR,
        actor="operator:persistence",
        warnings=("RESEARCH_ONLY",),
        created_at=NOW,
    )
    operations.save_proposal(refined)
    refinement = ResearchProposalRefinement.create(
        refinement_id="persistence-refinement-v1",
        original=original,
        refined=refined,
        provider_request_limit=9,
        refined_at=NOW,
    )
    specification = CryptoStatefulMethodSpecification.create(
        specification_id="persistence-stateful-spec-v1",
        proposal=refined,
        implementation_id="persistence-stateful-implementation-v1",
        implemented_at=NOW,
    )
    assert stateful.save_refinement(refinement)
    assert stateful.save_specification(specification)
    assert stateful.refinement(refinement.refinement_id) == refinement
    assert stateful.specification(specification.specification_id) == specification
    with (
        pytest.raises(Exception, match="immutable Apex row cannot be changed"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE crypto_stateful_method_specification SET authority = 'PAPER' "
                "WHERE specification_id = :specification_id"
            ),
            {"specification_id": specification.specification_id},
        )
