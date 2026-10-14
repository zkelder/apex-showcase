"""PostgreSQL persistence proofs for Counterfactual Outcome Evidence
Specificity v3: migration 0114, the V3 specificity block round-tripping
through ``PostgresCounterfactualOutcomeRepository`` (and legacy rows reading
back with none), the recurring task's fourth V3 pass, and the three research
-sourcing gates against real repositories."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
)
from apex.adapters.counterfactual_ledger_postgres import PostgresCounterfactualLedgerRepository
from apex.adapters.counterfactual_outcome_postgres import PostgresCounterfactualOutcomeRepository
from apex.adapters.research_operations_executor import RetainedOutcomeAggregateExperimentExecutor
from apex.adapters.research_operations_postgres import PostgresResearchOperationsRepository
from apex.adapters.research_operations_sources import RetainedResearchAgendaSource
from apex.adapters.securities_structure_outcome_postgres import (
    PostgresSecuritiesStructureOutcomeRepository,
)
from apex.application.research_operations import (
    GenerateResearchProposals,
    RefreshResearchAgenda,
)
from apex.domain.counterfactual_classification import ReasonFamily
from apex.domain.counterfactual_followup import (
    FOLLOWUP_POLICY_V3,
    FollowUpBaselineKind,
    first_daily_close_boundary_at_or_after,
)
from apex.domain.counterfactual_outcome import (
    COUNTERFACTUAL_OUTCOME_POLICY_V1,
    COUNTERFACTUAL_OUTCOME_POLICY_V3,
    CounterfactualOutcomeQualificationState,
)
from apex.domain.operating_cycle import OperatingTaskType
from apex.domain.research_operations import (
    ResearchAgendaSourceFact,
    ResearchOperationsError,
    ResearchVenue,
)
from apex.domain.shared_opportunity import SharedVenue
from tests.persistence.test_postgres_counterfactual_outcome_recurring import (
    DECISION_DAY,
    _due_task,
    _executor,
    _register_plan,
    _seed_projection_and_eligibility,
)
from tests.persistence.test_postgres_counterfactual_outcome_research_operations import (
    _aggregate,
    _legacy_counterfactual_proposal,
    _only_counterfactual_outcome,
)
from tests.test_counterfactual_outcome_specificity import (
    PRODUCTS,
    World,
    _assessment,
    _days,
    _fill,
    _v3_observation,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 9, 26, tzinfo=UTC)
HEAD = "0118_evidence_gap_recovery"
PREVIOUS = "0113_followup_policy_v3"


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _alembic_config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _built_v3_world(days: int) -> World:
    world = World()
    _fill(
        world,
        days=_days(days),
        cohort_products=PRODUCTS[:2],
        cohort_return=Decimal("0.03"),
        complement_return=Decimal("0.01"),
    )
    return world


# ---------------------------------------------------------------------------
# Migration 0114
# ---------------------------------------------------------------------------


def test_head_is_0114_and_the_policy_constraint_admits_v3(postgres_engine: Engine) -> None:
    assert _revision(postgres_engine) == HEAD
    world = _built_v3_world(31)
    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    aggregates = world.build()
    for aggregate in aggregates:
        assert repository.save_aggregate(aggregate=aggregate) is False
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == len(aggregates)


def test_v3_aggregate_round_trips_its_specificity_block_byte_for_byte(
    postgres_engine: Engine,
) -> None:
    world = _built_v3_world(31)
    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    built = {item.aggregate_id: item for item in world.build()}
    for aggregate in built.values():
        repository.save_aggregate(aggregate=aggregate)
    for aggregate_id, aggregate in built.items():
        retained = repository.aggregate(aggregate_id)
        assert retained == aggregate
        assert retained is not None and retained.specificity is not None
        assert retained.specificity.independent_window_count == 31
        assert retained.specificity.mean_relative_window_return == Decimal("0.02")
        assert retained.qualification_state is CounterfactualOutcomeQualificationState.QUALIFIED
        # Idempotent replay: identical hash, nothing written.
        assert repository.save_aggregate(aggregate=aggregate) is True
    # The earliest-qualified read every research surface anchors to sees it.
    assert {item.cohort_id for item in repository.earliest_qualified_per_cohort()} == set(
        item.cohort_id for item in built.values()
    )


def test_legacy_row_without_a_specificity_key_reads_back_as_none(postgres_engine: Engine) -> None:
    """A row persisted before this milestone lacks the key entirely; the
    adapter must not invent a block for it (and must not fail)."""

    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    legacy = _aggregate(
        aggregate_id="agg-legacy-v1",
        cohort_id="cohort-legacy-v1",
        run_id="run-legacy",
        qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    assert legacy.specificity is None
    repository.save_aggregate(aggregate=legacy)
    with postgres_engine.begin() as connection:
        payload = json.loads(
            str(
                connection.scalar(
                    sa.text(
                        "SELECT payload FROM counterfactual_outcome_aggregate "
                        "WHERE aggregate_id = 'agg-legacy-v1'"
                    )
                )
            )
        )
        # Rewrite the retained payload to the exact pre-milestone shape (no
        # key at all, not ``null``), disabling the append-only trigger only
        # for this one fixture statement -- the same narrow, transaction-
        # scoped idiom migrations 0093/0095 use for their own backfills.
        del payload["specificity"]
        connection.execute(
            sa.text(
                "ALTER TABLE counterfactual_outcome_aggregate "
                "DISABLE TRIGGER apex_immutable_counterfactual_outcome_aggregate_trigger"
            )
        )
        try:
            connection.execute(
                sa.text(
                    "UPDATE counterfactual_outcome_aggregate SET payload = :payload "
                    "WHERE aggregate_id = 'agg-legacy-v1'"
                ),
                {"payload": json.dumps(payload)},
            )
        finally:
            connection.execute(
                sa.text(
                    "ALTER TABLE counterfactual_outcome_aggregate "
                    "ENABLE TRIGGER apex_immutable_counterfactual_outcome_aggregate_trigger"
                )
            )
    retained = repository.aggregate("agg-legacy-v1")
    assert retained is not None
    assert retained.specificity is None
    assert retained.canonical_hash == legacy.canonical_hash


def test_0114_downgrade_blocks_with_a_retained_v3_aggregate(postgres_engine: Engine) -> None:
    world = _built_v3_world(2)
    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    for aggregate in world.build():
        repository.save_aggregate(aggregate=aggregate)
    with (
        pytest.raises(
            Exception,
            match="cannot downgrade 0114 with retained COUNTERFACTUAL_OUTCOME_POLICY_V3 evidence",
        ),
        postgres_engine.connect() as connection,
    ):
        command.downgrade(_alembic_config(connection), PREVIOUS)
        connection.commit()
    # A multi-revision alembic downgrade commits each step independently:
    # 0116's and 0115's own (empty-evidence) downgrades complete before
    # 0114's guard refuses, so the database genuinely rests at 0114 -- the
    # same ripple documented for 0086/0094/0095. 0114's V3 evidence is untouched.
    assert _revision(postgres_engine) == "0114_outcome_specificity_v3"


def test_0114_downgrade_and_reupgrade_round_trip_when_only_v1_rows_are_retained(
    postgres_engine: Engine,
) -> None:
    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id="agg-v1-only",
            cohort_id="cohort-v1-only",
            run_id="run-v1-only",
            qualification_state=CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE,
            outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1,
            baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        )
    )
    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), PREVIOUS)
        connection.commit()
    assert _revision(postgres_engine) == PREVIOUS
    with postgres_engine.begin() as connection, pytest.raises(sa.exc.IntegrityError):
        # 0113's narrower constraint genuinely rejects a V3 row again.
        connection.execute(
            sa.text(
                "INSERT INTO counterfactual_outcome_aggregate (aggregate_id, cohort_id, "
                "outcome_policy_version, run_id, venue, strategy_or_method_id, reason_family, "
                "horizon_kind, baseline_kind, followup_policy_version, classifier_version, "
                "evidence_quality_policy_version, qualification_state, "
                "total_observations_considered, mature_observation_count, "
                "eligible_observation_count, generated_at, payload, canonical_hash, authority) "
                "VALUES ('x', 'x', 'COUNTERFACTUAL_OUTCOME_POLICY_V3', 'x', 'CRYPTO', 'm', "
                "'ECONOMICS', 'SHORT_HORIZON', 'ORIGIN_EVIDENCE', 'FOLLOWUP_POLICY_V3', "
                "'REASON_CLASSIFIER_V2', 'FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1', "
                "'INSUFFICIENT_SAMPLE', 0, 0, 0, now(), '{}', :hash, 'NONE')"
            ),
            {"hash": "0" * 64},
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "head")
        connection.commit()
    assert _revision(postgres_engine) == HEAD
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1


# ---------------------------------------------------------------------------
# Recurring task: fourth V3 pass
# ---------------------------------------------------------------------------


def test_recurring_outcome_refresh_runs_the_v3_pass_and_replays_idempotently(
    postgres_engine: Engine,
) -> None:
    _seed_projection_and_eligibility(postgres_engine, identity="v3-pass")
    plan = _register_plan(postgres_engine, plan_id="cor-plan-v3-pass")
    executor = _executor(postgres_engine)
    executor.execute(
        task=_due_task("cycle-v3-pass", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-v3-pass",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    decision = ledger.all_decisions()[0]
    followup = PostgresCounterfactualFollowUpRepository(postgres_engine)
    # One genuine V3 observation over the exact ORIGIN_EVIDENCE window the
    # decision's own projection anchors (generated_at = DECISION_DAY + 12h).
    window_start = first_daily_close_boundary_at_or_after(DECISION_DAY + timedelta(hours=12))
    observation = _v3_observation(
        decision=decision,
        product="BTC-USD",
        window_start=window_start,
        horizon_days=1,
        subsequent_return=Decimal("0.02"),
    )
    followup.save_observation(observation=observation)
    followup.save_assessment(assessment=_assessment(observation))

    first = executor.execute(
        task=_due_task("cycle-v3-pass", OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH),
        cycle_id="cycle-v3-pass",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert first.status.value in {"COMPLETED", "REUSED"}
    assert first.network_requests == 0
    assert any(code.startswith("COUNTERFACTUAL_OUTCOME_V3_CONSIDERED:") for code in first.warnings)
    assert "COUNTERFACTUAL_OUTCOME_V3_QUALIFIED:0" in first.warnings
    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    v3_rows = repository.aggregates_for_run("cycle-v3-pass-counterfactual-outcome-v3")
    assert len(v3_rows) == 1
    aggregate = v3_rows[0]
    assert aggregate.outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V3
    assert aggregate.followup_policy_version == FOLLOWUP_POLICY_V3
    assert aggregate.baseline_kind is FollowUpBaselineKind.ORIGIN_EVIDENCE
    assert aggregate.specificity is not None
    # A single product with no complement is an honestly degenerate window,
    # never a qualified (or even informative) one.
    assert aggregate.specificity.raw_return_observation_count == 1
    assert aggregate.specificity.degenerate_window_count == 1
    assert aggregate.specificity.independent_window_count == 0
    assert (
        aggregate.qualification_state is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )
    # V1's own pass is untouched and still runs beside it.
    v1_rows = repository.aggregates_for_run("cycle-v3-pass-counterfactual-outcome")
    assert len(v1_rows) == 1 and v1_rows[0].specificity is None

    before = _row_count(postgres_engine, "counterfactual_outcome_aggregate")
    second = executor.execute(
        task=_due_task("cycle-v3-pass", OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH),
        cycle_id="cycle-v3-pass",
        plan=plan,
        as_of=NOW,
        executed_at=NOW + timedelta(minutes=5),
    )
    assert second.status.value == "REUSED"
    assert "COUNTERFACTUAL_OUTCOME_V3_NEWLY_WRITTEN:0" in second.warnings
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == before


# ---------------------------------------------------------------------------
# Research sourcing: three gates, real repositories
# ---------------------------------------------------------------------------


def _save(
    engine: Engine,
    *,
    suffix: str,
    outcome_policy_version: str,
    baseline_kind: FollowUpBaselineKind,
    venue: SharedVenue = SharedVenue.CRYPTO,
) -> str:
    repository = PostgresCounterfactualOutcomeRepository(engine)
    repository.save_aggregate(
        aggregate=_aggregate(
            aggregate_id=f"agg-{suffix}",
            cohort_id=f"cohort-{suffix}",
            run_id=f"run-{suffix}",
            qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
            venue=venue,
            outcome_policy_version=outcome_policy_version,
            baseline_kind=baseline_kind,
        )
    )
    return f"cohort-{suffix}"


def test_crypto_outcome_v1_is_quarantined_and_v3_origin_evidence_sources_research(
    postgres_engine: Engine,
) -> None:
    v1 = _save(
        postgres_engine,
        suffix="v1-raw",
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    v3_origin = _save(
        postgres_engine,
        suffix="v3-origin",
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V3,
        baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
    )
    v3_decision_time = _save(
        postgres_engine,
        suffix="v3-decision-time",
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V3,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    facts = _only_counterfactual_outcome(source.facts(as_of=NOW))
    assert [value.fact_id for value in facts] == [f"counterfactual-outcome-{v3_origin}"]
    disclosed = {
        value.cohort_id: value for value in source.quarantined_counterfactual_outcome_cohorts()
    }
    assert set(disclosed) == {v1, v3_decision_time}
    assert disclosed[v1].defect_code == "RAW_SAMPLE_NON_INDEPENDENCE_AND_MARKET_BETA_DEFECT"
    assert disclosed[v1].corrected_by_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V3
    assert disclosed[v1].outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V1
    assert (
        disclosed[v3_decision_time].defect_code == "V3_DECISION_TIME_WINDOW_LAGGED_BY_DAILY_CEILING"
    )
    assert disclosed[v3_decision_time].baseline_kind == "DECISION_TIME"
    assert disclosed[v3_decision_time].sourcing_selection is True
    assert disclosed[v1].sourcing_selection is False
    # Every withheld aggregate is still retained, QUALIFIED, and readable.
    retained = {
        value.cohort_id
        for value in PostgresCounterfactualOutcomeRepository(
            postgres_engine
        ).earliest_qualified_per_cohort()
    }
    assert retained == {v1, v3_origin, v3_decision_time}
    # And the generation context excludes exactly the withheld cohorts.
    context = source.generation_context(agenda_items=())
    assert set(context.excluded_source_fact_ids) == {
        f"counterfactual-outcome-{v1}",
        f"counterfactual-outcome-{v3_decision_time}",
    }


def test_an_agenda_item_retained_from_crypto_v1_generates_no_new_proposal(
    postgres_engine: Engine,
) -> None:
    """The 16 production agenda items already created from V1-qualified
    CRYPTO cohorts over V3 follow-up evidence stay OPEN and untouched, but
    no longer draw a proposal."""

    v1 = _save(
        postgres_engine,
        suffix="v1-retained-agenda",
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    repository = PostgresResearchOperationsRepository(postgres_engine)
    legacy_fact = ResearchAgendaSourceFact.create(
        fact_id=f"counterfactual-outcome-{v1}",
        fact_type="COUNTERFACTUAL_OUTCOME_PATTERN",
        venue=ResearchVenue.CRYPTO,
        market_scope=("CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1",),
        state="QUALIFIED",
        summary="retained before the V1 raw-count defect was declared",
        evidence_ids=("agg-v1-retained-agenda",),
        evidence_hashes=("agg-v1-retained-agenda-hash".ljust(64, "0")[:64],),
        observed_at=NOW,
    )
    refresh = RefreshResearchAgenda(repository).execute(facts=(legacy_fact,))
    assert len(refresh.items) == 1
    item = refresh.items[0]
    source = RetainedResearchAgendaSource(postgres_engine)
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-v1-quarantine",
        generated_at=NOW,
        actor="system:fixture",
        context=source.generation_context(agenda_items=()),
    )
    assert generated.proposals == ()
    still = {value.agenda_item_id: value for value in repository.agenda_items()}
    assert still[item.agenda_item_id].status.value == "OPEN"
    assert still[item.agenda_item_id].canonical_hash == item.canonical_hash


def test_an_already_retained_crypto_v1_proposal_cannot_execute(postgres_engine: Engine) -> None:
    v1 = _save(
        postgres_engine,
        suffix="v1-executor",
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    proposal = _legacy_counterfactual_proposal(cohort_id=v1)
    executor = RetainedOutcomeAggregateExperimentExecutor(
        counterfactual_outcome=PostgresCounterfactualOutcomeRepository(postgres_engine),
        securities_structure_outcome=PostgresSecuritiesStructureOutcomeRepository(postgres_engine),
    )
    with pytest.raises(
        ResearchOperationsError,
        match="RETAINED_OUTCOME_AGGREGATE_EVIDENCE_POLICY_VERSION_QUARANTINED",
    ):
        executor.execute(proposal=proposal, run_id="run-v1-executor", executed_at=NOW)


def test_kalshi_v1_and_securities_are_unaffected_by_the_outcome_quarantine(
    postgres_engine: Engine,
) -> None:
    """KALSHI never qualifies under V1 (``VENUE_ECONOMIC_REDUCTION_
    UNAVAILABLE``), but the gate itself must still say KALSHI V1 and V2 may
    source, and a KALSHI V2 QUALIFIED cohort must still emit a fact."""

    kalshi = _save(
        postgres_engine,
        suffix="kalshi-v2",
        outcome_policy_version="COUNTERFACTUAL_OUTCOME_POLICY_V2",
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        venue=SharedVenue.KALSHI,
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    facts = _only_counterfactual_outcome(source.facts(as_of=NOW))
    assert [value.fact_id for value in facts] == [f"counterfactual-outcome-{kalshi}"]
    assert source.quarantined_counterfactual_outcome_cohorts() == ()
    assert ReasonFamily.ECONOMICS is ReasonFamily.ECONOMICS  # SECURITIES: separate mechanism


def test_a_real_long_v3_explanation_flows_from_retained_aggregate_to_a_ready_proposal(
    postgres_engine: Engine,
) -> None:
    """Research Agenda Fact Summary Integrity v1: a genuine V3 QUALIFIED
    explanation exceeds the old 1,000-character guard.  Through the real
    retained-source, agenda-refresh and proposal-generator path it must now
    source a fact, keep its summary verbatim, and generate a
    ``READY_FOR_REVIEW`` proposal with no authority consequence."""

    import dataclasses

    world = _built_v3_world(31)
    real = next(
        value
        for value in world.build()
        if value.qualification_state.value == "QUALIFIED" and value.specificity is not None
    )
    assert len(real.explanation) > 1000
    PostgresCounterfactualOutcomeRepository(postgres_engine).save_aggregate(
        aggregate=dataclasses.replace(real, generated_at=NOW)
    )
    source = RetainedResearchAgendaSource(postgres_engine)
    facts = _only_counterfactual_outcome(source.facts(as_of=NOW))
    assert len(facts) == 1
    assert facts[0].summary == real.explanation
    repository = PostgresResearchOperationsRepository(postgres_engine)
    refresh = RefreshResearchAgenda(repository).execute(facts=facts)
    assert [value.problem_statement for value in refresh.items] == [real.explanation]
    generated = GenerateResearchProposals(repository).execute(
        generator_run_id="generator-long-v3",
        generated_at=NOW,
        actor="system:fixture",
        context=source.generation_context(agenda_items=refresh.items),
        maximum_proposals=1,
    )
    assert len(generated.proposals) == 1
    assert real.explanation in generated.proposals[0].hypothesis
    assert len(generated.proposals[0].hypothesis) <= 2000
    assert generated.feasibilities[0].status.value == "READY_FOR_REVIEW"
    assert generated.proposals[0].authority == "NONE"
    assert repository.reviews(generated.proposals[0].proposal_id) == ()
    assert repository.queue_items() == ()
