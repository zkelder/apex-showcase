from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.adapters.research_operations_executor import (
    GovernedResearchExecutor,
    RetainedOutcomeAggregateExperimentExecutor,
)
from apex.adapters.research_operations_sources import RetainedResearchAgendaSource
from apex.application.research_operations import (
    CancelQueuedResearchExperiment,
    CreateResearchProposal,
    EnqueueResearchExperiment,
    ExecuteApprovedResearchQueueItem,
    GenerateResearchProposals,
    InMemoryResearchOperationsRepository,
    RefreshResearchAgenda,
    ResearchProposalGenerationContext,
    ReviewResearchProposal,
    RunGovernedResearchExperiment,
    duplicate_decision,
    proposal_state,
)
from apex.domain.research_operations import (
    DuplicateClassification,
    PriorResearchKnowledge,
    ResearchAgendaSourceFact,
    ResearchExperimentOutcome,
    ResearchExperimentQueueEvent,
    ResearchExperimentQueueStatus,
    ResearchFeasibilityState,
    ResearchOperationsError,
    ResearchProposal,
    ResearchProposalClass,
    ResearchProposalOrigin,
    ResearchProposalState,
    ResearchRecommendationState,
    ResearchVenue,
    recommend_research_outcome,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
HASH = "a" * 64


class Knowledge:
    def __init__(self, *values: PriorResearchKnowledge) -> None:
        self._values = values

    def knowledge(self) -> tuple[PriorResearchKnowledge, ...]:
        return self._values


class Executor:
    def __init__(
        self,
        *,
        net: Decimal | None = Decimal("0.04"),
        uncertainty: bool = True,
        liquidity: bool = True,
        downside: bool = True,
        scopes: int = 3,
        improved: bool = True,
        completed: bool = True,
    ) -> None:
        self.net = net
        self.uncertainty = uncertainty
        self.liquidity = liquidity
        self.downside = downside
        self.scopes = scopes
        self.improved = improved
        self.completed = completed
        self.calls = 0

    def execute(
        self, *, proposal: ResearchProposal, run_id: str, executed_at: datetime
    ) -> ResearchExperimentOutcome:
        self.calls += 1
        return ResearchExperimentOutcome.create(
            run_id=run_id,
            proposal_id=proposal.proposal_id,
            existing_experiment_id="existing-family-1",
            existing_trial_ids=("existing-trial-1",),
            result_hash=HASH,
            completed=self.completed,
            reproducible=self.completed,
            expected_net_economics=self.net,
            uncertainty_sufficient=self.uncertainty,
            liquidity_sufficient=self.liquidity,
            downside_within_limit=self.downside,
            robust_scope_count=self.scopes,
            comparison_improved=self.improved,
            warnings=(),
            completed_at=executed_at,
        )


class _FakeOutcomeAggregate:
    """Duck-typed stand-in for ``CounterfactualOutcomeAggregate``/
    ``SecuritiesStructureOutcomeAggregate`` -- ``RetainedOutcomeAggregate
    ExperimentExecutor`` only ever reads these four attributes, so a full
    29-field domain fixture would test nothing extra. Not typechecked
    (tests are outside mypy's --strict scope), so this stays intentionally
    minimal rather than mirroring the real dataclass shape."""

    def __init__(
        self,
        *,
        cohort_id: str,
        mean_subsequent_return: Decimal | None,
        contributing_decision_ids: tuple[str, ...] = ("decision-1", "decision-2"),
        canonical_hash: str = HASH,
    ) -> None:
        self.cohort_id = cohort_id
        self.mean_subsequent_return = mean_subsequent_return
        self.contributing_decision_ids = contributing_decision_ids
        self.canonical_hash = canonical_hash


class _FakeOutcomeAggregateSource:
    def __init__(self, *aggregates: _FakeOutcomeAggregate) -> None:
        self._aggregates = aggregates

    def earliest_qualified_per_cohort(self) -> tuple[_FakeOutcomeAggregate, ...]:
        return self._aggregates


def _raising_executor() -> object:
    class _Raises:
        def execute(self, **_kwargs: object) -> ResearchExperimentOutcome:
            raise AssertionError("this executor must never be called for this proposal family")

    return _Raises()


def fact(fact_id: str = "negative-economics") -> ResearchAgendaSourceFact:
    return ResearchAgendaSourceFact.create(
        fact_id=fact_id,
        fact_type="NEGATIVE_EXPECTED_ECONOMICS",
        venue=ResearchVenue.CRYPTO,
        market_scope=("CURRENT_CANONICAL_CRYPTO_UNIVERSE",),
        state="BLOCKED",
        summary="Current reversal-regime expected net economics remain negative.",
        evidence_ids=("reversal-run-1",),
        evidence_hashes=(HASH,),
        observed_at=NOW,
    )


def proposal(
    proposal_id: str = "proposal-1",
    *,
    origin: ResearchProposalOrigin = ResearchProposalOrigin.OPERATOR,
    definition: tuple[tuple[str, str], ...] = (("lookback", "14"),),
    configuration: tuple[tuple[str, str], ...] = (("dataset", "current-730-day"),),
    definition_kind: str = "CRYPTO_GENERIC_LONG_CASH",
    exact_change: str = "MATERIAL_MECHANISM_CHANGE_FROM_RETAINED_REVERSAL",
    required_data: tuple[str, ...] = ("current-730-day",),
    proposed_family: str = "GENERIC_LONG_CASH",
) -> ResearchProposal:
    return ResearchProposal.create(
        proposal_id=proposal_id,
        version=1,
        agenda_item_id="agenda-negative-economics",
        venue=ResearchVenue.CRYPTO,
        hypothesis="A bounded retained-evidence mechanism may improve net economics.",
        expected_mechanism="Different completed-close behavior with next-open execution.",
        instruments=("CURRENT_CANONICAL_CRYPTO_UNIVERSE",),
        proposed_family=proposed_family,
        definition_kind=definition_kind,
        strategy_definition=definition,
        exact_change_from_prior=exact_change,
        prior_research_ids=("reversal-regime-v1",),
        required_data_ids=required_data,
        experiment_configuration=configuration,
        validation_windows=(("development", "2024-08-01/2026-08-01"),),
        cost_assumptions=(("round_trip_bps", "80"),),
        success_criteria=("EXPECTED_NET_POSITIVE", "DOWNSIDE_WITHIN_LIMIT"),
        failure_criteria=("EXPECTED_NET_NON_POSITIVE",),
        maximum_experiment_runs=2,
        origin=origin,
        actor="operator:zach",
        warnings=("RESEARCH_ONLY",),
        created_at=NOW,
    )


def setup_repository(
    value: ResearchProposal | None = None,
) -> tuple[InMemoryResearchOperationsRepository, ResearchProposal]:
    repository = InMemoryResearchOperationsRepository()
    result = RefreshResearchAgenda(repository).execute(facts=(fact(),))
    assert result.items[0].authority == "NONE"
    retained = value or proposal()
    CreateResearchProposal(repository).execute(retained)
    return repository, retained


def transition(
    repository: InMemoryResearchOperationsRepository,
    value: ResearchProposal,
    state: ResearchProposalState,
    *,
    knowledge: Knowledge | None = None,
    actor: str = "operator:zach",
) -> None:
    source = knowledge or Knowledge()
    decision = duplicate_decision(repository, value, source)
    ReviewResearchProposal(repository, source).execute(
        proposal_id=value.proposal_id,
        target_state=state,
        actor=actor,
        reason=f"TEST_{state.value}",
        confirmation=f"{state.value}:{value.proposal_id}:{decision.canonical_hash}",
        reviewed_at=NOW,
        idempotency_key=(
            f"approval-key-{value.proposal_id}"
            if state is ResearchProposalState.APPROVED_FOR_EXPERIMENT
            else None
        ),
    )


def test_agenda_generation_is_fact_bound_and_idempotent() -> None:
    repository = InMemoryResearchOperationsRepository()
    service = RefreshResearchAgenda(repository)
    first = service.execute(facts=(fact(),))
    second = service.execute(facts=(fact(),))

    assert first.items[0].suggested_next_step == "REVISE_MECHANISM_OR_RETAIN_NEGATIVE_KNOWLEDGE"
    assert first.items[0].source_fact_hash == fact().canonical_hash
    assert second.replayed_item_ids == ("agenda-negative-economics",)
    assert repository.agenda_items() == first.items


def test_health_agenda_source_preserves_supported_venue_semantics() -> None:
    assert RetainedResearchAgendaSource._health_venue("KALSHI_METHOD_V1") is ResearchVenue.KALSHI
    assert RetainedResearchAgendaSource._health_venue("COINBASE_CRYPTO_METHOD_V1") is (
        ResearchVenue.CRYPTO
    )
    assert RetainedResearchAgendaSource._health_venue("ALPACA_EXTERNAL_PAPER_V1") is None


def test_proposal_rejects_arbitrary_definition_kind_and_missing_evidence() -> None:
    with pytest.raises(ResearchOperationsError, match="DEFINITION_KIND_UNSUPPORTED"):
        proposal(definition_kind="EXECUTABLE_PYTHON")
    with pytest.raises(ResearchOperationsError, match="EVIDENCE_OR_CRITERIA_MISSING"):
        proposal(required_data=())


def test_duplicate_gate_distinguishes_exact_parameter_replication_challenger_and_new() -> None:
    value = proposal()
    exact = PriorResearchKnowledge(
        "prior-exact",
        value.venue,
        value.proposed_family,
        value.semantic_hash,
        value.experiment_configuration_hash,
        value.validation_windows,
        True,
        HASH,
    )
    assert (
        duplicate_decision(setup_repository(value)[0], value, Knowledge(exact)).classification
        is DuplicateClassification.EXACT_DUPLICATE
    )

    changed = proposal(configuration=(("dataset", "replication"),))
    parameter = replace(exact, experiment_configuration_hash="b" * 64)
    decision = duplicate_decision(setup_repository(changed)[0], changed, Knowledge(parameter))
    assert decision.classification is DuplicateClassification.PARAMETER_ONLY_VARIATION
    assert decision.approval_permitted is False

    replication = proposal(
        definition_kind="EXISTING_RESEARCH_REPLICATION",
        configuration=(("dataset", "disjoint"),),
    )
    replication_prior = replace(
        exact,
        semantic_hash=replication.semantic_hash,
        experiment_configuration_hash="c" * 64,
        validation_windows=(("prior", "2020/2022"),),
    )
    assert (
        duplicate_decision(
            setup_repository(replication)[0], replication, Knowledge(replication_prior)
        ).classification
        is DuplicateClassification.VALID_REPLICATION
    )

    challenger_prior = replace(exact, semantic_hash="d" * 64)
    assert (
        duplicate_decision(
            setup_repository(value)[0], value, Knowledge(challenger_prior)
        ).classification
        is DuplicateClassification.MATERIALLY_DIFFERENT_CHALLENGER
    )
    assert (
        duplicate_decision(setup_repository(value)[0], value, Knowledge()).classification
        is DuplicateClassification.GENUINELY_NEW_METHOD_FAMILY
    )


def test_metadata_only_change_is_rejected_by_duplicate_gate() -> None:
    value = proposal(exact_change="METADATA_ONLY_LABEL_CHANGE")
    prior = PriorResearchKnowledge(
        "prior-semantic-match",
        value.venue,
        value.proposed_family,
        value.semantic_hash,
        "b" * 64,
        value.validation_windows,
        False,
        HASH,
    )

    decision = duplicate_decision(setup_repository(value)[0], value, Knowledge(prior))

    assert decision.classification is DuplicateClassification.METADATA_ONLY_CHANGE
    assert decision.approval_permitted is False


def test_exact_duplicate_cannot_be_approved() -> None:
    repository, value = setup_repository()
    exact = PriorResearchKnowledge(
        "prior-exact",
        value.venue,
        value.proposed_family,
        value.semantic_hash,
        value.experiment_configuration_hash,
        value.validation_windows,
        True,
        HASH,
    )
    knowledge = Knowledge(exact)
    transition(repository, value, ResearchProposalState.READY_FOR_REVIEW, knowledge=knowledge)
    decision = duplicate_decision(repository, value, knowledge)
    with pytest.raises(ResearchOperationsError, match="DUPLICATE_APPROVAL_FORBIDDEN"):
        ReviewResearchProposal(repository, knowledge).execute(
            proposal_id=value.proposal_id,
            target_state=ResearchProposalState.APPROVED_FOR_EXPERIMENT,
            actor="operator:zach",
            reason="must fail",
            confirmation=f"APPROVED_FOR_EXPERIMENT:{value.proposal_id}:{decision.canonical_hash}",
            reviewed_at=NOW,
            idempotency_key="duplicate-approval-key",
        )


def test_ai_origin_requires_exact_operator_review_and_cannot_self_approve() -> None:
    repository, value = setup_repository(proposal(origin=ResearchProposalOrigin.AI_ASSISTED))
    transition(repository, value, ResearchProposalState.READY_FOR_REVIEW)
    decision = duplicate_decision(repository, value, Knowledge())
    with pytest.raises(ResearchOperationsError, match="OPERATOR_APPROVAL_REQUIRED"):
        ReviewResearchProposal(repository).execute(
            proposal_id=value.proposal_id,
            target_state=ResearchProposalState.APPROVED_FOR_EXPERIMENT,
            actor="ai:assistant",
            reason="invalid self approval",
            confirmation=f"APPROVED_FOR_EXPERIMENT:{value.proposal_id}:{decision.canonical_hash}",
            reviewed_at=NOW,
        )


@pytest.mark.parametrize(
    ("executor", "expected"),
    (
        (Executor(net=Decimal("-0.01")), ResearchRecommendationState.REJECT),
        (Executor(net=None), ResearchRecommendationState.INSUFFICIENT_EVIDENCE),
        (Executor(liquidity=False), ResearchRecommendationState.REVISE),
        (Executor(scopes=1), ResearchRecommendationState.REPLICATE),
        (Executor(), ResearchRecommendationState.PAPER_CANDIDATE),
    ),
)
def test_governed_experiment_recommendations_have_zero_authority(
    executor: Executor, expected: ResearchRecommendationState
) -> None:
    repository, value = setup_repository()
    transition(repository, value, ResearchProposalState.READY_FOR_REVIEW)
    transition(repository, value, ResearchProposalState.APPROVED_FOR_EXPERIMENT)
    result = RunGovernedResearchExperiment(repository, executor).execute(
        proposal_id=value.proposal_id,
        run_id=f"run-{expected.value.lower()}",
        actor="operator:zach",
        confirmation=f"RUN:{value.proposal_id}:run-{expected.value.lower()}:{value.canonical_hash}",
        executed_at=NOW,
    )

    assert result.recommendation.state is expected
    assert result.recommendation.authority == "NONE"
    assert result.recommendation.paper_authorization_created is False
    assert result.recommendation.live_authority == "NONE"
    assert result.budget.consumed_runs == 1
    assert proposal_state(repository, value.proposal_id) is ResearchProposalState.COMPLETED


def test_exact_replay_does_not_consume_budget() -> None:
    repository, value = setup_repository()
    transition(repository, value, ResearchProposalState.READY_FOR_REVIEW)
    transition(repository, value, ResearchProposalState.APPROVED_FOR_EXPERIMENT)
    executor = Executor()
    service = RunGovernedResearchExperiment(repository, executor)
    confirmation = f"RUN:{value.proposal_id}:run-1:{value.canonical_hash}"
    first = service.execute(
        proposal_id=value.proposal_id,
        run_id="run-1",
        actor="operator:zach",
        confirmation=confirmation,
        executed_at=NOW,
    )
    second = service.execute(
        proposal_id=value.proposal_id,
        run_id="run-1",
        actor="operator:zach",
        confirmation=confirmation,
        executed_at=NOW + timedelta(minutes=1),
    )
    third = service.execute(
        proposal_id=value.proposal_id,
        run_id="run-1",
        actor="operator:zach",
        confirmation=confirmation,
        executed_at=NOW + timedelta(minutes=2),
    )

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert third.idempotent_replay is True
    assert second.budget.consumed_runs == 1
    assert second.budget.replays == 1
    assert third.budget.replays == 1


def test_incomplete_experiment_consumes_one_failed_run_budget_unit() -> None:
    repository, value = setup_repository()
    transition(repository, value, ResearchProposalState.READY_FOR_REVIEW)
    transition(repository, value, ResearchProposalState.APPROVED_FOR_EXPERIMENT)

    result = RunGovernedResearchExperiment(repository, Executor(completed=False)).execute(
        proposal_id=value.proposal_id,
        run_id="run-incomplete",
        actor="operator:zach",
        confirmation=f"RUN:{value.proposal_id}:run-incomplete:{value.canonical_hash}",
        executed_at=NOW,
    )

    assert result.recommendation.state is ResearchRecommendationState.INSUFFICIENT_EVIDENCE
    assert result.budget.failed_runs == 1
    assert result.budget.experiment_runs == 0
    assert result.budget.consumed_runs == 1


def test_bounded_generator_is_deterministic_and_classifies_unsupported_work() -> None:
    repository = InMemoryResearchOperationsRepository()
    facts = (
        fact("negative_expected_economics-current-reversal"),
        ResearchAgendaSourceFact.create(
            fact_id="zero_paper_ready-current-allocation",
            fact_type="ZERO_PAPER_READY",
            venue=ResearchVenue.CROSS_VENUE,
            market_scope=("CURRENT_CROSS_VENUE_ALLOCATION",),
            state="BLOCKED",
            summary="No current method is connected to a PAPER-ready research proposal.",
            evidence_ids=("allocation-1",),
            evidence_hashes=("b" * 64,),
            observed_at=NOW,
        ),
        ResearchAgendaSourceFact.create(
            fact_id="exhaustiveness_unproven-current-event",
            fact_type="EXHAUSTIVENESS_UNPROVEN",
            venue=ResearchVenue.KALSHI,
            market_scope=("KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1",),
            state="BLOCKED",
            summary="Exact exhaustive relationship evidence remains unavailable.",
            evidence_ids=("kalshi-scan-1",),
            evidence_hashes=("c" * 64,),
            observed_at=NOW,
        ),
        ResearchAgendaSourceFact.create(
            fact_id="insufficient_calibration-current-relative-strength",
            fact_type="INSUFFICIENT_CALIBRATION",
            venue=ResearchVenue.CRYPTO,
            market_scope=("CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1",),
            state="BLOCKED",
            summary="Relative-strength sample count is insufficient.",
            evidence_ids=("crypto-calibration-1",),
            evidence_hashes=("d" * 64,),
            observed_at=NOW,
        ),
    )
    agenda = RefreshResearchAgenda(repository).execute(facts=facts).items
    context = ResearchProposalGenerationContext(
        retained_data_ids=tuple(value.source_fact_id for value in agenda),
        executable_experiment_ids=tuple(
            value.market_scope[0] for value in agenda if value.market_scope
        ),
        provider_capability_ids=(),
        remaining_experiment_budget=3,
    )
    first = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-fixture-1",
        generated_at=NOW,
        actor="system:fixture",
        context=context,
    )
    replay = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-fixture-1",
        generated_at=NOW + timedelta(minutes=1),
        actor="system:fixture",
        context=context,
    )

    assert len(first.proposals) == 3
    assert len({value.agenda_item_id for value in first.proposals}) == 3
    assert len({value.proposed_family for value in first.proposals}) == 3
    assert tuple(value.total_score for value in first.priorities) == tuple(
        sorted((value.total_score for value in first.priorities), reverse=True)
    )
    assert replay.idempotent_replay is True
    assert replay.generator_run == first.generator_run
    unsupported = next(
        (
            value
            for value in first.feasibilities
            if repository.proposal(value.proposal_id).definition_kind  # type: ignore[union-attr]
            == "REQUIRES_IMPLEMENTATION"
        ),
        None,
    )
    assert unsupported is not None
    assert unsupported.status is ResearchFeasibilityState.REQUIRES_IMPLEMENTATION
    assert first.generator_run.external_model_called is False


