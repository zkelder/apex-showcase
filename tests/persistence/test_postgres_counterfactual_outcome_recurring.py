"""PostgreSQL persistence proofs for Counterfactual Outcome Feedback v1's
recurring-cycle wiring: Phase 0 (ledger + classification sync now run
automatically inside the ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` executor
branch, no longer CLI-only) and Phase 5 (the new
``COUNTERFACTUAL_OUTCOME_REFRESH`` task).

Drives ``ExistingCapabilityOperatingTaskExecutor.execute`` directly for
these two task types against a real, freshly migrated ``apex_test``
database -- the same scoped-executor pattern
``test_postgres_counterfactual_followup_recurring.py`` already
established. Zero genuine network requests anywhere in this file."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
)
from apex.adapters.counterfactual_ledger_postgres import PostgresCounterfactualLedgerRepository
from apex.adapters.counterfactual_outcome_postgres import PostgresCounterfactualOutcomeRepository
from apex.adapters.operating_cycle_postgres import (
    PostgresOperatingCycleRepository,
    PostgresOperatingStateSource,
)
from apex.adapters.operating_cycle_tasks import ExistingCapabilityOperatingTaskExecutor
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.application.counterfactual_followup import FollowUpSourceUnavailable, KalshiMarketRead
from apex.application.operating_cycle import RegisterOperatingCyclePlan
from apex.domain.counterfactual_classification import CURRENT_CLASSIFIER_VERSION
from apex.domain.counterfactual_outcome import CounterfactualOutcomeQualificationState
from apex.domain.operating_cycle import (
    DueDisposition,
    DueTask,
    OperatingCyclePlan,
    OperatingTaskType,
)
from apex.domain.shared_opportunity import (
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
)
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
DECISION_DAY = datetime(2026, 7, 3, 0, 0, tzinfo=UTC)


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _projection(identity: str) -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=f"cor-proj-{identity}",
        venue=SharedVenue.CRYPTO,
        source_record_type="FixtureOpportunity",
        source_record_id=f"cor-source-{identity}",
        source_canonical_hash=content_digest((identity,)),
        strategy_or_method_id="COR_FIXTURE_METHOD_V1",
        instrument_id="BTC",
        product_or_market_id="BTC-USD",
        direction="HYPOTHETICAL_LONG_SPOT_RESEARCH",
        generated_at=DECISION_DAY + timedelta(hours=12),
        valid_until=DECISION_DAY + timedelta(days=60),
        evidence_class="HISTORICAL_PUBLIC",
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
        # Deliberately INSUFFICIENT: this milestone's whole subject is
        # rejected-opportunity evidence (``domain.counterfactual_ledger``'s
        # own module docstring: "Rejected-Opportunity / Counterfactual
        # Decision Ledger v1"), so the fixture must genuinely reject, not
        # merely retain an ELIGIBLE decision whose sole reason
        # ("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY") maps to no ReasonFamily
        # at all and would silently contribute to zero cohorts.
        liquidity_state="INSUFFICIENT",
        liquidity_limitations=("FIXTURE_LIQUIDITY_LIMITATION",),
        required_hypothetical_capital="100",
        maximum_sensible_research_allocation="200",
        source_maximum_allocation_fraction="0.5",
        expected_holding_days=30,
        estimated_lockup_days=30,
        supporting_evidence_ids=(f"cor-evidence-{identity}",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(),
    )


def _seed_projection_and_eligibility(engine: Engine, *, identity: str) -> None:
    """Seed a genuine ``SharedOpportunityProjection`` + rejected
    ``EligibilityDecision`` through the real production path -- deliberately
    WITHOUT calling any ledger-sync service, so a later assertion that a
    ``CounterfactualDecision`` now exists is proof the recurring task's own
    executor branch performed that sync, not this fixture."""

    projection = _projection(identity)
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id=f"cor-set-{identity}", created_at=DECISION_DAY, projections=(projection,)
    )
    PostgresSharedOpportunityRepository(engine).save_opportunity_bundle(
        idempotency_key=f"cor-set-{identity}-bundle",
        opportunity_set=opportunity_set,
        projections=(projection,),
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )


def _due_task(cycle_id: str, task_type: OperatingTaskType) -> DueTask:
    return DueTask.create(
        task_type=task_type,
        disposition=DueDisposition.DUE,
        reasons=("fixture-due",),
        dependency_types=(),
        source_evidence_ids=(),
        cycle_id=cycle_id,
        network_behavior="fixture",
    )


def _register_plan(engine: Engine, *, plan_id: str) -> OperatingCyclePlan:
    repository = PostgresOperatingCycleRepository(engine)
    plan, _ = RegisterOperatingCyclePlan(repository).execute(
        plan_id=plan_id,
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW,
        idempotency_key=f"{plan_id}-key",
    )
    return plan


class _NoKalshiSource:
    """Zero-network Kalshi source that always reports the request budget as
    exhausted -- every fixture decision in this file is CRYPTO, so it is
    never genuinely called, but its presence proves no accidental network
    capability was wired in."""

    def read_market_status(self, **_: object) -> KalshiMarketRead:
        raise FollowUpSourceUnavailable("FIXTURE_KALSHI_NEVER_CALLED", request_count=0)


def _seed_observation_for_decision(engine: Engine, *, decision_id: str, identity: str) -> None:
    """Directly persist one genuine, ELIGIBLE CRYPTO follow-up observation
    for an already-ledger-synced decision -- bypasses the full
    ``SyncFollowUpObservations``/price-source machinery (already proven
    exhaustively in ``test_postgres_counterfactual_followup_recurring.py``)
    since this file's purpose is proving the *outcome-aggregation* wiring,
    not re-proving follow-up sync itself."""

    from decimal import Decimal

    from apex.domain.counterfactual_followup import (
        FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        FOLLOWUP_POLICY_V2,
        DecisionFollowUpObservation,
        FollowUpBaselineKind,
        FollowUpEvidenceDefectType,
        FollowUpEvidenceQualityAssessment,
        FollowUpEvidenceQualityVerdict,
        FollowUpHorizonKind,
        FollowUpObservationType,
    )
    from apex.domain.counterfactual_ledger import DecisionDisposition
    from apex.domain.research_evidence import EvidenceClass

    observation_id = f"cor-obs-{identity}"
    observation = DecisionFollowUpObservation(
        observation_id=observation_id,
        decision_id=decision_id,
        decision_canonical_hash="b" * 64,
        venue=SharedVenue.CRYPTO,
        disposition=DecisionDisposition.NOT_ADVANCED,
        policy_version=FOLLOWUP_POLICY_V2,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        horizon_days=1,
        decision_evaluated_at=NOW,
        baseline_time=NOW,
        target_time=NOW,
        observation_type=FollowUpObservationType.CRYPTO_REFERENCE_PRICE_MOVEMENT,
        subject_instrument_id="BTC-USD",
        subject_product_or_market_id="BTC-USD",
        provenance=EvidenceClass.GENUINE_PUBLIC,
        baseline_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        target_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        evidence_eligibility_policy_version="CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1",
        observed_at=NOW,
        source_lineage=("batch-1",),
        data_limitations=(),
        reference_price_at_baseline=Decimal("100"),
        reference_price_at_target=Decimal("105"),
        subsequent_return=Decimal("0.05"),
        market_status_at_target=None,
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result=None,
        settlement_source=None,
        measurement_as_of=None,
        policy_anchor_at=None,
        decision_input_reference_price=None,
        decision_input_reference_bucket_start=None,
        decision_input_source_observation_id=None,
        decision_input_evidence_class=None,
        canonical_hash=f"{observation_id}-hash".ljust(64, "0")[:64],
        authority="NONE",
    )
    followup = PostgresCounterfactualFollowUpRepository(engine)
    followup.save_observation(observation=observation)
    followup.save_assessment(
        assessment=FollowUpEvidenceQualityAssessment(
            assessment_id=f"{observation_id}-assessment",
            observation_id=observation_id,
            observation_canonical_hash=observation.canonical_hash,
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
            baseline_evidence_class=observation.baseline_evidence_class,
            target_evidence_class=observation.target_evidence_class,
            verdict=FollowUpEvidenceQualityVerdict.ELIGIBLE,
            reason_code="TEST_REASON",
            evidence_defect_type=FollowUpEvidenceDefectType.NONE,
            assessed_at=NOW,
            canonical_hash=f"{observation_id}-assessment-hash".ljust(64, "0")[:64],
            authority="NONE",
        )
    )


def _executor(engine: Engine) -> ExistingCapabilityOperatingTaskExecutor:
    state_source = PostgresOperatingStateSource(engine, PostgresOperatingCycleRepository(engine))
    return ExistingCapabilityOperatingTaskExecutor(
        engine, state_source, followup_kalshi_source=_NoKalshiSource()
    )


# ---------------------------------------------------------------------------
# Phase 0: ledger + classification sync now automatic
# ---------------------------------------------------------------------------


def test_ledger_and_classification_sync_now_run_automatically_inside_the_recurring_task(
    postgres_engine: Engine,
) -> None:
    _seed_projection_and_eligibility(postgres_engine, identity="auto-sync")
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    assert ledger.all_decisions() == ()  # genuinely un-synced before the task runs

    plan = _register_plan(postgres_engine, plan_id="cor-plan-ledger-sync")
    executor = _executor(postgres_engine)
    result = executor.execute(
        task=_due_task("cycle-ledger-sync", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-ledger-sync",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert result.status.value in {"COMPLETED", "REUSED"}
    assert any(code.startswith("LEDGER_TOTAL_NEWLY_WRITTEN:") for code in result.warnings)
    assert "LEDGER_TOTAL_NEWLY_WRITTEN:0" not in result.warnings

    decisions = ledger.all_decisions()
    assert len(decisions) == 1
    classifications = ledger.classifications(classifier_version=CURRENT_CLASSIFIER_VERSION)
    assert len(classifications) == 1
    assert classifications[0].decision_id == decisions[0].decision_id
    assert any(code.startswith("CLASSIFICATION_NEWLY_WRITTEN:") for code in result.warnings)
    assert "CLASSIFICATION_NEWLY_WRITTEN:0" not in result.warnings


def test_ledger_and_classification_sync_replay_is_idempotent(postgres_engine: Engine) -> None:
    _seed_projection_and_eligibility(postgres_engine, identity="idempotent-sync")
    plan = _register_plan(postgres_engine, plan_id="cor-plan-idempotent-sync")
    executor = _executor(postgres_engine)
    task = _due_task("cycle-idempotent-sync", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH)

    executor.execute(
        task=task, cycle_id="cycle-idempotent-sync", plan=plan, as_of=NOW, executed_at=NOW
    )
    assert _row_count(postgres_engine, "counterfactual_decision") == 1
    assert _row_count(postgres_engine, "counterfactual_decision_classification") == 1

    second = executor.execute(
        task=task,
        cycle_id="cycle-idempotent-sync",
        plan=plan,
        as_of=NOW,
        executed_at=NOW + timedelta(minutes=5),
    )
    assert _row_count(postgres_engine, "counterfactual_decision") == 1
    assert _row_count(postgres_engine, "counterfactual_decision_classification") == 1
    assert "LEDGER_TOTAL_NEWLY_WRITTEN:0" in second.warnings
    assert "CLASSIFICATION_NEWLY_WRITTEN:0" in second.warnings


# ---------------------------------------------------------------------------
# Phase 5: COUNTERFACTUAL_OUTCOME_REFRESH
# ---------------------------------------------------------------------------


def test_outcome_refresh_task_persists_an_aggregate_over_currently_retained_evidence(
    postgres_engine: Engine,
) -> None:
    _seed_projection_and_eligibility(postgres_engine, identity="outcome-refresh")
    plan = _register_plan(postgres_engine, plan_id="cor-plan-outcome-refresh")
    executor = _executor(postgres_engine)

    # First sync the ledger/classification (COUNTERFACTUAL_FOLLOWUP_REFRESH's
    # own branch), matching real cycle ordering.
    executor.execute(
        task=_due_task("cycle-outcome-refresh", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-outcome-refresh",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 0
    decision_id = (
        PostgresCounterfactualLedgerRepository(postgres_engine).all_decisions()[0].decision_id
    )
    # A cohort cell is only formed from triples that genuinely occur in the
    # retained follow-up corpus (see domain.counterfactual_outcome's own
    # docstring: "cohorts are discovered from real data, not enumerated
    # from the type system") -- seed one real observation so this decision's
    # own (short-horizon, decision-time, V2) triple actually exists.
    _seed_observation_for_decision(
        postgres_engine, decision_id=decision_id, identity="outcome-refresh"
    )

    result = executor.execute(
        task=_due_task("cycle-outcome-refresh", OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH),
        cycle_id="cycle-outcome-refresh",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert result.status.value in {"COMPLETED", "REUSED"}
    assert result.network_requests == 0

    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    aggregates = outcome_repository.aggregates_for_run(
        "cycle-outcome-refresh-counterfactual-outcome"
    )
    assert len(aggregates) == 1
    aggregate = aggregates[0]
    assert aggregate.strategy_or_method_id == "COR_FIXTURE_METHOD_V1"
    # A single eligible observation is an honest INSUFFICIENT_SAMPLE cell
    # (n=1 << 30), not a crash and not a fabricated QUALIFIED result.
    assert (
        aggregate.qualification_state is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )
    assert aggregate.total_observations_considered == 1
    assert aggregate.mature_observation_count == 1
    assert aggregate.eligible_observation_count == 1
    assert aggregate.unavailable_observation_count == 0
    assert aggregate.contributing_decision_ids == (decision_id,)


def test_outcome_refresh_replay_within_the_same_cycle_is_idempotent(
    postgres_engine: Engine,
) -> None:
    _seed_projection_and_eligibility(postgres_engine, identity="outcome-replay")
    plan = _register_plan(postgres_engine, plan_id="cor-plan-outcome-replay")
    executor = _executor(postgres_engine)
    executor.execute(
        task=_due_task("cycle-outcome-replay", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-outcome-replay",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    decision_id = (
        PostgresCounterfactualLedgerRepository(postgres_engine).all_decisions()[0].decision_id
    )
    _seed_observation_for_decision(
        postgres_engine, decision_id=decision_id, identity="outcome-replay"
    )
    task = _due_task("cycle-outcome-replay", OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH)

    first = executor.execute(
        task=task, cycle_id="cycle-outcome-replay", plan=plan, as_of=NOW, executed_at=NOW
    )
    assert first.status.value == "COMPLETED"
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1

    second = executor.execute(
        task=task,
        cycle_id="cycle-outcome-replay",
        plan=plan,
        as_of=NOW,
        executed_at=NOW + timedelta(minutes=5),
    )
    assert second.status.value == "REUSED"
    assert "COUNTERFACTUAL_OUTCOME_REUSED_IDEMPOTENT:1" in second.warnings
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1


def test_outcome_refresh_second_dispatch_with_matured_evidence_does_not_fail_the_task(
    postgres_engine: Engine,
) -> None:
    """Reproduces the exact 2026-09-02 production incident end-to-end
    through the real recurring-task executor (Counterfactual Outcome
    Refresh Recovery v1). Root cause: an unrelated crash inside the same
    dispatch's V2 pass (since independently resolved) left no terminal
    task result after V1 had already persisted a cohort aggregate; a
    second same-day dispatch of the identical run_id then found this
    task's own same-cycle dependency (COUNTERFACTUAL_FOLLOWUP_REFRESH,
    which uses intraday continuation) had matured more evidence for that
    already-persisted cohort in between -- an unguarded ``save_aggregate``
    identity conflict then failed the whole task. This proves the fixed
    behavior: the second dispatch completes (never FAILED), the conflict
    is visible as a bounded warning, and the first-persisted row is
    neither duplicated nor silently mutated."""

    _seed_projection_and_eligibility(postgres_engine, identity="cor-drift-1")
    _seed_projection_and_eligibility(postgres_engine, identity="cor-drift-2")
    plan = _register_plan(postgres_engine, plan_id="cor-plan-drift")
    executor = _executor(postgres_engine)
    executor.execute(
        task=_due_task("cycle-drift", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-drift",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    decision_ids = sorted(
        decision.decision_id
        for decision in PostgresCounterfactualLedgerRepository(postgres_engine).all_decisions()
    )
    assert len(decision_ids) == 2

    # Only the first decision has matured follow-up evidence before the
    # first COUNTERFACTUAL_OUTCOME_REFRESH dispatch this cycle.
    _seed_observation_for_decision(
        postgres_engine, decision_id=decision_ids[0], identity="cor-drift-a"
    )

    task = _due_task("cycle-drift", OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH)
    first = executor.execute(
        task=task, cycle_id="cycle-drift", plan=plan, as_of=NOW, executed_at=NOW
    )
    assert first.status.value == "COMPLETED"
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1
    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    aggregates_after_first = outcome_repository.aggregates_for_run(
        "cycle-drift-counterfactual-outcome"
    )
    assert len(aggregates_after_first) == 1
    aggregate_before = aggregates_after_first[0]
    assert aggregate_before.total_observations_considered == 2
    assert aggregate_before.mature_observation_count == 1

    # The second decision matures in between -- standing in for
    # COUNTERFACTUAL_FOLLOWUP_REFRESH's own real intraday continuation
    # (bypassed here exactly like this file's other tests bypass the full
    # sync machinery; see ``_seed_observation_for_decision``'s docstring).
    _seed_observation_for_decision(
        postgres_engine, decision_id=decision_ids[1], identity="cor-drift-b"
    )

    second = executor.execute(
        task=task,
        cycle_id="cycle-drift",
        plan=plan,
        as_of=NOW,
        executed_at=NOW + timedelta(minutes=5),
    )
    assert second.status.value in {"COMPLETED", "REUSED"}  # never FAILED
    assert "COUNTERFACTUAL_OUTCOME_STALE_IDENTITY_SUPERSEDED:1" in second.warnings
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1  # no duplicate

    # The already-persisted row is untouched -- append-only, never mutated
    # or silently "upgraded" to reflect the newly-matured evidence.
    unchanged = outcome_repository.aggregate(aggregate_before.aggregate_id)
    assert unchanged == aggregate_before

    # Deterministic on repeated replay: a third dispatch against the same
    # (still-grown) evidence isolates again, identically.
    third = executor.execute(
        task=task,
        cycle_id="cycle-drift",
        plan=plan,
        as_of=NOW,
        executed_at=NOW + timedelta(minutes=10),
    )
    assert third.status.value in {"COMPLETED", "REUSED"}
    assert "COUNTERFACTUAL_OUTCOME_STALE_IDENTITY_SUPERSEDED:1" in third.warnings
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1


def test_outcome_refresh_on_a_new_cycle_day_adds_a_fresh_row_without_touching_the_first(
    postgres_engine: Engine,
) -> None:
    _seed_projection_and_eligibility(postgres_engine, identity="outcome-new-day")
    plan = _register_plan(postgres_engine, plan_id="cor-plan-outcome-new-day")
    executor = _executor(postgres_engine)
    executor.execute(
        task=_due_task("cycle-day1", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-day1",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    decision_id = (
        PostgresCounterfactualLedgerRepository(postgres_engine).all_decisions()[0].decision_id
    )
    _seed_observation_for_decision(
        postgres_engine, decision_id=decision_id, identity="outcome-new-day"
    )
    executor.execute(
        task=_due_task("cycle-day1", OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH),
        cycle_id="cycle-day1",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1
    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    day_one_aggregate = outcome_repository.aggregates_for_run("cycle-day1-counterfactual-outcome")[
        0
    ]

    next_day = NOW + timedelta(days=1)
    executor.execute(
        task=_due_task("cycle-day2", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-day2",
        plan=plan,
        as_of=next_day,
        executed_at=next_day,
    )
    executor.execute(
        task=_due_task("cycle-day2", OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH),
        cycle_id="cycle-day2",
        plan=plan,
        as_of=next_day,
        executed_at=next_day,
    )
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 2
    # The day-1 row is untouched -- append-only, never mutated in place.
    unchanged = outcome_repository.aggregate(day_one_aggregate.aggregate_id)
    assert unchanged == day_one_aggregate
