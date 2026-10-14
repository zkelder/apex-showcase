"""PostgreSQL persistence proofs for Counterfactual Outcome Feedback v1's
Phase 6: the Research Operations fact consumer. Proves a QUALIFIED
aggregate produces exactly one, stable, idempotent agenda item; an
INSUFFICIENT_SAMPLE or VENUE_ECONOMIC_REDUCTION_UNAVAILABLE aggregate
produces none (no agenda spam from underpowered cells); and a cohort that
stays QUALIFIED across multiple daily runs never re-notifies."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
from apex.domain.counterfactual_classification import CURRENT_CLASSIFIER_VERSION, ReasonFamily
from apex.domain.counterfactual_followup import (
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_POLICY_V2,
    FOLLOWUP_POLICY_V3,
    FollowUpBaselineKind,
    FollowUpHorizonKind,
)
from apex.domain.counterfactual_outcome import (
    CANONICAL_REASON_FAMILY_ATTRIBUTION_V1,
    COUNTERFACTUAL_OUTCOME_POLICY_V1,
    COUNTERFACTUAL_OUTCOME_POLICY_V3,
    COUNTERFACTUAL_OUTCOME_POLICY_V3_HASH,
    INDEPENDENT_WINDOW_POLICY_V1,
    PEER_REFERENCE_POLICY_V1,
    CounterfactualOutcomeAggregate,
    CounterfactualOutcomeQualificationState,
    CounterfactualOutcomeSpecificity,
)
from apex.domain.research_operations import (
    ResearchAgendaSourceFact,
    ResearchExperimentQueueStatus,
    ResearchOperationsError,
    ResearchProposal,
    ResearchProposalOrigin,
    ResearchProposalState,
    ResearchRecommendationState,
    ResearchVenue,
)
from apex.domain.shared_opportunity import SharedVenue

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 19, tzinfo=UTC)


def _only_counterfactual_outcome(facts: tuple) -> tuple:
    # Scoped deliberately: other fact sources on this same source object
    # (e.g. strategy-library-derived facts) can legitimately anchor their
    # own ``observed_at`` to ``as_of`` when no retained evaluation exists
    # yet -- an unrelated, pre-existing property of this shared source, not
    # something these tests exercise or should be broken by.
    return tuple(f for f in facts if f.fact_type == "COUNTERFACTUAL_OUTCOME_PATTERN")


def _aggregate(
    *,
    aggregate_id: str,
    cohort_id: str,
    run_id: str,
    qualification_state: CounterfactualOutcomeQualificationState,
    venue: SharedVenue = SharedVenue.CRYPTO,
    generated_at: datetime = NOW,
    eligible_observation_count: int = 30,
    # Counterfactual V2 Research Evidence Quarantine v1: defaults to CRYPTO's
    # current, non-quarantined policy version so every test in this module
    # keeps proving exactly what it was written to prove. The KALSHI case
    # below passes ``FOLLOWUP_POLICY_V2`` explicitly -- KALSHI's V2 is
    # current and sound, and stating it at the call site is what makes the
    # "KALSHI is unaffected" property visible rather than incidental.
    followup_policy_version: str = FOLLOWUP_POLICY_V3,
    # Counterfactual Outcome Evidence Specificity v3: CRYPTO's current,
    # research-sourceable shape is now an ORIGIN_EVIDENCE cohort under
    # ``COUNTERFACTUAL_OUTCOME_POLICY_V3`` carrying a specificity block --
    # CRYPTO V1 is quarantined from new research exactly like CRYPTO's V2
    # follow-up evidence.  Defaults follow the current shape; the tests that
    # prove the quarantines pass the older shape explicitly.
    outcome_policy_version: str = COUNTERFACTUAL_OUTCOME_POLICY_V3,
    baseline_kind: FollowUpBaselineKind = FollowUpBaselineKind.ORIGIN_EVIDENCE,
) -> CounterfactualOutcomeAggregate:
    is_v3 = outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V3
    qualified = qualification_state is CounterfactualOutcomeQualificationState.QUALIFIED
    return CounterfactualOutcomeAggregate(
        aggregate_id=aggregate_id,
        cohort_id=cohort_id,
        outcome_policy_version=outcome_policy_version,
        run_id=run_id,
        venue=venue,
        strategy_or_method_id="CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1",
        reason_family=ReasonFamily.ECONOMICS,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=baseline_kind,
        followup_policy_version=followup_policy_version,
        classifier_version=CURRENT_CLASSIFIER_VERSION,
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        generated_at=generated_at,
        total_observations_considered=eligible_observation_count,
        mature_observation_count=eligible_observation_count,
        eligible_observation_count=eligible_observation_count,
        evidence_ineligible_observation_count=0,
        unassessed_observation_count=0,
        unavailable_observation_count=0,
        positive_outcome_count=eligible_observation_count,
        negative_outcome_count=0,
        zero_outcome_count=0,
        mean_subsequent_return=Decimal("0.02") if qualified and not is_v3 else None,
        median_subsequent_return=Decimal("0.02") if qualified and not is_v3 else None,
        positive_rate=Decimal("1") if qualified and not is_v3 else None,
        qualification_state=qualification_state,
        contributing_decision_ids=tuple(f"decision-{i}" for i in range(eligible_observation_count)),
        explanation=f"test aggregate {qualification_state.value}",
        canonical_hash=f"{aggregate_id}-hash".ljust(64, "0")[:64],
        specificity=(
            _specificity(windows=eligible_observation_count, qualified=qualified) if is_v3 else None
        ),
        authority="NONE",
    )


def _specificity(*, windows: int, qualified: bool) -> CounterfactualOutcomeSpecificity:
    return CounterfactualOutcomeSpecificity(
        attribution_policy_version=CANONICAL_REASON_FAMILY_ATTRIBUTION_V1,
        peer_reference_policy_version=PEER_REFERENCE_POLICY_V1,
        independent_window_policy_version=INDEPENDENT_WINDOW_POLICY_V1,
        policy_hash=COUNTERFACTUAL_OUTCOME_POLICY_V3_HASH,
        multi_family_decision_count=0,
        method_unattributed_decision_count=0,
        raw_return_observation_count=windows,
        anchor_mismatch_observation_count=0,
        raw_window_count=windows,
        inconsistent_window_count=0,
        mixed_origin_window_count=0,
        membership_conflict_product_count=0,
        degenerate_window_count=0,
        products_with_decision_but_no_return_count=0,
        awaiting_peer_completion_window_count=0,
        off_grid_window_count=0,
        window_horizon_days=1,
        horizon_days_heterogeneous=False,
        independent_window_count=windows,
        independent_window_keys=tuple(
            f"2026-08-{4 + i:02d}T00:00:00Z/2026-08-{5 + i:02d}T00:00:00Z:cohort=1:complement=4"
            for i in range(min(windows, 20))
        ),
        first_window_start=NOW,
        last_window_end=NOW + timedelta(days=windows),
        descriptive_mean_raw_return=Decimal("0.05"),
        descriptive_mean_cohort_window_return=Decimal("0.05"),
        descriptive_mean_complement_return=Decimal("0.03"),
        descriptive_mean_window_spread=Decimal("0.02"),
        descriptive_informative_window_count=windows,
        relative_positive_window_count=windows,
        relative_negative_window_count=0,
        relative_zero_window_count=0,
        mean_relative_window_return=Decimal("0.02") if qualified else None,
        median_relative_window_return=Decimal("0.02") if qualified else None,
        relative_positive_window_rate=Decimal("1") if qualified else None,
        relative_window_return_standard_deviation=Decimal("0") if qualified else None,
    )


def test_qualified_aggregate_produces_exactly_one_stable_agenda_item(
    postgres_engine: Engine,
) -> None:
    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="agg-qualified-day1",
            cohort_id="cohort-qualified",
            run_id="run-day1",
            qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
        )
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    facts = source.facts(as_of=NOW)
    matching = [item for item in facts if item.fact_type == "COUNTERFACTUAL_OUTCOME_PATTERN"]
    assert len(matching) == 1
    fact = matching[0]
    assert fact.venue is ResearchVenue.CRYPTO
    assert fact.market_scope == ("CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1",)
    assert fact.state == "QUALIFIED"

    repository = PostgresResearchOperationsRepository(postgres_engine)
    refresh = RefreshResearchAgenda(repository).execute(facts=facts)
    matching_items = [
        item for item in refresh.items if item.source_fact_id.startswith("counterfactual-outcome-")
    ]
    assert len(matching_items) == 1
    assert matching_items[0].suggested_next_step == (
        "REVIEW_COUNTERFACTUAL_OUTCOME_EVIDENCE_FOR_POLICY_IMPLICATIONS"
    )


def test_insufficient_sample_and_venue_unavailable_never_generate_a_fact(
    postgres_engine: Engine,
) -> None:
    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="agg-insufficient",
            cohort_id="cohort-insufficient",
            run_id="run-day1",
            qualification_state=CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE,
            eligible_observation_count=5,
        )
    )
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="agg-kalshi-unavailable",
            cohort_id="cohort-kalshi",
            run_id="run-day1",
            qualification_state=CounterfactualOutcomeQualificationState.VENUE_ECONOMIC_REDUCTION_UNAVAILABLE,
            venue=SharedVenue.KALSHI,
            eligible_observation_count=50,
            followup_policy_version=FOLLOWUP_POLICY_V2,
        )
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    facts = source.facts(as_of=NOW)
    matching = [item for item in facts if item.fact_type == "COUNTERFACTUAL_OUTCOME_PATTERN"]
    assert matching == []


def test_replay_is_idempotent_no_duplicate_agenda_item(postgres_engine: Engine) -> None:
    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="agg-replay-day1",
            cohort_id="cohort-replay",
            run_id="run-day1",
            qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
        )
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    repository = PostgresResearchOperationsRepository(postgres_engine)

    # A genuine replay reuses the identical ``as_of`` -- this is not the
    # place to also prove tolerance to a *different* as_of, which would
    # legitimately move any other, unrelated as-of-anchored fact this
    # source can produce (e.g. a retained-evaluation-less strategy-library
    # fact falls back to ``as_of`` for its own ``observed_at``) and is
    # exactly why that scenario is intentionally not exercised here.
    first = RefreshResearchAgenda(repository).execute(facts=source.facts(as_of=NOW))
    second = RefreshResearchAgenda(repository).execute(facts=source.facts(as_of=NOW))
    matching_first = [
        i for i in first.items if i.source_fact_id.startswith("counterfactual-outcome-")
    ]
    matching_second_replayed = [
        i for i in second.replayed_item_ids if i.startswith("agenda-counterfactual-outcome-")
    ]
    assert len(matching_first) == 1
    assert len(matching_second_replayed) == 1
    assert matching_second_replayed[0] == f"agenda-{matching_first[0].source_fact_id}"


def test_cohort_qualified_across_multiple_days_still_yields_one_stable_agenda_item(
    postgres_engine: Engine,
) -> None:
    """The whole point of anchoring the fact to the *earliest* qualified
    aggregate: a cohort that stays QUALIFIED (or accumulates even more
    evidence) on day 2 must not mint a second agenda item or raise an
    identity conflict."""

    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    day1 = _aggregate(
        aggregate_id="agg-multi-day1",
        cohort_id="cohort-multi",
        run_id="run-day1",
        qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
        generated_at=NOW,
        eligible_observation_count=30,
    )
    day2 = replace(
        day1,
        aggregate_id="agg-multi-day2",
        run_id="run-day2",
        generated_at=NOW + timedelta(days=1),
        eligible_observation_count=45,
        total_observations_considered=45,
        mature_observation_count=45,
        positive_outcome_count=45,
        contributing_decision_ids=tuple(f"decision-{i}" for i in range(45)),
        canonical_hash="f" * 64,
    )
    outcome_repository.save_aggregate(aggregate=day1)
    outcome_repository.save_aggregate(aggregate=day2)

    source = RetainedResearchAgendaSource(postgres_engine)
    repository = PostgresResearchOperationsRepository(postgres_engine)

    facts_day1 = _only_counterfactual_outcome(source.facts(as_of=NOW))
    assert len(facts_day1) == 1
    RefreshResearchAgenda(repository).execute(facts=facts_day1)

    # Day 2: two retained aggregates for the same cohort now exist, but the
    # fact source still anchors to the single earliest one -- same fact_id,
    # same hash, pure idempotent replay, not a second item or a conflict.
    facts_day2 = _only_counterfactual_outcome(source.facts(as_of=NOW + timedelta(days=1)))
    assert len(facts_day2) == 1
    assert facts_day2[0].fact_id == facts_day1[0].fact_id
    assert facts_day2[0].canonical_hash == facts_day1[0].canonical_hash
    refresh_day2 = RefreshResearchAgenda(repository).execute(facts=facts_day2)
    assert len(refresh_day2.replayed_item_ids) == 1  # pure replay, no new/conflicting item
    assert refresh_day2.replayed_item_ids == (f"agenda-{facts_day1[0].fact_id}",)


def test_qualified_aggregate_reaches_a_real_recommendation_end_to_end(
    postgres_engine: Engine,
) -> None:
    """Research Experiment Executability v1, against real Postgres: extends
    the proposal-classification proof above one stage further -- through
    feasibility, review, approval, the experiment queue, and a genuine
    ``RetainedOutcomeAggregateExperimentExecutor`` execution that reads this
    exact aggregate back out of the same ``PostgresCounterfactualOutcome
    Repository`` used to save it -- to a real recommendation. Before this
    milestone, every organically-generated ``COUNTERFACTUAL_OUTCOME_POLICY_
    REVIEW`` proposal landed ``REQUIRES_IMPLEMENTATION`` forever (confirmed
    against production: 87 feasibility rows, 0 ever ``READY_FOR_REVIEW``
    from the automatic generator), so this exact chain never once
    completed."""

    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="agg-end-to-end-day1",
            cohort_id="cohort-end-to-end",
            run_id="run-end-to-end-day1",
            qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
        )
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    repository = PostgresResearchOperationsRepository(postgres_engine)
    matching_facts = tuple(
        value
        for value in source.facts(as_of=NOW)
        if value.fact_type == "COUNTERFACTUAL_OUTCOME_PATTERN"
    )
    agenda = RefreshResearchAgenda(repository).execute(facts=matching_facts).items

    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-counterfactual-end-to-end",
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
        idempotency_key="approval-key-end-to-end",
    )
    enqueued = EnqueueResearchExperiment(repository).execute(
        queue_id="queue-counterfactual-end-to-end",
        proposal_id=value.proposal_id,
        run_id="run-counterfactual-end-to-end",
        actor="operator:zach",
        idempotency_key="enqueue-key-end-to-end",
        enqueued_at=NOW,
    )
    assert enqueued.event.to_status is ResearchExperimentQueueStatus.QUEUED

    executor = GovernedResearchExecutor(
        ledger=_UnreachableExecutor(),
        operational=_UnreachableExecutor(),
        outcome_aggregate=RetainedOutcomeAggregateExperimentExecutor(
            counterfactual_outcome=outcome_repository,
            securities_structure_outcome=PostgresSecuritiesStructureOutcomeRepository(
                postgres_engine
            ),
        ),
    )
    result = ExecuteApprovedResearchQueueItem(repository, executor).execute(
        queue_id="queue-counterfactual-end-to-end",
        actor="operator:zach",
        idempotency_key="execute-key-end-to-end",
        executed_at=NOW,
    )

    assert result.event.to_status is ResearchExperimentQueueStatus.COMPLETED
    # The fixture's aggregate has ``mean_subsequent_return=Decimal("0.02")``
    # (positive) -- honestly REVISE, never PAPER_CANDIDATE, matching the
    # executor's own documented "never overclaims" contract.
    assert result.governed_result.recommendation.state is ResearchRecommendationState.REVISE
    # Counterfactual Outcome Evidence Specificity v3: the economics are the
    # gate-complement spread, named as such in the outcome's own warnings.
    assert result.governed_result.outcome.expected_net_economics == Decimal("0.02")
    assert "PEER_RELATIVE_ECONOMICS_NOT_ABSOLUTE_RETURN" in result.governed_result.outcome.warnings
    assert result.governed_result.outcome.existing_trial_ids == tuple(
        f"decision-{i}" for i in range(30)
    )
    assert result.governed_result.recommendation.paper_authorization_created is False
    assert result.governed_result.recommendation.authority == "NONE"
    assert result.governed_result.recommendation.live_authority == "NONE"


def test_quarantined_crypto_v2_cohort_creates_no_new_agenda_fact(
    postgres_engine: Engine,
) -> None:
    """Counterfactual V2 Research Evidence Quarantine v1, the core property:
    a CRYPTO ``FOLLOWUP_POLICY_V2`` aggregate that is genuinely ``QUALIFIED``
    -- byte-for-byte the shape that seeded all 26 production agenda items --
    originates no new research fact, while its V3 sibling in the identical
    structural cohort still does."""

    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="agg-quarantined-v2",
            cohort_id="cohort-quarantined-v2",
            run_id="run-quarantine",
            qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
            followup_policy_version=FOLLOWUP_POLICY_V2,
        )
    )
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="agg-current-v3",
            cohort_id="cohort-current-v3",
            run_id="run-quarantine",
            qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
            followup_policy_version=FOLLOWUP_POLICY_V3,
        )
    )

    source = RetainedResearchAgendaSource(postgres_engine)
    matching = _only_counterfactual_outcome(source.facts(as_of=NOW))
    assert [value.fact_id for value in matching] == ["counterfactual-outcome-cohort-current-v3"]

    # The quarantined aggregate itself is untouched: still retained, still
    # QUALIFIED, still readable, still returned by the very query the
    # agenda source calls. Only its eligibility to source new work is gone.
    retained = {
        value.cohort_id: value for value in outcome_repository.earliest_qualified_per_cohort()
    }
    assert set(retained) == {"cohort-quarantined-v2", "cohort-current-v3"}
    assert (
        retained["cohort-quarantined-v2"].qualification_state
        is CounterfactualOutcomeQualificationState.QUALIFIED
    )

    disclosed = source.quarantined_counterfactual_outcome_cohorts()
    assert [value.cohort_id for value in disclosed] == ["cohort-quarantined-v2"]
    assert disclosed[0].defect_code == "FORWARD_RETURN_TEMPORAL_INTEGRITY_DEFECT"
    assert disclosed[0].corrected_by_policy_version == FOLLOWUP_POLICY_V3


def test_kalshi_v2_cohort_still_sources_research_normally(postgres_engine: Engine) -> None:
    """KALSHI shares only the ``FOLLOWUP_POLICY_V2`` label; its follow-up
    behavior never changed and it is explicitly unaffected."""

    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="agg-kalshi-qualified",
            cohort_id="cohort-kalshi-qualified",
            run_id="run-kalshi",
            qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
            venue=SharedVenue.KALSHI,
            followup_policy_version=FOLLOWUP_POLICY_V2,
            outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1,
            baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        )
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    matching = _only_counterfactual_outcome(source.facts(as_of=NOW))
    assert [value.venue for value in matching] == [ResearchVenue.KALSHI]
    assert source.quarantined_counterfactual_outcome_cohorts() == ()


def test_an_already_retained_v2_proposal_cannot_execute_after_quarantine(
    postgres_engine: Engine,
) -> None:
    """The second enforcement point, and why one is not enough: this
    milestone deliberately leaves the 13 already-retained V2-descended
    proposals in place, and each still names its cohort in
    ``required_data_ids``. If one later became feasible it would route
    straight back into defective evidence through a path the agenda filter
    never sees. The executor refuses, with a reason code distinct from
    ``..._NO_LONGER_QUALIFIED`` -- the cohort *is* still qualified; its
    evidence policy version is what is disqualified."""

    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="agg-legacy-v2",
            cohort_id="cohort-legacy-v2",
            run_id="run-legacy",
            qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
            followup_policy_version=FOLLOWUP_POLICY_V2,
        )
    )
    executor = RetainedOutcomeAggregateExperimentExecutor(
        counterfactual_outcome=outcome_repository,
        securities_structure_outcome=PostgresSecuritiesStructureOutcomeRepository(postgres_engine),
    )
    proposal = _legacy_counterfactual_proposal(cohort_id="cohort-legacy-v2")
    with pytest.raises(ResearchOperationsError) as error:
        executor.execute(proposal=proposal, run_id="run-legacy-exec", executed_at=NOW)
    assert str(error.value) == "RETAINED_OUTCOME_AGGREGATE_EVIDENCE_POLICY_VERSION_QUARANTINED"


def test_an_agenda_item_retained_before_quarantine_generates_no_new_proposal(
    postgres_engine: Engine,
) -> None:
    """The property that actually closes the live production gap, and the
    one withholding the fact alone does **not** reach.

    ``GenerateResearchProposals`` draws its candidates from the *retained
    agenda item table*, never from this cycle's facts. Production holds 26
    agenda items created from CRYPTO V2 cohorts between 2026-08-22 and
    2026-08-26, of which 13 still have no proposal -- each an `OPEN` row
    with no expiry, permanently eligible in every generator run. This test
    reproduces exactly that: an agenda item persisted from a V2-sourced
    fact (as one would have been before the defect was known), then a
    generator run using the real ``generation_context()``.

    The item is **excluded, never closed**: its status stays `OPEN` and the
    row is untouched, matching this milestone's "no automatic closure or
    deletion of old research artifacts" boundary."""

    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="agg-preexisting-v2",
            cohort_id="cohort-preexisting-v2",
            run_id="run-preexisting",
            qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
            followup_policy_version=FOLLOWUP_POLICY_V2,
        )
    )
    repository = PostgresResearchOperationsRepository(postgres_engine)
    legacy_fact = ResearchAgendaSourceFact.create(
        fact_id="counterfactual-outcome-cohort-preexisting-v2",
        fact_type="COUNTERFACTUAL_OUTCOME_PATTERN",
        venue=ResearchVenue.CRYPTO,
        market_scope=("CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1",),
        state="QUALIFIED",
        summary="retained before the V2 temporal-integrity defect was known",
        evidence_ids=("agg-preexisting-v2",),
        evidence_hashes=("agg-preexisting-v2-hash".ljust(64, "0")[:64],),
        observed_at=NOW,
    )
    refresh = RefreshResearchAgenda(repository).execute(facts=(legacy_fact,))
    assert len(refresh.items) == 1
    retained_item = refresh.items[0]
    assert retained_item.status.value == "OPEN"

    source = RetainedResearchAgendaSource(postgres_engine)
    context = source.generation_context(agenda_items=())
    assert context.excluded_source_fact_ids == ("counterfactual-outcome-cohort-preexisting-v2",)

    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-quarantine-preexisting",
        generated_at=NOW,
        actor="system:fixture",
        context=context,
    )
    assert [value.agenda_item_id for value in generated.proposals] == []

    # Excluded, never closed: the retained row is exactly as it was.
    still_retained = {value.agenda_item_id: value for value in repository.agenda_items()}
    assert still_retained[retained_item.agenda_item_id].status.value == "OPEN"
    assert (
        still_retained[retained_item.agenda_item_id].canonical_hash == retained_item.canonical_hash
    )