def test_securities_structure_outcome_fact_gets_replication_not_evidence_completion() -> None:
    """Regression test for Research Proposal Coverage & Fairness v1: before
    this milestone, ``_agenda_template`` had no branch recognizing
    ``SECURITIES_STRUCTURE_OUTCOME_PATTERN`` facts (unlike its
    ``COUNTERFACTUAL_OUTCOME_PATTERN`` sibling), so every one fell through
    to the generic ``EVIDENCE_COMPLETION`` fallback -- semantically wrong
    (that template means "evidence is missing", not "a mature qualified
    pattern exists") and the lowest-scoring proposal class in the system."""

    repository = InMemoryResearchOperationsRepository()
    securities_fact = ResearchAgendaSourceFact.create(
        fact_id="securities-structure-outcome-cohort-1",
        fact_type="SECURITIES_STRUCTURE_OUTCOME_PATTERN",
        venue=ResearchVenue.SECURITIES,
        market_scope=("CALL_CREDIT_VERTICAL", "SECURITIES_DEFINED_RISK_OPTIONS_STRUCTURE_V1"),
        state="QUALIFIED",
        summary="A qualified structure-outcome cohort shows a genuine positive pattern.",
        evidence_ids=("securities-structure-outcome-agg-1",),
        evidence_hashes=("f" * 64,),
        observed_at=NOW,
    )
    agenda = RefreshResearchAgenda(repository).execute(facts=(securities_fact,)).items
    context = ResearchProposalGenerationContext(
        retained_data_ids=tuple(value.source_fact_id for value in agenda),
        executable_experiment_ids=(),
        provider_capability_ids=(),
        remaining_experiment_budget=1,
    )
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-securities-fixture",
        generated_at=NOW,
        actor="system:fixture",
        context=context,
        maximum_proposals=1,
    )

    assert len(generated.proposals) == 1
    assert "SECURITIES_STRUCTURE_OUTCOME_POLICY_REVIEW" in generated.proposals[0].proposed_family
    priority_value = generated.priorities[0]
    assert priority_value.proposal_class is ResearchProposalClass.REPLICATION
    assert priority_value.evidence_score == 85  # EVIDENCE_COMPLETION's would be 45.
    # Research Experiment Executability v1: fixed the very next stage this
    # exact proposal stalled at -- without that fix this asserts
    # REQUIRES_IMPLEMENTATION, matching every organically-generated
    # REPLICATION-class proposal ever produced in production (confirmed via
    # a direct read-only production audit: 87 feasibility rows, 0 ever
    # READY_FOR_REVIEW from the automatic generator).
    assert generated.feasibilities[0].status is ResearchFeasibilityState.READY_FOR_REVIEW


