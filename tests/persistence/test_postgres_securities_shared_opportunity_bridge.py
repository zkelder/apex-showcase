"""Securities Research -> Shared Opportunity Recurring Bridge v1:
persistence-level proofs.

Every ``OptionStructureCandidate`` here is built with the identical,
unmodified ``domain.options_structures.generate_structure_candidates``
pipeline ``tests/test_options_structures.py`` itself uses (imported directly,
not reimplemented) -- no hand-crafted candidate, matching this codebase's
established RED-testing standard. This file proves the adapter-layer
contract the domain-level test file cannot: real batched queries against a
real database, real FK-constrained persistence, real migration 0104 CHECK
constraints, and the real recurring-cycle composition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
)
from apex.adapters.counterfactual_ledger_postgres import PostgresCounterfactualLedgerRepository
from apex.adapters.options_quote_intelligence_postgres import (
    PostgresOptionsQuoteIntelligenceRepository,
)
from apex.adapters.options_structures_postgres import PostgresOptionsStructuresRepository
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.adapters.shared_opportunity_sources import RetainedSharedOpportunitySource
from apex.application.counterfactual_followup import SyncFollowUpObservations
from apex.domain.counterfactual_ledger import build_decision_from_eligibility
from apex.domain.options_intelligence import OptionType
from apex.domain.options_quote_intelligence import (
    CaptureRunStatus,
    OptionChainCaptureBounds,
    OptionChainCapturePlan,
    OptionChainCaptureRun,
)
from apex.domain.options_structures import StructurePricingPolicy
from apex.domain.shared_opportunity import (
    EligibilityState,
    PriorityTier,
    SharedVenue,
    build_opportunity_set,
    evaluate_eligibility,
)
from tests.persistence.test_postgres_options_quote_intelligence import _members
from tests.test_options_structures import (
    UNDERLYING_ID,
    build,
    make_observation,
)

NOW = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)  # a Thursday, XNYS close


def _chain_at(captured_at: datetime) -> tuple:
    """The same six-leg synthetic-but-genuinely-two-sided chain
    ``tests.test_options_structures.standard_chain`` builds, reconstructed
    (via the identical, unmodified ``make_observation``) with
    ``quote_timestamp`` pinned to this session's own ``captured_at`` --
    ``standard_chain``'s own ``quote_timestamp`` default is fixed at that
    module's single module-level ``NOW`` regardless of ``captured_at``
    (correct for that module's own single-instant tests; genuinely wrong for
    this file's multi-session scenarios, where a later session's observations
    must not be measured stale against an unrelated, unmoving quote_timestamp
    default)."""

    strikes_and_quotes = (
        (OptionType.CALL, "495", "9.00", "9.20"),
        (OptionType.CALL, "500", "6.00", "6.20"),
        (OptionType.CALL, "505", "4.00", "4.20"),
        (OptionType.PUT, "495", "4.00", "4.20"),
        (OptionType.PUT, "500", "6.00", "6.20"),
        (OptionType.PUT, "505", "9.00", "9.20"),
    )
    return tuple(
        make_observation(
            option_type=option_type,
            strike=strike,
            bid=bid,
            ask=ask,
            quote_timestamp=captured_at,
            captured_at=captured_at,
        )
        for option_type, strike, bid, ask in strikes_and_quotes
    )


def _ensure_capture_lineage(engine: sa.engine.Engine) -> None:
    """``tests.test_options_structures.make_snapshot`` stamps a fixed
    ``plan_id="plan-1"``/``run_id="run-1"`` (that domain-only test never
    touches Postgres, so it never needed a real FK parent) -- this
    repository's own ``options_chain_snapshot`` table genuinely FK-references
    both. Hand-constructed directly (not via ``.create()``, which derives its
    own content-addressed ids) so the ids match exactly; content otherwise
    does not matter to any test in this file. Idempotent by primary key
    (``save_plan``/``save_run`` both check-then-insert), so calling this once
    per session capture is safe."""

    quote_repo = PostgresOptionsQuoteIntelligenceRepository(engine)
    plan = OptionChainCapturePlan(
        plan_id="plan-1",
        cohort_policy_id="research_cohort_policy_test",
        cohort_policy_hash="0" * 64,
        selection_version="v1",
        members=_members(),
        unresolved_symbols=(),
        bounds=OptionChainCaptureBounds.create(),
        provider="ALPACA_OPTIONS_MARKET_DATA",
        feed="indicative",
        created_at=NOW,
        canonical_hash="1" * 64,
    )
    run = OptionChainCaptureRun(
        run_id="run-1",
        plan_id="plan-1",
        requested_at=NOW,
        completed_at=NOW,
        status=CaptureRunStatus.COMPLETE,
        underlyings_requested=1,
        underlyings_captured=1,
        provider_request_count=1,
        payload_bytes=0,
        contracts_considered=6,
        contracts_quoted=6,
        runtime_seconds=Decimal("1"),
        snapshot_ids=(),
        reason=None,
        canonical_hash="2" * 64,
    )
    quote_repo.save_plan(plan)
    quote_repo.save_run(run)


def _persist_full_session(engine: sa.engine.Engine, *, captured_at: datetime):
    """Runs the real, unmodified candidate-generation pipeline
    (``tests.test_options_structures.build``) for one session and persists
    every FK-required layer in dependency order (observations -> snapshot ->
    policies -> structures) -- mirrors exactly what the real
    ``AlpacaOptionsResearchEvidenceCapture`` executor's own 3-phase persist
    does, just without a provider client in front of it.

    ``StructurePricingPolicy.create()`` takes no arguments and is therefore
    deterministic -- calling it again here reproduces the identical
    ``policy_id`` ``build()`` already embedded into ``structure_policy.
    pricing_policy_id`` internally (``build()`` itself does not return the
    pricing policy object it minted); ``save_pricing_policy`` is itself
    idempotent (checks by ``canonical_hash`` before inserting), so this is
    a safe, genuine save, not a guess."""

    _ensure_capture_lineage(engine)
    observations = _chain_at(captured_at)
    result, snapshot, structure_policy, fee, slippage = build(observations, captured_at=captured_at)
    quote_repo = PostgresOptionsQuoteIntelligenceRepository(engine)
    quote_repo.save_observations(observations)
    quote_repo.save_snapshot(snapshot)
    structures_repo = PostgresOptionsStructuresRepository(engine)
    structures_repo.save_pricing_policy(StructurePricingPolicy.create())
    structures_repo.save_fee_policy(fee)
    structures_repo.save_slippage_policy(slippage)
    structures_repo.save_structure_policy(structure_policy)
    structures_repo.save_structures(result.evaluated)
    return result, snapshot, quote_repo, structures_repo


@pytest.mark.postgres
class TestLatestStructuresByUnderlying:
    def test_returns_only_the_latest_session_per_underlying(
        self, postgres_engine: sa.engine.Engine
    ) -> None:
        older = NOW - timedelta(days=1)
        _result_old, _snap_old, _quote_repo, structures_repo = _persist_full_session(
            postgres_engine, captured_at=older
        )
        result_new, snap_new, _quote_repo2, _structures_repo2 = _persist_full_session(
            postgres_engine, captured_at=NOW
        )

        latest = structures_repo.latest_structures_by_underlying()

        assert latest
        assert {item.underlying_canonical_security_id for item in latest} == {UNDERLYING_ID}
        assert {item.evaluated_at for item in latest} == {NOW}
        assert {item.structure_id for item in latest} == {
            item.structure_id for item in result_new.evaluated
        }
        assert snap_new.snapshot_id in {item.source_chain_snapshot_id for item in latest}


@pytest.mark.postgres
class TestEarliestSnapshotAtOrAfter:
    def test_returns_the_earliest_snapshot_on_or_after_the_boundary(
        self, postgres_engine: sa.engine.Engine
    ) -> None:
        quote_repo = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
        day1 = NOW
        day2 = NOW + timedelta(days=1)
        day3 = NOW + timedelta(days=2)
        for captured_at in (day1, day2, day3):
            _persist_full_session(postgres_engine, captured_at=captured_at)

        found = quote_repo.earliest_snapshot_for_underlying_at_or_after(
            UNDERLYING_ID, day2 - timedelta(hours=1)
        )
        assert found is not None
        assert found.captured_at == day2

        exact = quote_repo.earliest_snapshot_for_underlying_at_or_after(UNDERLYING_ID, day2)
        assert exact is not None
        assert exact.captured_at == day2

        none_found = quote_repo.earliest_snapshot_for_underlying_at_or_after(
            UNDERLYING_ID, day3 + timedelta(days=1)
        )
        assert none_found is None


@pytest.mark.postgres
class TestSecuritiesProjectionsProducer:
    def test_produces_eligible_research_only_projections_from_real_evidence(
        self, postgres_engine: sa.engine.Engine
    ) -> None:
        result, _snapshot, _quote_repo, _structures_repo = _persist_full_session(
            postgres_engine, captured_at=NOW
        )
        source = RetainedSharedOpportunitySource(postgres_engine)

        projections = source.securities_projections(as_of=NOW)

        assert projections
        retained_ids = {item.structure_id for item in result.evaluated}
        seen_source_ids = {item.source_record_id for item in projections}
        # Every projected candidate really was retained; quality filtering
        # may legitimately exclude some (MARGINAL/UNUSABLE), never include
        # an id that was never generated.
        assert seen_source_ids <= retained_ids
        assert seen_source_ids  # the synthetic standard_chain fixture is high-quality
        for projection in projections:
            assert projection.venue is SharedVenue.SECURITIES
            assert projection.evidence_class == "GENUINE_PUBLIC"
            assert projection.comparable_value is None
            assert projection.authority == "NONE"
            decision = evaluate_eligibility(projection, evaluated_at=NOW)
            assert decision.state is EligibilityState.ELIGIBLE

    def test_replay_is_idempotent_same_as_of_reproduces_identical_projections(
        self, postgres_engine: sa.engine.Engine
    ) -> None:
        _persist_full_session(postgres_engine, captured_at=NOW)
        source = RetainedSharedOpportunitySource(postgres_engine)

        first = source.securities_projections(as_of=NOW + timedelta(minutes=5))
        second = source.securities_projections(as_of=NOW + timedelta(minutes=5))

        assert {item.projection_id for item in first} == {item.projection_id for item in second}
        first_by_id = {item.projection_id: item for item in first}
        second_by_id = {item.projection_id: item for item in second}
        for projection_id, item in first_by_id.items():
            assert item.canonical_hash == second_by_id[projection_id].canonical_hash

    def test_stops_projecting_a_structure_once_a_newer_session_supersedes_it(
        self, postgres_engine: sa.engine.Engine
    ) -> None:
        older = NOW - timedelta(days=1)
        result_old, _snap_old, _q, _s = _persist_full_session(postgres_engine, captured_at=older)
        source = RetainedSharedOpportunitySource(postgres_engine)
        old_ids = {
            f"shared-securities-structure-{item.structure_id}" for item in result_old.evaluated
        }

        _persist_full_session(postgres_engine, captured_at=NOW)
        current = source.securities_projections(as_of=NOW)

        current_ids = {item.projection_id for item in current}
        assert current_ids.isdisjoint(old_ids)


@pytest.mark.postgres
def test_shared_opportunity_bundle_round_trips_immutably_through_postgres(
    postgres_engine: sa.engine.Engine,
) -> None:
    """The real repository round trip ``application.shared_opportunity.
    MaterializeSharedOpportunitySet`` itself performs for CRYPTO/KALSHI,
    proven directly for SECURITIES: build_opportunity_set (pure) +
    PostgresSharedOpportunityRepository.save_opportunity_bundle (real DB)."""

    _persist_full_session(postgres_engine, captured_at=NOW)
    source = RetainedSharedOpportunitySource(postgres_engine)
    projections = source.securities_projections(as_of=NOW)
    assert projections

    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id="securities-bridge-persistence-set-1",
        created_at=NOW,
        projections=projections,
    )
    repository = PostgresSharedOpportunityRepository(postgres_engine)
    replay = repository.save_opportunity_bundle(
        idempotency_key="securities-bridge-persistence-key-1",
        opportunity_set=opportunity_set,
        projections=projections,
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    assert replay is False

    reloaded_set = repository.opportunity_set(opportunity_set.opportunity_set_id)
    assert reloaded_set is not None
    assert reloaded_set.canonical_hash == opportunity_set.canonical_hash
    for projection in projections:
        reloaded = repository.projection(projection.projection_id)
        assert reloaded is not None
        assert reloaded.canonical_hash == projection.canonical_hash
        assert reloaded.venue is SharedVenue.SECURITIES

    every_priority = repository.priorities(opportunity_set.opportunity_set_id)
    assert every_priority
    assert all(item.tier is PriorityTier.RESEARCH_ONLY for item in every_priority)

    # Idempotent replay: identical inputs, identical idempotency key.
    replay_again = repository.save_opportunity_bundle(
        idempotency_key="securities-bridge-persistence-key-1",
        opportunity_set=opportunity_set,
        projections=projections,
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    assert replay_again is True


@pytest.mark.postgres
def test_shared_opportunity_rebuild_ranks_the_same_persistent_projection_across_two_days(
    postgres_engine: sa.engine.Engine,
) -> None:
    """Regression for the real 2026-08-24T00:00:54Z SHARED_OPPORTUNITY_REBUILD
    production outage: a real Postgres UniqueViolation on
    ``shared_opportunity_priority_pkey`` --

        DETAIL:  Key (priority_id)=(shared-securities-structure-
        options_structure_33747461635b2064f7bc853a-priority-v1) already exists.

    -- surfacing as ``SharedOpportunityError("SHARED_OPPORTUNITY_PERSISTENCE_
    CONFLICT")``. Root cause: ``_reuse_projection`` correctly keeps a
    SECURITIES structure's projection_id (and its projection_table row)
    identical across days once a structure has been retained once, but
    ``prioritize`` (pre-fix) minted ``priority_id`` from the projection_id
    ALONE, with no ``opportunity_set_id`` component -- unlike the sibling
    ``explanation_id``, which already does. 2026-08-23 was the first day
    ANY SECURITIES structure was ever ranked (270 brand-new priority rows,
    zero possible collision); 2026-08-24 was the first time the *same*
    still-retained structure needed ranking a *second* time under a new
    day's opportunity_set_id -- exactly what this test reproduces directly
    against the real ``shared_opportunity_priority`` table and its real
    primary-key constraint, not a toy in-memory approximation.

    Fails pre-fix with the identical ``SharedOpportunityError`` production
    hit; passes post-fix because priority_id is now scoped by
    opportunity_set_id, so day 2's row is a distinct primary key."""

    _persist_full_session(postgres_engine, captured_at=NOW)
    source = RetainedSharedOpportunitySource(postgres_engine)
    repository = PostgresSharedOpportunityRepository(postgres_engine)

    # Day 1: the structure's first-ever rebuild -- brand new projection AND
    # priority rows, mirroring the real 2026-08-23 cycle.
    day_one_projections = source.securities_projections(as_of=NOW)
    assert day_one_projections
    day_one_set, day_one_decisions, day_one_priorities, day_one_explanations = (
        build_opportunity_set(
            opportunity_set_id="cross-day-rank-set-day-1",
            created_at=NOW,
            projections=day_one_projections,
        )
    )
    repository.save_opportunity_bundle(
        idempotency_key="cross-day-rank-key-day-1",
        opportunity_set=day_one_set,
        projections=day_one_projections,
        decisions=day_one_decisions,
        priorities=day_one_priorities,
        explanations=day_one_explanations,
    )

    # Day 2: the identical retained structure (_reuse_projection returns the
    # SAME projection_id/canonical_hash -- no options capture happens in
    # between) gets ranked again under a NEW opportunity_set_id, exactly
    # like the real once-per-UTC-day SHARED_OPPORTUNITY_REBUILD cycle does.
    day_two = NOW + timedelta(days=1)
    day_two_projections = source.securities_projections(as_of=day_two)
    assert {item.projection_id for item in day_two_projections} == {
        item.projection_id for item in day_one_projections
    }
    day_two_set, day_two_decisions, day_two_priorities, day_two_explanations = (
        build_opportunity_set(
            opportunity_set_id="cross-day-rank-set-day-2",
            created_at=day_two,
            projections=day_two_projections,
        )
    )

    # Pre-fix, this raised SharedOpportunityError("SHARED_OPPORTUNITY_
    # PERSISTENCE_CONFLICT") from a real IntegrityError on
    # shared_opportunity_priority_pkey -- the exact production failure.
    replay = repository.save_opportunity_bundle(
        idempotency_key="cross-day-rank-key-day-2",
        opportunity_set=day_two_set,
        projections=day_two_projections,
        decisions=day_two_decisions,
        priorities=day_two_priorities,
        explanations=day_two_explanations,
    )
    assert replay is False

    day_one_stored = repository.priorities(day_one_set.opportunity_set_id)
    day_two_stored = repository.priorities(day_two_set.opportunity_set_id)
    assert day_one_stored and day_two_stored
    by_projection_day_one = {item.projection_id: item.priority_id for item in day_one_stored}
    by_projection_day_two = {item.projection_id: item.priority_id for item in day_two_stored}
    shared_projection_ids = set(by_projection_day_one) & set(by_projection_day_two)
    assert shared_projection_ids
    for projection_id in shared_projection_ids:
        # Same projection, ranked on two different days: two distinct,
        # independently-persisted priority rows, never a silently reused id.
        assert by_projection_day_one[projection_id] != by_projection_day_two[projection_id]


@pytest.mark.postgres
def test_venue_check_constraint_still_rejects_an_unrecognized_value(
    postgres_engine: sa.engine.Engine,
) -> None:
    """Migration 0104 widened the allow-list; it must not have accidentally
    dropped it. A genuinely bogus venue must still be rejected at the
    database boundary."""

    with (
        postgres_engine.connect() as connection,
        connection.begin(),
        pytest.raises(sa.exc.IntegrityError),
    ):
        connection.execute(
            sa.text(
                "INSERT INTO shared_opportunity_projection "
                "(projection_id, venue, source_record_type, source_record_id, "
                "source_canonical_hash, payload, canonical_hash, authority) "
                "VALUES "
                "('bogus-venue-check', 'BOGUS', 'X', 'x', :hash64, '{}', :hash64, 'NONE')"
            ),
            {"hash64": "a" * 64},
        )


@pytest.mark.postgres
def test_securities_followup_round_trips_end_to_end_against_real_postgres(
    postgres_engine: sa.engine.Engine,
) -> None:
    """The full recurring-cycle chain, for real: a genuine retained structure
    candidate -> a genuine SharedOpportunityProjection -> a genuine
    CounterfactualDecision -> (later session) a genuine terminal
    OptionsChainSnapshot -> SyncFollowUpObservations produces and persists a
    genuine DecisionFollowUpObservation, using the real, production
    PostgresCounterfactualFollowUpRepository (including its real
    ``securities_prices`` composition) -- not a fake/stub source."""

    _result, _snapshot, _quote_repo, _structures_repo = _persist_full_session(
        postgres_engine, captured_at=NOW
    )
    source = RetainedSharedOpportunitySource(postgres_engine)
    (projection, *_rest) = source.securities_projections(as_of=NOW)
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id="securities-followup-set-1",
        created_at=NOW,
        projections=(projection,),
    )
    (eligibility_decision,) = decisions
    assert eligibility_decision.state is EligibilityState.ELIGIBLE

    shared_repository = PostgresSharedOpportunityRepository(postgres_engine)
    shared_repository.save_opportunity_bundle(
        idempotency_key="securities-followup-shared-key-1",
        opportunity_set=opportunity_set,
        projections=(projection,),
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )

    ledger_repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    decision = build_decision_from_eligibility(
        decision=eligibility_decision,
        projection=projection,
        opportunity_set_id="securities-followup-set-1",
    )
    ledger_repository.save_decision(decision=decision)

    # A later real trading-session capture supplies the terminal underlying
    # reference price this follow-up observation needs -- the SAME already-
    # recurring options capture, reused, never a new/synthetic price series.
    target_time = NOW + timedelta(days=projection.expected_holding_days or 1)
    _persist_full_session(postgres_engine, captured_at=target_time + timedelta(hours=1))

    followup_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    result_sync = SyncFollowUpObservations(
        followup_repository.ledger,
        followup_repository.subjects,
        followup_repository.crypto_prices,
        kalshi_source=_NeverCalledKalshiSource(),
        repository=followup_repository,
        securities_prices=followup_repository.securities_prices,
    ).execute(
        as_of=target_time + timedelta(hours=2),
        policy_version="FOLLOWUP_POLICY_V2",
    )

    assert result_sync.created_count >= 1
    matching = [
        entry
        for entry in result_sync.entries
        if entry.decision_id == decision.decision_id and entry.venue is SharedVenue.SECURITIES
    ]
    assert matching
    assert any(entry.outcome == "CREATED" for entry in matching)
    observation_id = next(entry.observation_id for entry in matching if entry.outcome == "CREATED")
    reloaded = followup_repository.observation(observation_id)
    assert reloaded is not None
    assert reloaded.venue is SharedVenue.SECURITIES
    assert reloaded.authority == "NONE"

    # Idempotent replay: a second sync call makes no further genuine attempt
    # for the same (decision, horizon, baseline) triple.
    result_sync_again = SyncFollowUpObservations(
        followup_repository.ledger,
        followup_repository.subjects,
        followup_repository.crypto_prices,
        kalshi_source=_NeverCalledKalshiSource(),
        repository=followup_repository,
        securities_prices=followup_repository.securities_prices,
    ).execute(
        as_of=target_time + timedelta(hours=3),
        policy_version="FOLLOWUP_POLICY_V2",
    )
    assert result_sync_again.idempotent_replay_count >= 1 or result_sync_again.created_count == 0


class _NeverCalledKalshiSource:
    def read_market_status(self, **_: object):
        raise AssertionError("SECURITIES follow-up must never reach the KALSHI source")