def test_a_current_version_agenda_item_still_generates_a_proposal(
    postgres_engine: Engine,
) -> None:
    """The control for the test above: the identical shape under CRYPTO's
    current, non-quarantined policy version still generates normally, so
    the exclusion is proven to be about the policy version and not about
    counterfactual-outcome agenda items in general."""

    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    outcome_repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="agg-current-v3-control",
            cohort_id="cohort-current-v3-control",
            run_id="run-control",
            qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
            followup_policy_version=FOLLOWUP_POLICY_V3,
        )
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    repository = PostgresResearchOperationsRepository(postgres_engine)
    refresh = RefreshResearchAgenda(repository).execute(
        facts=_only_counterfactual_outcome(source.facts(as_of=NOW))
    )
    assert len(refresh.items) == 1

    context = source.generation_context(agenda_items=tuple(refresh.items))
    assert context.excluded_source_fact_ids == ()
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-control",
        generated_at=NOW,
        actor="system:fixture",
        context=context,
    )
    assert [value.agenda_item_id for value in generated.proposals] == [
        refresh.items[0].agenda_item_id
    ]


def _legacy_counterfactual_proposal(*, cohort_id: str) -> ResearchProposal:
    """A proposal shaped exactly like the ones production already retains
    against quarantined V2 cohorts: same proposed family suffix, same
    ``counterfactual-outcome-<cohort_id>`` entry in ``required_data_ids``,
    same ``RETAINED_COUNTERFACTUAL_AGGREGATE_ONLY`` scope."""

    return ResearchProposal.create(
        proposal_id=f"legacy-proposal-{cohort_id}",
        version=1,
        agenda_item_id=f"agenda-counterfactual-outcome-{cohort_id}",
        venue=ResearchVenue.CRYPTO,
        hypothesis="Retained qualified counterfactual outcome evidence warrants policy review.",
        expected_mechanism="Retrospective review of an already-retained aggregate.",
        instruments=("CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1",),
        proposed_family="CRYPTO_COUNTERFACTUAL_OUTCOME_POLICY_REVIEW",
        definition_kind="EXISTING_RESEARCH_REPLICATION",
        strategy_definition=(("semantics", "FROZEN"),),
        exact_change_from_prior="REVIEW_WHETHER_CURRENT_REJECTION_POLICY_REMAINS_CALIBRATED",
        prior_research_ids=(),
        required_data_ids=(f"counterfactual-outcome-{cohort_id}",),
        experiment_configuration=(("scope", "RETAINED_COUNTERFACTUAL_AGGREGATE_ONLY"),),
        validation_windows=(("window", "RETAINED_EVIDENCE_ONLY"),),
        cost_assumptions=(("costs", "NOT_APPLICABLE_TO_RETROSPECTIVE_REVIEW"),),
        success_criteria=("POLICY_CONFIRMED_CALIBRATED",),
        failure_criteria=("POLICY_NOT_CONFIRMED_CALIBRATED",),
        maximum_experiment_runs=1,
        origin=ResearchProposalOrigin.SYSTEM_HEURISTIC,
        actor="system:fixture",
        warnings=(),
        created_at=NOW,
    )


class _UnreachableExecutor:
    """Proves the generic ledger/operational executors are never reached
    for a ``COUNTERFACTUAL_OUTCOME_POLICY_REVIEW`` proposal."""

    def execute(self, **_kwargs: object) -> None:
        raise AssertionError("this executor must never be called for this proposal family")