def test_counterfactual_outcome_fact_reaches_ready_for_review_without_a_family_match() -> None:
    """Research Experiment Executability v1. ``COUNTERFACTUAL_OUTCOME_
    PATTERN`` facts have used the ``REPLICATION`` template (unlike their
    ``SECURITIES_STRUCTURE_OUTCOME_PATTERN`` sibling above) since Counterfactual
    Outcome Feedback v1 -- but production evidence shows every one of them
    has *also* always landed ``REQUIRES_IMPLEMENTATION``, forever, for a
    structural reason unrelated to routing: ``experiment_engine_compatible``
    required ``existing_experiment_id`` (a human-readable method id such as
    ``CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1``) to appear in
    ``context.executable_experiment_ids`` (opaque, content-hashed
    ``research_experiment_family`` ids from a structurally separate,
    SECURITIES-only pre-registration mechanism that has never once
    registered a CRYPTO/KALSHI method). No family match has ever existed or
    ever will for this template family; ``executable_experiment_ids=()``
    below reproduces that exactly."""

    repository = InMemoryResearchOperationsRepository()
    counterfactual_fact = ResearchAgendaSourceFact.create(
        fact_id="counterfactual-outcome-cohort-1",
        fact_type="COUNTERFACTUAL_OUTCOME_PATTERN",
        venue=ResearchVenue.CRYPTO,
        market_scope=("CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1",),
        state="QUALIFIED",
        summary="A qualified counterfactual-outcome cohort shows a genuine pattern.",
        evidence_ids=("counterfactual-outcome-agg-1",),
        evidence_hashes=("e" * 64,),
        observed_at=NOW,
    )
    agenda = RefreshResearchAgenda(repository).execute(facts=(counterfactual_fact,)).items
    context = ResearchProposalGenerationContext(
        retained_data_ids=tuple(value.source_fact_id for value in agenda),
        executable_experiment_ids=(),
        provider_capability_ids=(),
        remaining_experiment_budget=1,
    )
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-counterfactual-fixture",
        generated_at=NOW,
        actor="system:fixture",
        context=context,
        maximum_proposals=1,
    )

    assert len(generated.proposals) == 1
    assert "COUNTERFACTUAL_OUTCOME_POLICY_REVIEW" in generated.proposals[0].proposed_family
    assert generated.feasibilities[0].status is ResearchFeasibilityState.READY_FOR_REVIEW


