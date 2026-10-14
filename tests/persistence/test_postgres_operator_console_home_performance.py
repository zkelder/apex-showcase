"""PostgreSQL persistence proofs for Operator Console Home Performance v1.

``GET /api/v1/operator-console/home`` had two independent, previously
undiagnosed read-amplification defects (measured at 87.33s in isolation
against the real retained corpus on unmodified ``main``):

1. ``overview_extensions_value`` (``operator_api.py``) constructed
   ``application.counterfactual_followup.PlanFollowUpObservations`` against
   the real, unwrapped repositories -- the identical defect Operator
   Console Follow-Up Performance v1 already fixed for
   ``operator_console_followups``, at a separate, previously-untouched call
   site. The fix reuses the identical request-scoped
   ``_PrefetchedDecisions``/``_PrefetchedProjections``/
   ``_MemoizedCryptoUniverseObservations`` wrappers -- see
   ``test_postgres_operator_console_followups_performance.py`` for their
   own exhaustive correctness/call-count proofs, not repeated here.

2. ``research_operations_value`` (``operator_api.py``, called directly by
   both ``operator_console_home`` and ``operator_console_attention``)
   passed the real, unwrapped ``research_operations_repository``/
   ``research_operations_knowledge`` singletons into
   ``application.research_operations.research_operations_workspace``. That
   function's own ``duplicate_decision``/``_proposal_knowledge`` helpers
   (invoked once per retained proposal from ``research_operations_workspace``'s
   own comprehension, again once per retained proposal-priority row from
   its nested ``research_review_queue`` call, and again from its
   ``agenda_items`` scan) each re-fetch ``repository.proposals()`` and
   re-derive ``proposal_state()`` (-> ``repository.reviews()``) for every
   retained proposal from scratch, with zero caching. Measured directly
   against the real retained corpus (28 proposals, 54 agenda items, 27
   proposal-priority rows), unprofiled: ~6.7-6.9s wall-clock across three
   repeated runs, with ``repository.reviews()`` alone called 1676 times (at
   most 28 of which can ever be distinct), ``repository.proposals()`` 213
   times, ``knowledge_source.knowledge()`` 55 times,
   ``repository.budget_events()`` 55 times, and ``repository.recommendations()``
   29 times.

``_MemoizedResearchOperationsRepository``/``_MemoizedResearchKnowledgeSource``
satisfy the exact same Protocols with memoized/cached reads instead.
``research_operations_workspace``, ``research_review_queue``,
``duplicate_decision``, ``_proposal_knowledge``, ``proposal_state``, and
``classify_research_proposal`` all receive zero line changes -- these
tests exercise the real, unmodified functions throughout; only the
dependency objects fed into them differ. These tests prove:

  A. the memoized wrapper never calls the genuinely expensive dependencies
     (``proposals()``, ``recommendations()``, ``knowledge()``) more than
     once per instance, and never calls the per-id dependencies
     (``reviews()``, ``budget_events()``) more than once per distinct id,
     regardless of how many proposals/agenda items exist (deterministic
     call-COUNT bounds, never flaky wall-clock ones);
  B/E. wrapped and unwrapped dependency construction produce byte-identical
     ``research_operations_workspace`` output;
  G. nothing this milestone added ever mutates retained evidence;
  H. the read model behaves correctly with zero and sparse proposal state,
     and the ``research_review_queue`` lineage-complete path (proposal +
     agenda item + feasibility all present) still works correctly through
     the wrapper's passthrough methods.

A genuine 521-decision/28-proposal reproduction is not something a
persistence test should reseed -- that evidence lives in this milestone's
own investigation (bounded reproduction/measurement scripts run directly
against the real retained ``apex`` database, read-only, documented in the
final report).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
    _CryptoFollowUpPriceSource,
)
from apex.adapters.counterfactual_ledger_postgres import PostgresCounterfactualLedgerRepository
from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
from apex.adapters.research_operations_postgres import (
    PostgresResearchOperationsRepository,
    PostgresRetainedResearchKnowledgeSource,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.application.counterfactual_followup import PlanFollowUpObservations
from apex.application.research_operations import (
    CreateResearchProposal,
    EmptyResearchKnowledgeSource,
    RefreshResearchAgenda,
    ReviewResearchProposal,
    duplicate_decision,
    research_operations_workspace,
    research_review_queue,
)
from apex.domain.counterfactual_followup import current_followup_policy_version
from apex.domain.research_operations import (
    ResearchAgendaSourceFact,
    ResearchOperationsError,
    ResearchProposal,
    ResearchProposalClass,
    ResearchProposalFeasibility,
    ResearchProposalOrigin,
    ResearchProposalPriority,
    ResearchProposalState,
    ResearchVenue,
)
from apex.domain.values import content_digest
from apex.operator_api import (
    _MemoizedResearchKnowledgeSource,
    _MemoizedResearchOperationsRepository,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)


# --------------------------------------------------------------------------
# Seeding helpers.
# --------------------------------------------------------------------------


def _fact(identity: str, *, observed_at: datetime = NOW) -> ResearchAgendaSourceFact:
    return ResearchAgendaSourceFact.create(
        fact_id=f"home-perf-{identity}",
        fact_type="ZERO_SHARED_ELIGIBLE",
        venue=ResearchVenue.CROSS_VENUE,
        market_scope=("CRYPTO", "KALSHI"),
        state="BLOCKED",
        summary=f"Home performance fixture fact {identity}.",
        evidence_ids=(f"home-perf-evidence-{identity}",),
        evidence_hashes=(content_digest((identity, "fact")),),
        observed_at=observed_at,
    )


def _proposal(
    identity: str, *, agenda_item_id: str, created_at: datetime = NOW
) -> ResearchProposal:
    return ResearchProposal.create(
        proposal_id=f"home-perf-proposal-{identity}",
        version=1,
        agenda_item_id=agenda_item_id,
        venue=ResearchVenue.CRYPTO,
        hypothesis="Home performance fixture hypothesis.",
        expected_mechanism="Home performance fixture mechanism.",
        instruments=("HOME_PERF_FIXTURE",),
        proposed_family=f"HOME_PERF_FAMILY_{identity}",
        definition_kind="EXISTING_RESEARCH_REPLICATION",
        strategy_definition=(("existing_experiment_id", f"home-perf-{identity}"),),
        exact_change_from_prior="HOME_PERF_FIXTURE_WINDOW",
        prior_research_ids=(),
        required_data_ids=(f"home-perf-evidence-{identity}",),
        experiment_configuration=(("existing_experiment_id", f"home-perf-{identity}"),),
        validation_windows=(("replication", "home-perf-fixture-window"),),
        cost_assumptions=(("fee_policy", "fixture-exact"),),
        success_criteria=("POSITIVE_CONSERVATIVE_NET_GAP",),
        failure_criteria=("NON_POSITIVE_CONSERVATIVE_NET_GAP",),
        maximum_experiment_runs=1,
        origin=ResearchProposalOrigin.OPERATOR,
        actor="operator:home-performance-test",
        warnings=("FIXTURE_ONLY",),
        created_at=created_at,
    )


def _seed_proposal(
    engine: Engine, *, identity: str, review: bool = True
) -> tuple[ResearchProposal, str]:
    """Seeds one real, retained agenda item + proposal (+ one
    ``READY_FOR_REVIEW`` review by default) via the genuine production
    services -- ``RefreshResearchAgenda``/``CreateResearchProposal``/
    ``ReviewResearchProposal`` -- exactly the path a real generator run
    would take. Returns ``(proposal, agenda_item_id)``."""

    repository = PostgresResearchOperationsRepository(engine)
    fact = _fact(identity)
    agenda = RefreshResearchAgenda(repository).execute(facts=(fact,)).items
    agenda_item_id = agenda[0].agenda_item_id
    proposal = _proposal(identity, agenda_item_id=agenda_item_id)
    CreateResearchProposal(repository).execute(proposal)
    if review:
        decision = duplicate_decision(repository, proposal, EmptyResearchKnowledgeSource())
        ReviewResearchProposal(repository).execute(
            proposal_id=proposal.proposal_id,
            target_state=ResearchProposalState.READY_FOR_REVIEW,
            actor="operator:home-performance-test",
            reason="HOME_PERFORMANCE_FIXTURE_REVIEW",
            confirmation=(
                f"{ResearchProposalState.READY_FOR_REVIEW.value}:"
                f"{proposal.proposal_id}:{decision.canonical_hash}"
            ),
            reviewed_at=NOW,
        )
    return proposal, agenda_item_id


def _seed_priority_and_feasibility(
    engine: Engine, *, proposal: ResearchProposal, generator_run_id: str
) -> None:
    """Seeds a retained ``ResearchProposalPriority``/``ResearchProposalFeasibility``
    pair for ``proposal`` -- the minimum lineage ``research_review_queue``
    requires (proposal + agenda item + feasibility, all retained) to avoid
    raising ``RESEARCH_REVIEW_QUEUE_LINEAGE_INCOMPLETE``."""

    repository = PostgresResearchOperationsRepository(engine)
    decision = duplicate_decision(repository, proposal, EmptyResearchKnowledgeSource())
    priority = ResearchProposalPriority.create(
        generator_run_id=generator_run_id,
        proposal=proposal,
        proposal_class=ResearchProposalClass.REPLICATION,
        importance_score=80,
        evidence_score=70,
        novelty_score=60,
        execution_cost_score=50,
        runtime_score=40,
        decision_value_score=30,
        library_coverage_score=20,
        deterministic_rank=1,
    )
    repository.save_proposal_priority(priority)
    feasibility = ResearchProposalFeasibility.create(
        generator_run_id=generator_run_id,
        proposal=proposal,
        duplicate_decision=decision,
        schema_valid=True,
        data_available=True,
        provider_capability_available=True,
        experiment_engine_compatible=True,
        budget_available=True,
        evidence_class_compatible=True,
        authority_restrictions_passed=True,
        evaluated_at=NOW,
    )
    repository.save_proposal_feasibility(feasibility)


def _wrapped_workspace(engine: Engine) -> dict[str, object]:
    """Exactly the construction ``operator_api.research_operations_value``
    now uses."""

    repository = PostgresResearchOperationsRepository(engine)
    knowledge = PostgresRetainedResearchKnowledgeSource(engine)
    return research_operations_workspace(
        _MemoizedResearchOperationsRepository(repository),
        _MemoizedResearchKnowledgeSource(knowledge),
    )


def _unwrapped_workspace(engine: Engine) -> dict[str, object]:
    """The real, unmodified construction this route used before this
    milestone -- kept only as the ground truth this file's consistency
    tests compare the wrapped construction against."""

    repository = PostgresResearchOperationsRepository(engine)
    knowledge = PostgresRetainedResearchKnowledgeSource(engine)
    return research_operations_workspace(repository, knowledge)


# --------------------------------------------------------------------------
# A: deterministic, non-flaky call-count bounds on the research-operations
# wrapper (never a wall-clock assertion).
# --------------------------------------------------------------------------


def test_memoized_research_operations_repository_fetches_proposals_and_recommendations_at_most_once(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With several proposals, each reviewed (so
    ``research_operations_workspace``'s own comprehension calls
    ``duplicate_decision`` -> ``_proposal_knowledge`` -> ``repository.
    proposals()`` once per proposal, and ``research_review_queue`` is not
    even exercised here), the real, underlying
    ``PostgresResearchOperationsRepository.proposals()``/``recommendations()``
    must be invoked at most once each through the wrapped construction."""

    engine = postgres_engine
    proposal_count = 6
    for index in range(proposal_count):
        _seed_proposal(engine, identity=f"many{index}")

    proposals_call_count = 0
    recommendations_call_count = 0
    real_proposals = PostgresResearchOperationsRepository.proposals
    real_recommendations = PostgresResearchOperationsRepository.recommendations

    def counting_proposals(self: PostgresResearchOperationsRepository):
        nonlocal proposals_call_count
        proposals_call_count += 1
        return real_proposals(self)

    def counting_recommendations(self: PostgresResearchOperationsRepository):
        nonlocal recommendations_call_count
        recommendations_call_count += 1
        return real_recommendations(self)

    monkeypatch.setattr(PostgresResearchOperationsRepository, "proposals", counting_proposals)
    monkeypatch.setattr(
        PostgresResearchOperationsRepository, "recommendations", counting_recommendations
    )

    value = _wrapped_workspace(engine)

    assert len(value["proposals"]) == proposal_count  # type: ignore[arg-type]
    assert proposals_call_count <= 1, (
        f"expected the memoized wrapper to fetch proposals() at most once regardless of "
        f"{proposal_count} retained proposals; observed {proposals_call_count} real fetches"
    )
    assert recommendations_call_count <= 1, (
        f"expected the memoized wrapper to fetch recommendations() at most once; "
        f"observed {recommendations_call_count} real fetches"
    )