def test_unrelated_replication_template_still_requires_implementation_without_a_family_match() -> (
    None
):
    """Regression guard for Research Experiment Executability v1: the new
    exception must be scoped to exactly the two ``_POLICY_REVIEW`` families
    -- ``INSUFFICIENT_CALIBRATION``'s ``FROZEN_SEMANTICS_REPLICATION``
    template is the same ``ResearchProposalClass.REPLICATION`` class with
    ``engine_compatible=True``, but declares no retained-aggregate-only
    scope and has no dedicated executor, so it must still honestly land
    ``REQUIRES_IMPLEMENTATION`` when (as in production) no matching
    ``research_experiment_family`` exists."""

    repository = InMemoryResearchOperationsRepository()
    calibration_fact = ResearchAgendaSourceFact.create(
        fact_id="insufficient_calibration-current-relative-strength",
        fact_type="INSUFFICIENT_CALIBRATION",
        venue=ResearchVenue.CRYPTO,
        market_scope=("CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1",),
        state="BLOCKED",
        summary="Relative-strength sample count is insufficient.",
        evidence_ids=("crypto-calibration-1",),
        evidence_hashes=("d" * 64,),
        observed_at=NOW,
    )
    agenda = RefreshResearchAgenda(repository).execute(facts=(calibration_fact,)).items
    context = ResearchProposalGenerationContext(
        retained_data_ids=tuple(value.source_fact_id for value in agenda),
        executable_experiment_ids=(),
        provider_capability_ids=(),
        remaining_experiment_budget=1,
    )
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-calibration-fixture",
        generated_at=NOW,
        actor="system:fixture",
        context=context,
        maximum_proposals=1,
    )

    assert len(generated.proposals) == 1
    assert "FROZEN_SEMANTICS_REPLICATION" in generated.proposals[0].proposed_family
    assert generated.feasibilities[0].status is ResearchFeasibilityState.REQUIRES_IMPLEMENTATION


def test_generator_reserves_one_slot_for_the_oldest_starved_family() -> None:
    """Regression test for Research Proposal Coverage & Fairness v1: before
    this milestone, the generator's family-deduped, priority-sorted, capped-
    at-3 selection had no fairness guarantee, so a family that always scores
    lower than another, continuously-replenishing family could never win a
    slot -- a fresh, higher-scoring candidate was always available to take
    its place, cycle after cycle, forever. This is the exact shape of the
    real SECURITIES/KALSHI starvation this milestone fixes (0 proposals
    ever, out of 60 combined agenda items, confirmed against production)."""

    repository = InMemoryResearchOperationsRepository()
    older_starved = ResearchAgendaSourceFact.create(
        fact_id="exhaustiveness_unproven-oldest-starved-scope",
        fact_type="EXHAUSTIVENESS_UNPROVEN",
        venue=ResearchVenue.KALSHI,
        market_scope=("KALSHI_OLDEST_STARVED_SCOPE",),
        state="BLOCKED",
        summary="Exact exhaustive relationship evidence remains unavailable.",
        evidence_ids=("kalshi-scan-oldest",),
        evidence_hashes=("g" * 64,),
        observed_at=NOW - timedelta(days=30),
    )
    fresh_facts = tuple(
        ResearchAgendaSourceFact.create(
            fact_id=f"negative_expected_economics-fresh-scope-{index}",
            fact_type="NEGATIVE_EXPECTED_ECONOMICS",
            venue=ResearchVenue.CRYPTO,
            market_scope=(f"CRYPTO_FRESH_SCOPE_{index}",),
            state="BLOCKED",
            summary="Current expected net economics remain negative.",
            evidence_ids=(f"crypto-economics-{index}",),
            evidence_hashes=(chr(ord("h") + index) * 64,),
            observed_at=NOW,
        )
        for index in range(3)
    )
    agenda = RefreshResearchAgenda(repository).execute(facts=(older_starved, *fresh_facts)).items
    context = ResearchProposalGenerationContext(
        retained_data_ids=tuple(value.source_fact_id for value in agenda),
        executable_experiment_ids=(),
        provider_capability_ids=(),
        remaining_experiment_budget=3,
    )
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-fairness-fixture",
        generated_at=NOW,
        actor="system:fixture",
        context=context,
    )

    # Exactly 3 of the 4 candidates fit (the default cap); without the
    # fairness reservation, the 3 fresh, higher-scoring MATERIAL_CHALLENGER
    # candidates would sweep all 3 slots and the older EVIDENCE_COMPLETION
    # candidate would never be selected, no matter how long it waited.
    assert len(generated.proposals) == 3
    selected_agenda_ids = {value.agenda_item_id for value in generated.proposals}
    assert "agenda-exhaustiveness_unproven-oldest-starved-scope" in selected_agenda_ids
    winning_classes = {value.proposal_class for value in generated.priorities}
    assert ResearchProposalClass.EVIDENCE_COMPLETION in winning_classes
    # Still fully deterministic and rank-ordered -- the fairness pick is
    # appended after the priority-sorted winners, never reordering them.
    assert tuple(value.total_score for value in generated.priorities) == tuple(
        sorted((value.total_score for value in generated.priorities), reverse=True)
    )


def test_generator_fails_closed_for_ai_missing_evidence_and_budget() -> None:
    ai_repository = InMemoryResearchOperationsRepository()
    RefreshResearchAgenda(ai_repository).execute(
        facts=(fact("negative_expected_economics-ai-disabled"),)
    )
    with pytest.raises(ResearchOperationsError, match="AI_PROPOSAL_PROVIDER_DISABLED"):
        GenerateResearchProposals(ai_repository).execute(
            generator_run_id="generator-ai-disabled",
            generated_at=NOW,
            actor="ai:disabled",
            origin=ResearchProposalOrigin.AI_ASSISTED,
            context=ResearchProposalGenerationContext((), (), (), 1),
            maximum_proposals=1,
        )

    evidence_repository = InMemoryResearchOperationsRepository()
    evidence_agenda = (
        RefreshResearchAgenda(evidence_repository)
        .execute(
            facts=(
                ResearchAgendaSourceFact.create(
                    fact_id="exhaustiveness_unproven-missing-evidence",
                    fact_type="EXHAUSTIVENESS_UNPROVEN",
                    venue=ResearchVenue.KALSHI,
                    market_scope=("KALSHI_EVENT_SCOPE",),
                    state="BLOCKED",
                    summary="Exact retained rule evidence remains missing.",
                    evidence_ids=("event-scan",),
                    evidence_hashes=("e" * 64,),
                    observed_at=NOW,
                ),
            )
        )
        .items
    )
    evidence = GenerateResearchProposals(evidence_repository).execute(
        generator_run_id="generator-missing-evidence",
        generated_at=NOW,
        actor="system:fixture",
        context=ResearchProposalGenerationContext(
            retained_data_ids=(evidence_agenda[0].source_fact_id,),
            executable_experiment_ids=(),
            provider_capability_ids=(),
            remaining_experiment_budget=1,
        ),
        maximum_proposals=1,
    )
    assert evidence.feasibilities[0].status is ResearchFeasibilityState.REQUIRES_EVIDENCE

    budget_repository = InMemoryResearchOperationsRepository()
    budget_agenda = (
        RefreshResearchAgenda(budget_repository)
        .execute(facts=(fact("negative_expected_economics-budget-blocked"),))
        .items
    )
    budget = GenerateResearchProposals(budget_repository).execute(
        generator_run_id="generator-budget-blocked",
        generated_at=NOW,
        actor="system:fixture",
        context=ResearchProposalGenerationContext(
            retained_data_ids=(budget_agenda[0].source_fact_id,),
            executable_experiment_ids=(budget_agenda[0].market_scope[0],),
            provider_capability_ids=(),
            remaining_experiment_budget=0,
        ),
        maximum_proposals=1,
    )
    assert budget.feasibilities[0].status is ResearchFeasibilityState.BUDGET_BLOCKED


def test_exact_approval_queue_execution_recommendation_and_replay() -> None:
    repository = InMemoryResearchOperationsRepository()
    agenda = (
        RefreshResearchAgenda(repository)
        .execute(facts=(fact("negative_expected_economics-queue"),))
        .items
    )
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-queue-1",
        generated_at=NOW,
        actor="system:fixture",
        context=ResearchProposalGenerationContext(
            retained_data_ids=(agenda[0].source_fact_id,),
            executable_experiment_ids=(agenda[0].market_scope[0],),
            provider_capability_ids=(),
            remaining_experiment_budget=1,
        ),
        maximum_proposals=1,
    )
    value = generated.proposals[0]
    assert generated.feasibilities[0].status is ResearchFeasibilityState.READY_FOR_REVIEW
    transition(repository, value, ResearchProposalState.READY_FOR_REVIEW)
    transition(repository, value, ResearchProposalState.APPROVED_FOR_EXPERIMENT)
    assert repository.experiment_approval(value.proposal_id) is not None
    enqueued = EnqueueResearchExperiment(repository).execute(
        queue_id="queue-1",
        proposal_id=value.proposal_id,
        run_id="queued-run-1",
        actor="operator:zach",
        idempotency_key="enqueue-key-1",
        enqueued_at=NOW,
    )
    assert enqueued.event.to_status is ResearchExperimentQueueStatus.QUEUED
    executor = Executor()
    service = ExecuteApprovedResearchQueueItem(repository, executor)
    first = service.execute(
        queue_id="queue-1",
        actor="operator:zach",
        idempotency_key="execute-key-1",
        executed_at=NOW,
    )
    replay = service.execute(
        queue_id="queue-1",
        actor="operator:zach",
        idempotency_key="execute-key-replay",
        executed_at=NOW + timedelta(minutes=1),
    )

    assert first.event.to_status is ResearchExperimentQueueStatus.COMPLETED
    assert first.governed_result.budget.consumed_runs == 1
    assert replay.idempotent_replay is True
    assert replay.governed_result.budget.consumed_runs == 1
    assert replay.governed_result.budget.replays == 1
    assert first.governed_result.recommendation.paper_authorization_created is False
    assert first.governed_result.recommendation.live_authority == "NONE"