def test_memoized_research_operations_repository_fetches_reviews_at_most_once_per_distinct_id(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before this fix, ``duplicate_decision`` -> ``_proposal_knowledge``
    re-derives ``proposal_state()`` (-> ``repository.reviews()``) for
    *every* retained proposal, *once per outer proposal* -- an O(n^2)
    pattern. With ``n`` reviewed proposals this means up to ``n^2`` real
    ``reviews()`` calls before the fix; the wrapped construction must issue
    at most ``n`` real calls (one per distinct ``proposal_id``, ever)."""

    engine = postgres_engine
    proposal_count = 6
    for index in range(proposal_count):
        _seed_proposal(engine, identity=f"reviews{index}")

    reviews_call_count = 0
    real_reviews = PostgresResearchOperationsRepository.reviews

    def counting_reviews(self: PostgresResearchOperationsRepository, proposal_id: str):
        nonlocal reviews_call_count
        reviews_call_count += 1
        return real_reviews(self, proposal_id)

    monkeypatch.setattr(PostgresResearchOperationsRepository, "reviews", counting_reviews)

    value = _wrapped_workspace(engine)

    assert len(value["proposals"]) == proposal_count  # type: ignore[arg-type]
    # Would be up to proposal_count**2 == 36 before this fix (bounded only by
    # duplicate_decision's own per-outer-proposal re-derivation); proves this
    # is a genuine, non-trivial N, not an N<=1 case that would pass trivially.
    assert reviews_call_count <= proposal_count, (
        f"expected the memoized wrapper to fetch reviews() at most once per distinct "
        f"proposal_id ({proposal_count} distinct ids exist); observed {reviews_call_count} "
        f"real fetches (would be up to {proposal_count**2} before this fix)"
    )


def test_memoized_research_operations_repository_fetches_budget_events_at_most_once_per_distinct_id(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = postgres_engine
    proposal_count = 5
    for index in range(proposal_count):
        _seed_proposal(engine, identity=f"budget{index}")

    budget_call_count = 0
    real_budget_events = PostgresResearchOperationsRepository.budget_events

    def counting_budget_events(self: PostgresResearchOperationsRepository, proposal_id: str):
        nonlocal budget_call_count
        budget_call_count += 1
        return real_budget_events(self, proposal_id)

    monkeypatch.setattr(
        PostgresResearchOperationsRepository, "budget_events", counting_budget_events
    )

    value = _wrapped_workspace(engine)

    assert len(value["proposals"]) == proposal_count  # type: ignore[arg-type]
    assert budget_call_count <= proposal_count, (
        f"expected the memoized wrapper to fetch budget_events() at most once per distinct "
        f"proposal_id; observed {budget_call_count} real fetches"
    )


def test_memoized_research_knowledge_source_fetches_knowledge_at_most_once(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``knowledge_source.knowledge()`` (an ``inspect()`` catalog query plus
    four table scans) is called once per ``duplicate_decision()`` --
    ``research_operations_workspace``'s own comprehension alone calls it
    once per retained proposal. The wrapped construction must fetch the
    real ``PostgresRetainedResearchKnowledgeSource.knowledge()`` at most
    once regardless."""

    engine = postgres_engine
    proposal_count = 6
    for index in range(proposal_count):
        _seed_proposal(engine, identity=f"knowledge{index}")

    knowledge_call_count = 0
    real_knowledge = PostgresRetainedResearchKnowledgeSource.knowledge

    def counting_knowledge(self: PostgresRetainedResearchKnowledgeSource):
        nonlocal knowledge_call_count
        knowledge_call_count += 1
        return real_knowledge(self)

    monkeypatch.setattr(PostgresRetainedResearchKnowledgeSource, "knowledge", counting_knowledge)

    value = _wrapped_workspace(engine)

    assert len(value["proposals"]) == proposal_count  # type: ignore[arg-type]
    assert knowledge_call_count <= 1, (
        f"expected the memoized wrapper to fetch knowledge() at most once regardless of "
        f"{proposal_count} retained proposals; observed {knowledge_call_count} real fetches"
    )


# --------------------------------------------------------------------------
# B/E: wrapped and unwrapped dependency construction are byte-identical.
# --------------------------------------------------------------------------


def test_wrapped_research_operations_workspace_matches_unwrapped_at_moderate_scale(
    postgres_engine: Engine,
) -> None:
    """Seeds several reviewed proposals plus one full review-queue-eligible
    lineage (proposal + agenda item + priority + feasibility all
    retained), and asserts the wrapped-dependency
    ``research_operations_workspace`` reports the *exact* same output --
    including its nested ``review_queue`` -- as the real, unwrapped
    repositories. This is the master proof that the performance fix
    changed nothing observable."""

    engine = postgres_engine
    proposal_count = 5
    proposals = []
    for index in range(proposal_count):
        proposal, _ = _seed_proposal(engine, identity=f"scale{index}")
        proposals.append(proposal)
    _seed_priority_and_feasibility(
        engine, proposal=proposals[0], generator_run_id="home-perf-generator-scale"
    )

    wrapped = _wrapped_workspace(engine)
    unwrapped = _unwrapped_workspace(engine)

    assert wrapped == unwrapped
    assert len(wrapped["proposals"]) == proposal_count  # type: ignore[arg-type]
    assert len(wrapped["review_queue"]) == 1  # type: ignore[arg-type]
    assert len(wrapped["agenda_items"]) == proposal_count  # type: ignore[arg-type]


def test_wrapped_research_review_queue_matches_unwrapped(postgres_engine: Engine) -> None:
    """``research_review_queue`` (also called directly by the standalone
    ``/api/v1/research-operations/review-queue`` route, unaffected by this
    milestone) must produce identical output whether called with the
    wrapped or unwrapped dependencies -- this route is not fixed by this
    milestone (out of scope: it does not sit on ``/home``'s request path),
    but the wrapper must still be safe to use with it, since
    ``research_operations_workspace`` itself calls it internally with
    whatever dependencies it was given."""

    engine = postgres_engine
    proposal, _ = _seed_proposal(engine, identity="review-queue-consistency")
    _seed_priority_and_feasibility(
        engine, proposal=proposal, generator_run_id="home-perf-generator-rq"
    )

    repository = PostgresResearchOperationsRepository(engine)
    knowledge = PostgresRetainedResearchKnowledgeSource(engine)
    unwrapped = research_review_queue(repository, knowledge)
    wrapped = research_review_queue(
        _MemoizedResearchOperationsRepository(repository),
        _MemoizedResearchKnowledgeSource(knowledge),
    )
    assert wrapped == unwrapped
    assert len(wrapped) == 1


# --------------------------------------------------------------------------
# H: correctness with no proposals, sparse state, and the lineage-complete
# review-queue path.
# --------------------------------------------------------------------------


def test_wrapped_workspace_handles_zero_proposals(postgres_engine: Engine) -> None:
    engine = postgres_engine
    value = _wrapped_workspace(engine)
    assert value["proposals"] == ()
    assert value["agenda_items"] == ()
    assert value["review_queue"] == ()
    assert value["recommendations"] == ()


def test_wrapped_workspace_handles_one_proposal_without_review(postgres_engine: Engine) -> None:
    engine = postgres_engine
    _seed_proposal(engine, identity="sparse-unreviewed", review=False)
    value = _wrapped_workspace(engine)
    assert len(value["proposals"]) == 1  # type: ignore[arg-type]
    entry = value["proposals"][0]  # type: ignore[index]
    assert entry["state"] == ResearchProposalState.DRAFT


def test_wrapped_workspace_review_queue_raises_identically_on_incomplete_lineage(
    postgres_engine: Engine,
) -> None:
    """A proposal with a priority but no feasibility is genuinely
    incomplete lineage -- both the wrapped and unwrapped constructions must
    fail identically (``ResearchOperationsError``), never silently succeed
    with partial data through the wrapper's passthrough methods."""

    engine = postgres_engine
    proposal, _ = _seed_proposal(engine, identity="incomplete-lineage")
    repository = PostgresResearchOperationsRepository(engine)
    priority = ResearchProposalPriority.create(
        generator_run_id="home-perf-generator-incomplete",
        proposal=proposal,
        proposal_class=ResearchProposalClass.REPLICATION,
        importance_score=50,
        evidence_score=50,
        novelty_score=50,
        execution_cost_score=50,
        runtime_score=50,
        decision_value_score=50,
        library_coverage_score=50,
        deterministic_rank=1,
    )
    repository.save_proposal_priority(priority)
    # Deliberately no feasibility saved.

    with pytest.raises(ResearchOperationsError, match="RESEARCH_REVIEW_QUEUE_LINEAGE_INCOMPLETE"):
        _wrapped_workspace(engine)
    with pytest.raises(ResearchOperationsError, match="RESEARCH_REVIEW_QUEUE_LINEAGE_INCOMPLETE"):
        _unwrapped_workspace(engine)


# --------------------------------------------------------------------------
# G: nothing this milestone's new read path can mutate retained evidence.
# --------------------------------------------------------------------------


def test_no_write_reachable_through_the_research_operations_wrapper(
    postgres_engine: Engine,
) -> None:
    engine = postgres_engine
    proposal, _ = _seed_proposal(engine, identity="writecheck")
    _seed_priority_and_feasibility(
        engine, proposal=proposal, generator_run_id="home-perf-generator-writecheck"
    )

    tables = (
        "research_agenda_item",
        "governed_research_proposal",
        "governed_research_proposal_review",
        "governed_research_recommendation",
        "governed_research_budget_event",
        "research_proposal_priority",
        "research_proposal_feasibility",
        "research_experiment_queue_item",
        "research_experiment_queue_event",
    )
    with engine.connect() as connection:
        before = {
            table: connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()
            for table in tables
        }

    value = _wrapped_workspace(engine)
    assert len(value["proposals"]) == 1  # type: ignore[arg-type]
    assert len(value["review_queue"]) == 1  # type: ignore[arg-type]

    with engine.connect() as connection:
        after = {
            table: connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()
            for table in tables
        }
    assert before == after


# --------------------------------------------------------------------------
# The crypto/follow-up half of this milestone reuses the exact wrapper
# classes Operator Console Follow-Up Performance v1 already introduced and
# already exhaustively tests (see
# test_postgres_operator_console_followups_performance.py) -- this test
# proves only the specific NEW composition ``overview_extensions_value``
# uses (decisions fetched first, then fed as prefetch into both
# ``coverage()`` and the plan construction) produces the identical
# ``FollowUpPlan`` a fully unwrapped construction would, at a small scale
# including a provably ``PERMANENTLY_UNAVAILABLE`` CRYPTO case.
# --------------------------------------------------------------------------


def _seed_followup_decision(
    engine: Engine, *, identity: str, evaluated_at: datetime, **projection_overrides: object
) -> str:
    """``projection_overrides`` must include ``venue=SharedVenue....`` --
    there is no default, so a caller can never accidentally seed the wrong
    venue by omission."""

    from apex.application.counterfactual_ledger import SyncCounterfactualLedgerFromSharedOpportunity
    from apex.domain.counterfactual_ledger import build_decision_from_eligibility
    from apex.domain.shared_opportunity import SharedOpportunityProjection, build_opportunity_set

    assert "venue" in projection_overrides, "caller must pass an explicit venue=SharedVenue...."
    values: dict[str, object] = {
        "projection_id": f"home-perf-proj-{identity}",
        "source_record_type": "FixtureOpportunity",
        "source_record_id": f"home-perf-source-{identity}",
        "source_canonical_hash": content_digest((identity, "followup")),
        "strategy_or_method_id": "HOME_PERF_FIXTURE_V1",
        "instrument_id": identity.upper(),
        "product_or_market_id": f"{identity.upper()}-USD",
        "direction": "HYPOTHETICAL_LONG_SPOT_RESEARCH",
        "generated_at": evaluated_at + timedelta(hours=12),
        "valid_until": evaluated_at + timedelta(days=200),
        "evidence_class": "HISTORICAL_PUBLIC",
        "freshness_seconds": 60,
        "freshness_limit_seconds": 3600,
        "expected_gross_return": None,
        "expected_net_return": None,
        "comparable_value": "0.10",
        "comparable_value_identity": None,
        "uncertainty": "0.10",
        "maximum_loss": "50",
        "modeled_fees": "1",
        "modeled_slippage": "1",
        "costs_retained": True,
        "liquidity_state": "SUFFICIENT",
        "liquidity_limitations": (),
        "required_hypothetical_capital": "100",
        "maximum_sensible_research_allocation": "200",
        "source_maximum_allocation_fraction": "0.5",
        "expected_holding_days": 30,
        "estimated_lockup_days": 30,
        "supporting_evidence_ids": (f"home-perf-evidence-{identity}",),
        "source_warnings": (),
        "source_rejection_reasons": (),
        "source_authority": "NONE",
        "projection_limitations": (),
        "source_details": (),
    }
    values.update(projection_overrides)
    projection = SharedOpportunityProjection.create(**values)  # type: ignore[arg-type]
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id=f"home-perf-set-{identity}",
        created_at=evaluated_at,
        projections=(projection,),
    )
    PostgresSharedOpportunityRepository(engine).save_opportunity_bundle(
        idempotency_key=f"home-perf-set-{identity}-bundle",
        opportunity_set=opportunity_set,
        projections=(projection,),
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    ledger = PostgresCounterfactualLedgerRepository(engine)
    result = SyncCounterfactualLedgerFromSharedOpportunity(
        ledger, ledger.shared_opportunity
    ).execute(evaluated_at=evaluated_at)
    assert result.decision_ids
    return build_decision_from_eligibility(
        decision=decisions[0],
        projection=projection,
        opportunity_set_id=opportunity_set.opportunity_set_id,
    ).decision_id


def test_home_specific_followup_plan_composition_matches_unwrapped(
    postgres_engine: Engine,
) -> None:
    """Replicates the exact construction ``overview_extensions_value`` now
    uses (fetch ``decisions`` once, pass it into both ``coverage()`` and
    the plan) and proves it matches a fully unwrapped, fresh-fetch-every-time
    construction -- including a CRYPTO decision with zero retained price
    history (provably ``PERMANENTLY_UNAVAILABLE``)."""

    from apex.domain.shared_opportunity import SharedVenue
    from apex.operator_api import (
        _MemoizedCryptoUniverseObservations,
        _PrefetchedDecisions,
        _PrefetchedProjections,
    )

    engine = postgres_engine
    decision_day = datetime(2026, 7, 3, tzinfo=UTC)

    resolvable_id = _seed_followup_decision(
        engine,
        identity="home-resolvable",
        evaluated_at=decision_day,
        product_or_market_id="HOMERESOLV-USD",
        instrument_id="HOMERESOLV",
        venue=SharedVenue.CRYPTO,
    )
    crypto_universe = PostgresCryptoUniverseRepository(engine)
    from apex.domain.crypto_universe import CryptoDailyObservation, CryptoProductObservation
    from apex.domain.research_evidence import EvidenceClass

    crypto_universe.save_observation(
        CryptoProductObservation.create(
            observation_id="home-perf-resolvable-bar",
            source_capture_id="home-perf-resolvable-bar-capture",
            product_id="HOMERESOLV-USD",
            interval_start=decision_day - timedelta(days=3),
            interval_end=decision_day + timedelta(days=200),
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=decision_day - timedelta(days=1), close="100", volume="1"
                ),
            ),
            missing_buckets=0,
            duplicate_records=0,
            rejected_records=0,
            request_count=1,
            raw_evidence_hashes=(content_digest(("home-perf-resolvable-bar",)),),
            source_contract_version="TEST_CONTRACT_V1",
            parser_version="TEST_PARSER_V1",
            checkpoint_ordinal=1,
            completed_at=decision_day - timedelta(hours=1),
            canonical_asset_id="HOMERESOLV",
        )
    )

    unavailable_id = _seed_followup_decision(
        engine,
        identity="home-unavailable",
        evaluated_at=decision_day,
        product_or_market_id="HOMEGONE-USD",
        instrument_id="HOMEGONE",
        venue=SharedVenue.CRYPTO,
    )
    # Deliberately zero crypto observations retained for HOMEGONE-USD.

    counterfactual_ledger_repository = PostgresCounterfactualLedgerRepository(engine)
    counterfactual_followup_repository = PostgresCounterfactualFollowUpRepository(engine)
    shared_opportunity_repository = PostgresSharedOpportunityRepository(engine)
    as_of = decision_day + timedelta(days=45)

    # -- the new, home-specific composition --
    observations = counterfactual_followup_repository.observations()
    already_observed_ids = {item.observation_id for item in observations}
    decisions = counterfactual_ledger_repository.decisions()
    followup_coverage = counterfactual_followup_repository.coverage(
        decisions=decisions, observations=observations
    )
    projections_by_id = shared_opportunity_repository.projections_by_id(
        {item.subject_projection_id for item in decisions}
    )
    wrapped_plan = PlanFollowUpObservations(
        _PrefetchedDecisions(decisions),
        _PrefetchedProjections(projections_by_id),
        _CryptoFollowUpPriceSource(_MemoizedCryptoUniverseObservations(crypto_universe)),
    ).execute(
        as_of=as_of,
        already_observed=already_observed_ids,
        policy_version=current_followup_policy_version(SharedVenue.CRYPTO),
    )

    # -- the old, fully unwrapped construction --
    unwrapped_plan = PlanFollowUpObservations(
        counterfactual_ledger_repository,
        shared_opportunity_repository,
        counterfactual_followup_repository.crypto_prices,
    ).execute(
        as_of=as_of,
        already_observed=already_observed_ids,
        policy_version=current_followup_policy_version(SharedVenue.CRYPTO),
    )
    unwrapped_coverage = counterfactual_followup_repository.coverage()

    assert wrapped_plan == unwrapped_plan
    assert followup_coverage == unwrapped_coverage
    assert wrapped_plan.considered_decisions == 2

    state_by_decision: dict[str, set[str]] = {}
    for entry in wrapped_plan.entries:
        state_by_decision.setdefault(entry.decision_id, set()).add(entry.state.value)
    assert "DUE" in state_by_decision[resolvable_id]
    assert "PERMANENTLY_UNAVAILABLE" in state_by_decision[unavailable_id]