def test_retained_outcome_aggregate_executor_never_reaches_paper_candidate() -> None:
    """``RetainedOutcomeAggregateExperimentExecutor`` never invents a
    liquidity or benchmark-comparison judgment this evidence class cannot
    support, so it can only ever reach ``REJECT`` (evidence confirms the
    current policy) or ``REVISE`` (evidence trends positive but this
    retrospective evidence alone cannot justify more) -- never
    ``PAPER_CANDIDATE``, no matter how positive the retained mean return."""

    negative = _FakeOutcomeAggregate(cohort_id="cohort-1", mean_subsequent_return=Decimal("-0.03"))
    positive = _FakeOutcomeAggregate(cohort_id="cohort-2", mean_subsequent_return=Decimal("0.11"))
    executor = RetainedOutcomeAggregateExperimentExecutor(
        counterfactual_outcome=_FakeOutcomeAggregateSource(negative, positive),
        securities_structure_outcome=_FakeOutcomeAggregateSource(),
    )
    family = "CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1_COUNTERFACTUAL_OUTCOME_POLICY_REVIEW"
    negative_outcome = executor.execute(
        proposal=proposal(
            "counterfactual-outcome-review-proposal-negative",
            proposed_family=family,
            required_data=("counterfactual-outcome-cohort-1",),
        ),
        run_id="run-negative",
        executed_at=NOW,
    )
    positive_outcome = executor.execute(
        proposal=proposal(
            "counterfactual-outcome-review-proposal-positive",
            proposed_family=family,
            required_data=("counterfactual-outcome-cohort-2",),
        ),
        run_id="run-positive",
        executed_at=NOW,
    )

    assert negative_outcome.expected_net_economics == Decimal("-0.03")
    assert recommend_research_outcome(negative_outcome).state is ResearchRecommendationState.REJECT
    assert positive_outcome.expected_net_economics == Decimal("0.11")
    assert recommend_research_outcome(positive_outcome).state is ResearchRecommendationState.REVISE
    assert positive_outcome.existing_trial_ids == ("decision-1", "decision-2")
    assert positive_outcome.robust_scope_count == 2


def test_retained_outcome_aggregate_executor_fails_closed_when_cohort_no_longer_qualified() -> None:
    executor = RetainedOutcomeAggregateExperimentExecutor(
        counterfactual_outcome=_FakeOutcomeAggregateSource(),  # empty: nothing QUALIFIED anymore
        securities_structure_outcome=_FakeOutcomeAggregateSource(),
    )
    stale_proposal = proposal(
        "stale-proposal",
        proposed_family="CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1_COUNTERFACTUAL_OUTCOME_POLICY_REVIEW",
        required_data=("counterfactual-outcome-cohort-1",),
    )

    with pytest.raises(ResearchOperationsError, match="NO_LONGER_QUALIFIED"):
        executor.execute(proposal=stale_proposal, run_id="run-1", executed_at=NOW)


def test_governed_research_executor_routes_retained_aggregate_families() -> None:
    outcome_aggregate = RetainedOutcomeAggregateExperimentExecutor(
        counterfactual_outcome=_FakeOutcomeAggregateSource(
            _FakeOutcomeAggregate(cohort_id="cohort-1", mean_subsequent_return=Decimal("-0.01"))
        ),
        securities_structure_outcome=_FakeOutcomeAggregateSource(),
    )
    dispatcher = GovernedResearchExecutor(
        ledger=_raising_executor(),  # type: ignore[arg-type]
        operational=_raising_executor(),  # type: ignore[arg-type]
        outcome_aggregate=outcome_aggregate,
    )
    counterfactual_proposal = proposal(
        "dispatch-proposal",
        proposed_family="CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1_COUNTERFACTUAL_OUTCOME_POLICY_REVIEW",
        required_data=("counterfactual-outcome-cohort-1",),
    )

    outcome = dispatcher.execute(proposal=counterfactual_proposal, run_id="run-1", executed_at=NOW)

    assert outcome.expected_net_economics == Decimal("-0.01")


def test_governed_research_executor_raises_when_outcome_aggregate_executor_not_configured() -> None:
    dispatcher = GovernedResearchExecutor(
        ledger=_raising_executor(),  # type: ignore[arg-type]
        operational=_raising_executor(),  # type: ignore[arg-type]
    )
    unconfigured_proposal = proposal(
        "unconfigured-proposal",
        proposed_family="CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1_COUNTERFACTUAL_OUTCOME_POLICY_REVIEW",
        required_data=("counterfactual-outcome-cohort-1",),
    )

    with pytest.raises(
        ResearchOperationsError, match="RETAINED_OUTCOME_AGGREGATE_EXECUTOR_NOT_CONFIGURED"
    ):
        dispatcher.execute(proposal=unconfigured_proposal, run_id="run-1", executed_at=NOW)


def test_counterfactual_outcome_proposal_reaches_recommendation_end_to_end() -> None:
    """End-to-end proof, offline: the exact chain that has never once
    completed organically in production for a
    ``COUNTERFACTUAL_OUTCOME_POLICY_REVIEW`` proposal --
    feasibility(READY_FOR_REVIEW) -> review -> approval -> enqueue ->
    execute -> recommendation -- now closes, with zero family-hash match
    and zero new investment judgment (the executor only restates the
    already-retained aggregate)."""

    repository = InMemoryResearchOperationsRepository()
    counterfactual_fact = ResearchAgendaSourceFact.create(
        fact_id="counterfactual-outcome-cohort-end-to-end",
        fact_type="COUNTERFACTUAL_OUTCOME_PATTERN",
        venue=ResearchVenue.CRYPTO,
        market_scope=("CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1",),
        state="QUALIFIED",
        summary="A qualified counterfactual-outcome cohort confirms the rejection policy.",
        evidence_ids=("counterfactual-outcome-agg-end-to-end",),
        evidence_hashes=("e" * 64,),
        observed_at=NOW,
    )
    agenda = RefreshResearchAgenda(repository).execute(facts=(counterfactual_fact,)).items
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-counterfactual-end-to-end",
        generated_at=NOW,
        actor="system:fixture",
        context=ResearchProposalGenerationContext(
            retained_data_ids=(agenda[0].source_fact_id,),
            executable_experiment_ids=(),
            provider_capability_ids=(),
            remaining_experiment_budget=1,
        ),
        maximum_proposals=1,
    )
    value = generated.proposals[0]
    assert generated.feasibilities[0].status is ResearchFeasibilityState.READY_FOR_REVIEW

    transition(repository, value, ResearchProposalState.READY_FOR_REVIEW)
    transition(repository, value, ResearchProposalState.APPROVED_FOR_EXPERIMENT)
    enqueued = EnqueueResearchExperiment(repository).execute(
        queue_id="queue-counterfactual-1",
        proposal_id=value.proposal_id,
        run_id="queued-run-counterfactual-1",
        actor="operator:zach",
        idempotency_key="enqueue-key-counterfactual-1",
        enqueued_at=NOW,
    )
    assert enqueued.event.to_status is ResearchExperimentQueueStatus.QUEUED

    executor = GovernedResearchExecutor(
        ledger=_raising_executor(),  # type: ignore[arg-type]
        operational=_raising_executor(),  # type: ignore[arg-type]
        outcome_aggregate=RetainedOutcomeAggregateExperimentExecutor(
            counterfactual_outcome=_FakeOutcomeAggregateSource(
                _FakeOutcomeAggregate(
                    cohort_id="cohort-end-to-end", mean_subsequent_return=Decimal("-0.02")
                )
            ),
            securities_structure_outcome=_FakeOutcomeAggregateSource(),
        ),
    )
    result = ExecuteApprovedResearchQueueItem(repository, executor).execute(
        queue_id="queue-counterfactual-1",
        actor="operator:zach",
        idempotency_key="execute-key-counterfactual-1",
        executed_at=NOW,
    )

    assert result.event.to_status is ResearchExperimentQueueStatus.COMPLETED
    assert result.governed_result.recommendation.state is ResearchRecommendationState.REJECT
    assert result.governed_result.recommendation.paper_authorization_created is False
    assert result.governed_result.recommendation.authority == "NONE"
    assert result.governed_result.recommendation.live_authority == "NONE"


def test_queued_experiment_can_be_canceled_only_before_execution() -> None:
    repository = InMemoryResearchOperationsRepository()
    agenda = (
        RefreshResearchAgenda(repository)
        .execute(facts=(fact("negative_expected_economics-cancel"),))
        .items
    )
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-cancel-1",
        generated_at=NOW,
        actor="system:fixture",
        context=ResearchProposalGenerationContext(
            retained_data_ids=(agenda[0].source_fact_id,),
            executable_experiment_ids=(agenda[0].market_scope[0],),
            provider_capability_ids=(),
            remaining_experiment_budget=1,
        ),
        maximum_proposals=1,
    )
    value = generated.proposals[0]
    transition(repository, value, ResearchProposalState.READY_FOR_REVIEW)
    transition(repository, value, ResearchProposalState.APPROVED_FOR_EXPERIMENT)
    EnqueueResearchExperiment(repository).execute(
        queue_id="queue-cancel",
        proposal_id=value.proposal_id,
        run_id="run-cancel",
        actor="operator:zach",
        idempotency_key="enqueue-cancel",
        enqueued_at=NOW,
    )
    event, replay = CancelQueuedResearchExperiment(repository).execute(
        queue_id="queue-cancel",
        actor="operator:zach",
        reason="FIXTURE_CANCEL",
        idempotency_key="cancel-key",
        canceled_at=NOW,
    )

    assert event.to_status is ResearchExperimentQueueStatus.CANCELED
    assert replay is False
    with pytest.raises(ResearchOperationsError, match="EXECUTION_STATE_INVALID"):
        ExecuteApprovedResearchQueueItem(repository, Executor()).execute(
            queue_id="queue-cancel",
            actor="operator:zach",
            idempotency_key="execute-canceled",
            executed_at=NOW,
        )


def test_queue_capacity_and_one_running_experiment_limit() -> None:
    repository = InMemoryResearchOperationsRepository()
    facts = tuple(
        ResearchAgendaSourceFact.create(
            fact_id=f"negative_expected_economics-capacity-{ordinal}",
            fact_type="NEGATIVE_EXPECTED_ECONOMICS",
            venue=ResearchVenue.CRYPTO,
            market_scope=(f"EXPERIMENT_{ordinal}",),
            state="BLOCKED",
            summary=f"Fixture method {ordinal} requires bounded material review.",
            evidence_ids=(f"evidence-{ordinal}",),
            evidence_hashes=(str(ordinal) * 64,),
            observed_at=NOW + timedelta(seconds=ordinal),
        )
        for ordinal in range(1, 5)
    )
    agenda = RefreshResearchAgenda(repository).execute(facts=facts).items
    context = ResearchProposalGenerationContext(
        retained_data_ids=tuple(value.source_fact_id for value in agenda),
        executable_experiment_ids=tuple(value.market_scope[0] for value in agenda),
        provider_capability_ids=(),
        remaining_experiment_budget=4,
    )
    first = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-capacity-1",
        generated_at=NOW,
        actor="system:fixture",
        context=context,
    )
    for ordinal, value in enumerate(first.proposals, start=1):
        transition(repository, value, ResearchProposalState.READY_FOR_REVIEW)
        transition(repository, value, ResearchProposalState.APPROVED_FOR_EXPERIMENT)
        EnqueueResearchExperiment(repository).execute(
            queue_id=f"queue-capacity-{ordinal}",
            proposal_id=value.proposal_id,
            run_id=f"run-capacity-{ordinal}",
            actor="operator:zach",
            idempotency_key=f"enqueue-capacity-{ordinal}",
            enqueued_at=NOW,
        )
    second = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-capacity-2",
        generated_at=NOW + timedelta(minutes=1),
        actor="system:fixture",
        context=context,
    )
    fourth = second.proposals[0]
    transition(repository, fourth, ResearchProposalState.READY_FOR_REVIEW)
    transition(repository, fourth, ResearchProposalState.APPROVED_FOR_EXPERIMENT)
    with pytest.raises(ResearchOperationsError, match="QUEUE_CAPACITY_EXCEEDED"):
        EnqueueResearchExperiment(repository).execute(
            queue_id="queue-capacity-4",
            proposal_id=fourth.proposal_id,
            run_id="run-capacity-4",
            actor="operator:zach",
            idempotency_key="enqueue-capacity-4",
            enqueued_at=NOW,
        )

    running = ResearchExperimentQueueEvent.create(
        event_id="queue-capacity-1-event-2",
        queue_id="queue-capacity-1",
        ordinal=2,
        from_status=ResearchExperimentQueueStatus.QUEUED,
        to_status=ResearchExperimentQueueStatus.RUNNING,
        attempt_count=1,
        budget_event_id=None,
        result_id=None,
        recommendation_id=None,
        reason="FIXTURE_CONCURRENT_RUNNING",
        actor="operator:zach",
        idempotency_key="fixture-running",
        occurred_at=NOW,
    )
    repository.save_queue_event(running)
    with pytest.raises(ResearchOperationsError, match="QUEUE_CONCURRENCY_LIMIT"):
        ExecuteApprovedResearchQueueItem(repository, Executor()).execute(
            queue_id="queue-capacity-2",
            actor="operator:zach",
            idempotency_key="execute-capacity-2",
            executed_at=NOW,
        )


# ---------------------------------------------------------------------------
# Research Agenda Fact Summary Integrity v1
# ---------------------------------------------------------------------------


def _real_v3_qualified_explanation() -> str:
    from tests.test_counterfactual_outcome_specificity import (
        PRODUCTS,
        World,
        _days,
        _fill,
    )

    world = World()
    _fill(
        world,
        days=_days(31),
        cohort_products=PRODUCTS[:2],
        cohort_return=Decimal("0.03"),
        complement_return=Decimal("0.01"),
    )
    (aggregate,) = [
        value for value in world.build() if value.qualification_state.value == "QUALIFIED"
    ]
    return aggregate.explanation


def _worst_case_v3_explanations() -> tuple[str, ...]:
    """The real explanation builder driven with the largest values every
    persisted column and Decimal/int/timestamp field can carry."""

    import dataclasses

    from apex.domain.counterfactual_classification import ReasonFamily
    from apex.domain.counterfactual_followup import FollowUpBaselineKind, FollowUpHorizonKind
    from apex.domain.counterfactual_outcome import (
        CounterfactualOutcomeQualificationState,
        CounterfactualOutcomeSpecificity,
        _explanation,
    )
    from apex.domain.shared_opportunity import SharedVenue
    from tests.test_counterfactual_outcome_specificity import (
        PRODUCTS,
        World,
        _days,
        _fill,
    )

    world = World()
    _fill(
        world,
        days=_days(31),
        cohort_products=PRODUCTS[:2],
        cohort_return=Decimal("0.03"),
        complement_return=Decimal("0.01"),
    )
    real = next(value.specificity for value in world.build() if value.specificity is not None)
    assert real is not None
    overrides: dict[str, object] = {}
    for field in dataclasses.fields(CounterfactualOutcomeSpecificity):
        current = getattr(real, field.name)
        if isinstance(current, bool):
            overrides[field.name] = True
        elif isinstance(current, int):
            overrides[field.name] = 10**15
        elif isinstance(current, datetime):
            overrides[field.name] = datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    for name in (
        "mean_relative_window_return",
        "median_relative_window_return",
        "relative_positive_window_rate",
        "relative_window_return_standard_deviation",
        "descriptive_mean_raw_return",
        "descriptive_mean_cohort_window_return",
        "descriptive_mean_complement_return",
        "descriptive_mean_window_spread",
    ):
        overrides[name] = Decimal("-99999999999999999999.99999999")
    overrides["window_horizon_days"] = 10**15
    specificity = dataclasses.replace(real, **overrides)  # type: ignore[arg-type]
    texts = []
    for state in (
        CounterfactualOutcomeQualificationState.QUALIFIED,
        CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE,
    ):
        texts.append(
            _explanation(
                venue=max(SharedVenue, key=lambda value: len(value.value)),
                strategy_or_method_id="M" * 300,  # the persisted column maximum
                reason_family=max(ReasonFamily, key=lambda value: len(value.value)),
                horizon_kind=max(FollowUpHorizonKind, key=lambda value: len(value.value)),
                baseline_kind=max(FollowUpBaselineKind, key=lambda value: len(value.value)),
                followup_policy_version="F" * 40,  # the persisted column maximum
                total_observations_considered=10**15,
                mature_observation_count=10**15,
                eligible_observation_count=10**15,
                qualification_state=state,
                mean_subsequent_return=None,
                positive_rate=None,
                specificity=specificity,
            )
        )
    return tuple(texts)


def _agenda_fact(summary: str, *, fact_id: str = "counterfactual-outcome-cohort-v3"):
    return ResearchAgendaSourceFact.create(
        fact_id=fact_id,
        fact_type="COUNTERFACTUAL_OUTCOME_PATTERN",
        venue=ResearchVenue.CRYPTO,
        market_scope=("CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1",),
        state="QUALIFIED",
        summary=summary,
        evidence_ids=("counterfactual-outcome-agg-v3",),
        evidence_hashes=("c" * 64,),
        observed_at=NOW,
    )


def test_summary_bound_is_derived_from_the_proposal_hypothesis_ceiling() -> None:
    from apex.domain.research_operations import (
        MAXIMUM_HYPOTHESIS_PREFIX_CHARS,
        RESEARCH_AGENDA_FACT_SUMMARY_MAX_CHARS,
        RESEARCH_PROPOSAL_HYPOTHESIS_MAX_CHARS,
        generated_proposal_hypothesis,
    )

    assert (
        RESEARCH_AGENDA_FACT_SUMMARY_MAX_CHARS + MAXIMUM_HYPOTHESIS_PREFIX_CHARS
        <= RESEARCH_PROPOSAL_HYPOTHESIS_MAX_CHARS
    )
    assert RESEARCH_AGENDA_FACT_SUMMARY_MAX_CHARS == 1955
    # Through the real construction path: a maximum-length summary yields a
    # hypothesis the real proposal validator accepts for every class, and the
    # longest prefix lands exactly on the ceiling (the bound is tight).
    lengths = []
    for proposal_class in ResearchProposalClass:
        fact = _agenda_fact("s" * RESEARCH_AGENDA_FACT_SUMMARY_MAX_CHARS)
        text = generated_proposal_hypothesis(proposal_class, fact.summary)
        lengths.append(len(text))
        assert len(text) <= RESEARCH_PROPOSAL_HYPOTHESIS_MAX_CHARS
    assert max(lengths) == RESEARCH_PROPOSAL_HYPOTHESIS_MAX_CHARS


def test_real_v3_qualified_explanation_over_1000_chars_is_accepted_and_preserved() -> None:
    explanation = _real_v3_qualified_explanation()
    assert len(explanation) > 1000
    fact = _agenda_fact(explanation)
    assert fact.summary == explanation


def test_worst_case_v3_explanation_fits_the_summary_bound() -> None:
    from apex.domain.research_operations import RESEARCH_AGENDA_FACT_SUMMARY_MAX_CHARS

    explanations = _worst_case_v3_explanations()
    assert max(len(value) for value in explanations) > len(_real_v3_qualified_explanation())
    for explanation in explanations:
        assert len(explanation) <= RESEARCH_AGENDA_FACT_SUMMARY_MAX_CHARS
        assert _agenda_fact(explanation).summary == explanation


def test_summary_over_the_maximum_still_fails_closed() -> None:
    from apex.domain.research_operations import RESEARCH_AGENDA_FACT_SUMMARY_MAX_CHARS

    _agenda_fact("s" * RESEARCH_AGENDA_FACT_SUMMARY_MAX_CHARS)
    with pytest.raises(ResearchOperationsError, match="RESEARCH_AGENDA_FACT_SUMMARY_INVALID"):
        _agenda_fact("s" * (RESEARCH_AGENDA_FACT_SUMMARY_MAX_CHARS + 1))
    with pytest.raises(ResearchOperationsError, match="RESEARCH_AGENDA_FACT_SUMMARY_INVALID"):
        _agenda_fact("   ")


def test_facts_within_the_old_1000_char_bound_hash_identically() -> None:
    """Golden hashes computed by the pre-change code (998b8be): widening the
    guard changes no digest for any fact that was already valid."""

    from apex.domain.research_operations import build_research_agenda_item

    fact = ResearchAgendaSourceFact.create(
        fact_id="golden-fact-1",
        fact_type="COUNTERFACTUAL_OUTCOME_PATTERN",
        venue=ResearchVenue.CRYPTO,
        market_scope=("B", "A"),
        state="QUALIFIED",
        summary=("s" * 999) + "t",
        evidence_ids=("e2", "e1"),
        evidence_hashes=("b" * 64, "a" * 64),
        observed_at=NOW,
    )
    assert len(fact.summary) == 1000
    assert fact.canonical_hash == (
        "4eaf6bb638680eb19aabee0dfee7366c3615db52b2754271a92906955f40bb36"
    )
    assert build_research_agenda_item(fact).canonical_hash == (
        "8f0927f32d28eb67b1f21ee136ef25b1a8d25e64fae8da380ab808dc9f884955"
    )


def test_long_v3_summary_flows_to_a_deterministic_ready_for_review_proposal() -> None:
    explanation = _real_v3_qualified_explanation()
    outcomes = []
    for _ in range(2):
        repository = InMemoryResearchOperationsRepository()
        fact = _agenda_fact(explanation)
        agenda = RefreshResearchAgenda(repository).execute(facts=(fact,)).items
        assert agenda[0].problem_statement == explanation
        generated = GenerateResearchProposals(repository).execute(
            generator_run_id="generator-long-v3",
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
        (proposal,) = generated.proposals
        assert explanation in proposal.hypothesis
        assert len(proposal.hypothesis) <= 2000
        assert generated.feasibilities[0].status is ResearchFeasibilityState.READY_FOR_REVIEW
        assert proposal_state(repository, proposal.proposal_id) is ResearchProposalState.DRAFT
        outcomes.append((fact.canonical_hash, agenda[0].canonical_hash, proposal.canonical_hash))
    assert outcomes[0] == outcomes[1]
