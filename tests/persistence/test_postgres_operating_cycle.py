from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.coinbase_universe_public import (
    MAX_CANDLES_PER_REQUEST,
    FixtureCoinbaseUniverseSource,
)
from apex.adapters.crypto_market_pulse_postgres import PostgresCryptoMarketPulseRepository
from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
from apex.adapters.investment_mandate_postgres import PostgresInvestmentMandateRepository
from apex.adapters.kalshi_operating_postgres import PostgresKalshiOperatingRepository
from apex.adapters.operating_cycle_postgres import (
    PostgresOperatingCycleRepository,
    PostgresOperatingStateSource,
)
from apex.adapters.operating_cycle_tasks import (
    GENUINE_CRYPTO_PAPER_ACCOUNT_ID,
    GENUINE_KALSHI_PAPER_ACCOUNT_ID,
    ExistingCapabilityOperatingTaskExecutor,
)
from apex.adapters.options_quote_intelligence_postgres import (
    PostgresOptionsQuoteIntelligenceRepository,
)
from apex.adapters.options_research_capture import resolve_options_capture_due_decision
from apex.adapters.options_research_operations_postgres import (
    PostgresOptionsResearchOperationsRepository,
)
from apex.adapters.portfolio_state_postgres import PostgresPortfolioSnapshotRepository
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.application.crypto_paper import CreateCryptoPaperAccount
from apex.application.crypto_universe import (
    CaptureCryptoUniverseObservations,
    CapturePublicCryptoProducts,
    MaterializeCryptoUniverse,
)
from apex.application.investment_mandate import CreateInvestmentMandate
from apex.application.kalshi_operating import CreateKalshiPaperAccount
from apex.application.operating_cycle import (
    ExecuteOperatingCycle,
    PlanOperatingDueWork,
    RegisterOperatingCyclePlan,
    SetOperatingCycleControl,
    SetPaperActivation,
)
from apex.cli import _deliver_operating_cycle_alerts
from apex.domain.investment_mandate import DEFAULT_MANDATE_LINEAGE_ID, AutonomyRequirement
from apex.domain.operating_cycle import (
    DueDisposition,
    DueTask,
    DueWorkPlan,
    OperatingControlState,
    OperatingCycleError,
    OperatingCycleRun,
    OperatingTaskStatus,
    OperatingTaskType,
    PaperActivationState,
    TaskExecution,
    effective_paper_activation,
)
from apex.domain.options_intelligence import CapabilityState, OptionProviderCapabilities
from apex.domain.options_quote_intelligence import (
    DEFAULT_OPTIONS_COHORT_SYMBOLS,
    OptionChainCaptureBounds,
    OptionChainCapturePlan,
    OptionsQuoteProviderCapabilitySnapshot,
)
from apex.domain.options_research_operations import (
    OptionsCaptureWindowPolicy,
    OptionsForwardEvidenceCheckpoint,
)
from apex.domain.portfolio_state import build_portfolio_snapshot
from apex.domain.research_cohort import CohortRole, CohortSector, ResolvedCohortMember
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import (
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
)
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)
# 2026-08-10 is a genuine, already-verified ordinary XNYS session (real
# calendar fact, not fabricated -- see tests/persistence/
# test_postgres_options_research_operations.py's identical constants and
# AGENTS.md's "Fresh Options Evidence Operations..." milestone): close
# 20:00:00Z, capture window [20:00:00Z, 20:15:00Z].
OPTIONS_SESSION_DAY = datetime(2026, 8, 10, tzinfo=UTC)
OPTIONS_WINDOW_OPEN = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def test_plan_is_immutable_idempotent_authority_free_and_blocks_downgrade(
    postgres_engine: Engine,
) -> None:
    repository = PostgresOperatingCycleRepository(postgres_engine)
    service = RegisterOperatingCyclePlan(repository)
    plan, replay = service.execute(
        plan_id="postgres-operating-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW,
        idempotency_key="postgres-operating-plan-key",
    )
    repeated, repeated_replay = service.execute(
        plan_id=plan.plan_id,
        crypto_pulse_plan_id=plan.crypto_pulse_plan_id,
        actor=plan.actor,
        created_at=NOW,
        idempotency_key="postgres-operating-plan-key",
    )
    assert not replay
    assert repeated_replay
    assert repeated == plan
    assert plan.authority == plan.capital_authority == plan.broker_authority == "NONE"

    with pytest.raises(OperatingCycleError, match="OPERATING_PLAN_IDENTITY_CONFLICT"):
        service.execute(
            plan_id=plan.plan_id,
            crypto_pulse_plan_id=plan.crypto_pulse_plan_id,
            actor=plan.actor,
            created_at=NOW,
            idempotency_key="conflicting-key",
        )
    with (
        postgres_engine.begin() as connection,
        pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"),
    ):
        connection.execute(
            sa.text(
                "UPDATE apex_operating_cycle_plan SET enabled = false WHERE plan_id = :plan_id"
            ),
            {"plan_id": plan.plan_id},
        )

    postgres_engine.dispose()
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="cannot downgrade 0060"):
            command.downgrade(_config(connection), "0059_total_market_context")
        connection.rollback()
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0060_recurring_operating_cycle"
        )


def test_paper_activation_is_governed_auditable_replayable_and_scoped_to_exact_plan(
    postgres_engine: Engine,
) -> None:
    repository = PostgresOperatingCycleRepository(postgres_engine)
    # other_plan is registered strictly earlier so workspace()'s "most
    # recently retained plan" view is unambiguously `plan`, not a tiebreak.
    other_plan, _ = RegisterOperatingCyclePlan(repository).execute(
        plan_id="postgres-paper-activation-other-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW - timedelta(days=1),
        idempotency_key="postgres-paper-activation-other-plan-key",
    )
    plan, _ = RegisterOperatingCyclePlan(repository).execute(
        plan_id="postgres-paper-activation-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW,
        idempotency_key="postgres-paper-activation-plan-key",
    )
    service = SetPaperActivation(repository)

    with pytest.raises(OperatingCycleError, match="OPERATING_PLAN_NOT_FOUND"):
        service.execute(
            event_id="paper-activate-unknown",
            plan_id="plan-does-not-exist",
            state=PaperActivationState.ENABLED,
            reason="must never be accepted",
            actor="persistence-test",
            occurred_at=NOW,
            idempotency_key="paper-activate-unknown-key",
        )

    event, replay = service.execute(
        event_id="postgres-paper-activate-v1",
        plan_id=plan.plan_id,
        state=PaperActivationState.ENABLED,
        reason="begin bounded genuine PAPER evidence accumulation",
        actor="persistence-test",
        occurred_at=NOW,
        idempotency_key="postgres-paper-activate-key",
    )
    assert not replay
    assert event.ordinal == 1
    assert event.authority == "NONE"

    # Exact replay (same event_id, same idempotency key, unchanged content) is
    # accepted idempotently and never creates a second row.
    replayed_event, replayed = service.execute(
        event_id="postgres-paper-activate-v1",
        plan_id=plan.plan_id,
        state=PaperActivationState.ENABLED,
        reason="begin bounded genuine PAPER evidence accumulation",
        actor="persistence-test",
        occurred_at=NOW,
        idempotency_key="postgres-paper-activate-key",
    )
    assert replayed
    assert replayed_event == event
    assert len(repository.paper_activation_events(plan.plan_id)) == 1

    # A conflicting idempotency key against the same event_id fails closed.
    with pytest.raises(OperatingCycleError, match="OPERATING_PAPER_ACTIVATION_IDENTITY_CONFLICT"):
        service.execute(
            event_id="postgres-paper-activate-v1",
            plan_id=plan.plan_id,
            state=PaperActivationState.ENABLED,
            reason="begin bounded genuine PAPER evidence accumulation",
            actor="persistence-test",
            occurred_at=NOW,
            idempotency_key="conflicting-key",
        )

    assert effective_paper_activation(repository.paper_activation_events(plan.plan_id)) is True
    # A different retained plan must never observe this plan's activation.
    assert repository.paper_activation_events(other_plan.plan_id) == ()
    assert effective_paper_activation(repository.paper_activation_events(other_plan.plan_id)) is (
        False
    )

    workspace = repository.workspace()
    assert workspace["plan"] == plan
    assert workspace["paper_activation"] == event
    assert workspace["paper_eligible"] is True

    deactivation, replay = service.execute(
        event_id="postgres-paper-deactivate-v1",
        plan_id=plan.plan_id,
        state=PaperActivationState.DISABLED,
        reason="pause pending operator review",
        actor="persistence-test",
        occurred_at=NOW,
        idempotency_key="postgres-paper-deactivate-key",
    )
    assert not replay
    assert deactivation.ordinal == 2
    assert effective_paper_activation(repository.paper_activation_events(plan.plan_id)) is False

    with (
        postgres_engine.begin() as connection,
        pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"),
    ):
        connection.execute(
            sa.text(
                "UPDATE apex_operating_paper_activation_event SET state = 'DISABLED' "
                "WHERE event_id = :event_id"
            ),
            {"event_id": event.event_id},
        )

    postgres_engine.dispose()
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="cannot downgrade 0074"):
            command.downgrade(_config(connection), "0073_live_preflight")
        connection.rollback()


def test_operating_control_is_replayable_conflict_safe_and_ordinal_correct_on_postgres(
    postgres_engine: Engine,
) -> None:
    """SetOperatingCycleControl retained the same recomputed-ordinal defect
    that was fixed in SetPaperActivation (see the sibling paper-activation
    test above): it always derived a new event's ordinal from
    ``len(events) + 1``, so an exact replay of an already-retained event_id
    recomputed a different ordinal, changed the replay's canonical_hash, and
    false-positived as ``OPERATING_CONTROL_IDENTITY_CONFLICT``.  This proves
    the fix round-trips through genuine PostgreSQL: exact replay returns the
    retained event with its original ordinal and appends no duplicate row,
    conflicting reuse of the same event_id still fails closed, and later
    distinct pause/resume events still receive correct, monotonic ordinals."""

    repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(repository).execute(
        plan_id="postgres-operating-control-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW,
        idempotency_key="postgres-operating-control-plan-key",
    )
    service = SetOperatingCycleControl(repository)

    with pytest.raises(OperatingCycleError, match="OPERATING_PLAN_NOT_FOUND"):
        service.execute(
            event_id="control-unknown-plan",
            plan_id="plan-does-not-exist",
            state=OperatingControlState.PAUSED,
            reason="must never be accepted",
            actor="persistence-test",
            occurred_at=NOW,
            idempotency_key="control-unknown-plan-key",
        )

    paused, replay = service.execute(
        event_id="postgres-control-pause-v1",
        plan_id=plan.plan_id,
        state=OperatingControlState.PAUSED,
        reason="operator pause",
        actor="persistence-test",
        occurred_at=NOW,
        idempotency_key="postgres-control-pause-key",
    )
    assert not replay
    assert paused.ordinal == 1
    assert paused.authority == "NONE"

    # Exact replay (same event_id, same idempotency key, unchanged content) is
    # accepted idempotently, reuses the original ordinal, and never creates a
    # second row -- the defect this test guards against.
    replayed_paused, replayed = service.execute(
        event_id="postgres-control-pause-v1",
        plan_id=plan.plan_id,
        state=OperatingControlState.PAUSED,
        reason="operator pause",
        actor="persistence-test",
        occurred_at=NOW,
        idempotency_key="postgres-control-pause-key",
    )
    assert replayed
    assert replayed_paused == paused
    assert replayed_paused.ordinal == 1
    assert len(repository.control_events(plan.plan_id)) == 1

    # A conflicting idempotency key against the same event_id fails closed.
    with pytest.raises(OperatingCycleError, match="OPERATING_CONTROL_IDENTITY_CONFLICT"):
        service.execute(
            event_id="postgres-control-pause-v1",
            plan_id=plan.plan_id,
            state=OperatingControlState.PAUSED,
            reason="operator pause",
            actor="persistence-test",
            occurred_at=NOW,
            idempotency_key="conflicting-key",
        )

    # A later, genuinely distinct resume event still receives the correct next
    # ordinal after the replay above.
    resumed, replay = service.execute(
        event_id="postgres-control-resume-v1",
        plan_id=plan.plan_id,
        state=OperatingControlState.ENABLED,
        reason="operator resume",
        actor="persistence-test",
        occurred_at=NOW + timedelta(minutes=1),
        idempotency_key="postgres-control-resume-key",
    )
    assert not replay
    assert resumed.ordinal == 2
    events = repository.control_events(plan.plan_id)
    assert len(events) == 2
    assert events[-1].state is OperatingControlState.ENABLED

    with (
        postgres_engine.begin() as connection,
        pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"),
    ):
        connection.execute(
            sa.text(
                "UPDATE apex_operating_control_event SET state = 'PAUSED' "
                "WHERE event_id = :event_id"
            ),
            {"event_id": resumed.event_id},
        )


def _save_minimal_cycle(
    repository: PostgresOperatingCycleRepository, *, plan_id: str, cycle_id: str
) -> OperatingCycleRun:
    due_work = DueWorkPlan(
        due_work_id=f"{cycle_id}-due-work-v1",
        plan_id=plan_id,
        cycle_id=cycle_id,
        as_of=NOW,
        retained_state_hash="r" * 64,
        tasks=(),
        dependency_order=(),
        canonical_hash=content_digest((cycle_id, "fixture-due-work")),
    )
    cycle = OperatingCycleRun.create(
        cycle_id=cycle_id,
        plan_id=plan_id,
        as_of=NOW,
        due_work=due_work,
        prior_cycle_id=None,
        created_at=NOW,
    )
    repository.save_cycle(idempotency_key=f"{cycle_id}-key", cycle=cycle, due_work=due_work)
    return cycle


def test_daily_checkpoint_uses_the_exact_plan_and_cycle_never_another_retained_pair(
    postgres_engine: Engine,
) -> None:
    """The daily checkpoint composition must key strictly off the caller's
    exact plan_id/cycle_id -- never an implicit "latest plan" or "latest
    cycle" -- so a second, unrelated retained plan/cycle pair in the same
    database can never leak into it, and a genuinely mismatched pair fails
    closed instead of silently substituting the wrong plan's evidence. An
    empty PAPER performance section on this fixture is valid and explained,
    never left as a silent absence."""

    from apex.cli import _build_operating_daily_checkpoint

    repository = PostgresOperatingCycleRepository(postgres_engine)
    plan_a, _ = RegisterOperatingCyclePlan(repository).execute(
        plan_id="postgres-checkpoint-plan-a",
        crypto_pulse_plan_id="retained-pulse-plan-a",
        actor="persistence-test",
        created_at=NOW - timedelta(days=1),
        idempotency_key="postgres-checkpoint-plan-a-key",
    )
    plan_b, _ = RegisterOperatingCyclePlan(repository).execute(
        plan_id="postgres-checkpoint-plan-b",
        crypto_pulse_plan_id="retained-pulse-plan-b",
        actor="persistence-test",
        created_at=NOW,
        idempotency_key="postgres-checkpoint-plan-b-key",
    )
    _save_minimal_cycle(repository, plan_id=plan_a.plan_id, cycle_id="postgres-checkpoint-cycle-a")
    _save_minimal_cycle(repository, plan_id=plan_b.plan_id, cycle_id="postgres-checkpoint-cycle-b")

    checkpoint = _build_operating_daily_checkpoint(
        engine=postgres_engine,
        repository=repository,
        plan_id=plan_a.plan_id,
        cycle_id="postgres-checkpoint-cycle-a",
        runner_id="postgres-checkpoint-runner",
    )
    assert checkpoint["operating_plan_id"] == plan_a.plan_id
    assert checkpoint["cycle_id"] == "postgres-checkpoint-cycle-a"
    assert checkpoint["crypto_pulse_plan_id"] == "retained-pulse-plan-a"
    assert checkpoint["eligible_count"] == 0
    assert checkpoint["rejected_count"] == 0
    assert checkpoint["opportunities_by_venue_and_strategy"] == ()
    assert checkpoint["completed_genuine_paper_sample_count"] == 0
    assert checkpoint["empty_performance_explanation"] is not None
    assert checkpoint["thirty_sample_live_readiness_gate"]["progress"] == "0/30"
    assert checkpoint["authority"] == "NONE"
    assert checkpoint["live_authority"] == "NONE"

    # plan_a's own cycle_id was never retained under plan_b's namespace; asking
    # for plan_b's identity with plan_a's cycle_id must fail closed rather than
    # silently substitute plan_b's evidence.
    with pytest.raises(ValueError, match="OPERATING_CYCLE_NOT_FOUND_FOR_EXACT_PLAN"):
        _build_operating_daily_checkpoint(
            engine=postgres_engine,
            repository=repository,
            plan_id=plan_b.plan_id,
            cycle_id="postgres-checkpoint-cycle-a",
            runner_id="postgres-checkpoint-runner",
        )


# --------------------------------------------------------------------------
# Recurring Operating Cycle Integration v2: the genuine evaluated_at window
# fix, and restart/idempotency proofs for the two new recurring tasks.
# --------------------------------------------------------------------------


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _due_task(task_type: OperatingTaskType, *, cycle_id: str) -> DueTask:
    return DueTask.create(
        task_type=task_type,
        disposition=DueDisposition.DUE,
        reasons=("fixture-due",),
        dependency_types=(),
        source_evidence_ids=(),
        cycle_id=cycle_id,
        network_behavior="RETAINED_EVIDENCE_ONLY_ZERO_PROVIDER_REQUESTS",
    )


def _options_plan() -> OptionChainCapturePlan:
    members = tuple(
        ResolvedCohortMember(
            stable_security_id=f"security-{symbol.lower()}-stable",
            symbol=symbol,
            sector=CohortSector.BROAD_MARKET,
            roles=(CohortRole.BROAD_INDEX_ETF,),
            asset_class="ETF",
        )
        for symbol in DEFAULT_OPTIONS_COHORT_SYMBOLS
    )
    return OptionChainCapturePlan.create(
        cohort_policy_id="research_cohort_policy_test",
        cohort_policy_hash="0" * 64,
        members=members,
        unresolved_symbols=(),
        bounds=OptionChainCaptureBounds.create(),
        created_at=OPTIONS_SESSION_DAY,
    )


def _options_capability(*, credentials_available: bool) -> OptionsQuoteProviderCapabilitySnapshot:
    capability = OptionProviderCapabilities.create(
        provider="ALPACA_OPTIONS_MARKET_DATA",
        contract_discovery=CapabilityState.UNKNOWN,
        quotes=CapabilityState.UNKNOWN,
        trades=CapabilityState.UNKNOWN,
        snapshots=CapabilityState.UNKNOWN,
        implied_volatility=CapabilityState.UNKNOWN,
        greeks=CapabilityState.UNKNOWN,
        open_interest=CapabilityState.UNKNOWN,
        volume=CapabilityState.UNKNOWN,
        limitations=()
        if credentials_available
        else ("ALPACA_CREDENTIALS_ABSENT_ZERO_REQUESTS_MADE",),
    )
    return OptionsQuoteProviderCapabilitySnapshot.create(
        checked_at=OPTIONS_SESSION_DAY,
        credentials_available=credentials_available,
        requests_made=0,
        capability=capability,
        unavailable_reason=None if credentials_available else "ALPACA_CREDENTIALS_ABSENT",
    )


def _seed_prior_session_fully_captured(engine: Engine, *, plan: OptionChainCapturePlan) -> None:
    """``determine_options_capture_due_state`` reports the single most
    relevant session -- current, else most recently *missed*, else next
    upcoming -- so a genuinely fresh test day still shows the immediately
    preceding session (2026-08-07, the last XNYS session before 2026-08-10)
    as ``MISSED_CAPTURE_WINDOW`` unless it is already retained as captured,
    which would otherwise shadow the target session's own ``NOT_DUE`` /
    ``BEFORE_CAPTURE_WINDOW`` reason. This directly mirrors the real
    2026-08-07 gap already disclosed and accepted in AGENTS.md's "Fresh
    Options Evidence Operations" milestone -- not a new assumption. A
    synthetic, clearly test-only checkpoint (never written to the genuine
    dev database) closes exactly that gap so these tests can observe the
    target session's own intraday transition in isolation."""

    window_policy = OptionsCaptureWindowPolicy.create()
    prior_session_id = "2026-08-07"
    window_open = datetime(2026, 8, 7, 20, 0, tzinfo=UTC)
    window_close = datetime(2026, 8, 7, 20, 15, tzinfo=UTC)
    symbols = tuple(sorted(member.symbol for member in plan.members))
    checkpoint = OptionsForwardEvidenceCheckpoint.create(
        plan_id=plan.plan_id,
        session_id=prior_session_id,
        capture_window_policy_id=window_policy.policy_id,
        window_open_at=window_open,
        window_close_at=window_close,
        provider="ALPACA_OPTIONS_MARKET_DATA",
        feed=plan.feed,
        feed_entitlement_state="INDICATIVE_ONLY_OPRA_UNCONFIRMED",
        underlying_symbols_captured=symbols,
        underlyings_requested_count=len(symbols),
        underlyings_captured_count=len(symbols),
        fresh_snapshot_ids=(),
        stale_snapshot_count=0,
        quote_quality_counts=(),
        chain_coverage_quality_counts=(),
        cap_constrained_underlyings=(),
        structure_snapshot_ids=(),
        structures_evaluated=0,
        structures_retained=0,
        fee_policy_id="fixture-fee-policy",
        fee_evidence_status="FEE_EVIDENCE_INCOMPLETE",
        opra_entitlement_state="INDICATIVE_ONLY_OPRA_UNCONFIRMED",
        open_interest_limitation="OPEN_INTEREST_UNAVAILABLE",
        iv_history_minimum_required=20,
        iv_history_ready_underlying_count=0,
        forward_history_underlying_count=0,
        created_at=window_close,
    )
    PostgresOptionsResearchOperationsRepository(engine).save_checkpoint(checkpoint)


def test_evaluated_at_reaches_genuine_options_due_window_while_as_of_stays_day_scoped(
    postgres_engine: Engine,
) -> None:
    """The concrete Part A fix, proved against real PostgreSQL and a real
    XNYS session: the supervised worker pins ``as_of`` to UTC midnight for
    day-scoped cycle identity, but the options capture due-check must see
    true wall-clock time or it can *never* observe DUE -- midnight is always
    before today's own window and always after any prior day's, so the old
    (pre-fix) behavior of feeding only ``as_of`` through would report the
    most recently missed session forever, no matter how often or when the
    worker actually polls."""

    quote_repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    checkpoint_repository = PostgresOptionsResearchOperationsRepository(postgres_engine)
    quote_repository.save_plan(_options_plan())
    quote_repository.save_capability(_options_capability(credentials_available=True))

    midnight = OPTIONS_SESSION_DAY
    genuinely_within_window = OPTIONS_WINDOW_OPEN + timedelta(minutes=5)

    state_source = PostgresOperatingStateSource(
        postgres_engine, PostgresOperatingCycleRepository(postgres_engine)
    )
    fixed_state = state_source.snapshot(as_of=midnight, evaluated_at=genuinely_within_window)
    assert fixed_state.options_capture_due_status == "DUE"
    # Day-scoped cycle identity is untouched by the fix.
    assert fixed_state.captured_at == midnight

    _plan_obj, _policy, _sessions, decision_at_window = resolve_options_capture_due_decision(
        quote_repository=quote_repository,
        checkpoint_repository=checkpoint_repository,
        as_of=genuinely_within_window,
    )
    assert decision_at_window.status.value == "DUE"

    # The old (pre-fix) shape: only ``as_of`` (midnight) ever reaches the
    # due-check -- it can never observe DUE for any session, only NOT_DUE or
    # a permanently-missed prior session.
    old_behavior_state = state_source.snapshot(as_of=midnight)
    assert old_behavior_state.options_capture_due_status != "DUE"
    _plan_obj, _policy, _sessions, decision_at_midnight = resolve_options_capture_due_decision(
        quote_repository=quote_repository,
        checkpoint_repository=checkpoint_repository,
        as_of=midnight,
    )
    assert decision_at_midnight.status.value in {"NOT_DUE", "MISSED_CAPTURE_WINDOW"}


def test_executor_reaches_genuine_options_due_window_using_executed_at_not_as_of(
    postgres_engine: Engine,
) -> None:
    """Regression for a genuine bug found while diagnosing Part 2 (Recurring
    Operating Health v1): ``plan_due_work`` correctly resolves ``DUE`` for
    the options task using ``evaluated_at``/``executed_at`` (the v2 fix).
    But ``ExistingCapabilityOperatingTaskExecutor.execute`` -- reached only
    once the planner already says DUE -- independently re-derives its own
    due decision as a defense-in-depth check. Before the fix, that inner
    check was fed the *day-scoped midnight* ``as_of`` instead of the true
    wall-clock ``executed_at``; since midnight is always before today's own
    window, the inner check would always disagree with a genuinely DUE
    planning decision and raise ``OPERATING_OPTIONS_CAPTURE_NOT_GENUINELY_
    DUE`` -- meaning the options task could never actually reach
    ``COMPLETED`` through the real worker, even when a poll happened to land
    exactly inside a valid capture window. This was never caught by the v2
    milestone's own acceptance run because that day's window had already
    closed before the worker ever polled, so the DUE branch of the executor
    was never actually exercised for real -- see AGENTS.md's "Recurring
    Operating Health & Unattended Evidence Closure v1" section."""

    quote_repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    quote_repository.save_plan(_options_plan())
    quote_repository.save_capability(_options_capability(credentials_available=True))

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-options-executor-as-of-bug-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=OPTIONS_SESSION_DAY,
        idempotency_key="postgres-options-executor-as-of-bug-plan-key",
    )
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)

    midnight = OPTIONS_SESSION_DAY
    genuinely_within_window = OPTIONS_WINDOW_OPEN + timedelta(minutes=5)
    # Exactly what the real planner produces at this instant: DUE.
    fresh_state = state_source.snapshot(as_of=midnight, evaluated_at=genuinely_within_window)
    assert fresh_state.options_capture_due_status == "DUE"
    task = _due_task(
        OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE, cycle_id="cycle-options-as-of-bug"
    )

    # The planner says DUE (``as_of`` stays day-scoped midnight throughout --
    # only ``executed_at`` carries the real capture-window instant); the
    # executor must genuinely execute rather than raise.
    execution = executor.execute(
        task=task,
        cycle_id="cycle-options-as-of-bug",
        plan=plan,
        as_of=midnight,
        executed_at=genuinely_within_window,
    )
    assert execution.status.value == "COMPLETED"
    # No real Alpaca credentials are present in this test environment (zero
    # network requests possible either way), so every cohort member is
    # honestly ``BLOCKED_CREDENTIALS_ABSENT`` -- the fix under test is that
    # the executor genuinely *reaches* and completes the capture attempt at
    # all, not that it captures real quotes.
    assert execution.network_requests == 0
    assert execution.output_identities


class _RealOptionsExecutorWrapper:
    """Delegates ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE`` to the real,
    Postgres-backed ``ExistingCapabilityOperatingTaskExecutor`` (genuine due
    re-check, genuine -- if credential-absent -- capture attempt); every
    other task type completes trivially. Proves the real options
    orchestration through ``ExecuteOperatingCycle`` without also standing up
    crypto/Kalshi/total-market fixtures unrelated to what these tests are
    proving (Recurring Operating Health & Unattended Evidence Closure v1)."""

    def __init__(self, real: ExistingCapabilityOperatingTaskExecutor) -> None:
        self._real = real
        self.calls: list[tuple[str, datetime]] = []

    def execute(
        self,
        *,
        task: DueTask,
        cycle_id: str,
        plan: object,
        as_of: datetime,
        executed_at: datetime,
    ) -> TaskExecution:
        self.calls.append((task.task_type.value, executed_at))
        if task.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE:
            return self._real.execute(
                task=task,  # type: ignore[arg-type]
                cycle_id=cycle_id,
                plan=plan,  # type: ignore[arg-type]
                as_of=as_of,
                executed_at=executed_at,
            )
        return TaskExecution(
            OperatingTaskStatus.COMPLETED, (f"{task.task_type.value}-output",), (), True, 0
        )


def test_execute_operating_cycle_transitions_options_capture_not_due_to_due_same_day(
    postgres_engine: Engine,
) -> None:
    """The full real planning/execution path (not only the due-state
    function) genuinely reevaluates and executes the options-capture task
    later the same UTC day, through ``ExecuteOperatingCycle`` -- proving
    the exact NOT_DUE -> DUE transition, exactly-once execution, and a
    genuinely idempotent later replay that Part 2 of "Recurring Operating
    Health & Unattended Evidence Closure v1" requires."""

    quote_repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    options_plan = _options_plan()
    quote_repository.save_plan(options_plan)
    quote_repository.save_capability(_options_capability(credentials_available=True))
    _seed_prior_session_fully_captured(postgres_engine, plan=options_plan)
    checkpoint_repository = PostgresOptionsResearchOperationsRepository(postgres_engine)

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-options-intraday-cycle-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=OPTIONS_SESSION_DAY,
        idempotency_key="postgres-options-intraday-cycle-plan-key",
    )
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    real_executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    wrapper = _RealOptionsExecutorWrapper(real_executor)

    midnight = OPTIONS_SESSION_DAY
    cycle_id = "postgres-options-intraday-cycle"

    morning = ExecuteOperatingCycle(plan_repository, state_source, wrapper).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=midnight,
        executed_at=midnight + timedelta(hours=9),
        idempotency_key=f"{cycle_id}-key",
    )
    assert "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" not in {
        item.task_type.value for item in morning.tasks
    }
    assert not any(call[0] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" for call in wrapper.calls)
    # Exactly the one synthetic prior-session checkpoint seeded above --
    # nothing genuine has been captured yet this call.
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 1

    # A genuinely new ``ExecuteOperatingCycle`` (and a fresh executor/state
    # source pair) simulates a supervised-worker restart between polls.
    later_wrapper = _RealOptionsExecutorWrapper(
        ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    )
    within_window = ExecuteOperatingCycle(plan_repository, state_source, later_wrapper).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=midnight,
        executed_at=OPTIONS_WINDOW_OPEN + timedelta(minutes=5),
        idempotency_key=f"{cycle_id}-key",
    )
    options_result = next(
        item
        for item in within_window.tasks
        if item.task_type.value == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
    )
    assert options_result.status.value == "COMPLETED"
    assert not within_window.idempotent_replay
    assert [c for c in later_wrapper.calls if c[0] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"]
    # The seeded prior-session checkpoint plus exactly one genuinely new one
    # from this call -- never more, regardless of how many polls happen
    # inside the same still-open window.
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 2
    assert _row_count(postgres_engine, "apex_operating_task_result") == len(within_window.tasks)

    # Later still: another simulated restart, well after the window closed.
    # No duplicate checkpoint, no repeated provider-capture attempt, and a
    # genuinely idempotent replay of this cycle's own retained result.
    final_wrapper = _RealOptionsExecutorWrapper(
        ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    )
    after_capture = ExecuteOperatingCycle(plan_repository, state_source, final_wrapper).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=midnight,
        executed_at=OPTIONS_WINDOW_OPEN + timedelta(hours=3),
        idempotency_key=f"{cycle_id}-key",
    )
    assert after_capture.idempotent_replay
    assert not final_wrapper.calls  # a pure replay never reaches the executor at all
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 2
    final_options_result = next(
        item
        for item in after_capture.tasks
        if item.task_type.value == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
    )
    assert final_options_result.output_identities == options_result.output_identities
    assert bool(checkpoint_repository.checkpoint_by_id(final_options_result.output_identities[0]))


class _SimulatedProcessCrash(BaseException):
    """Deliberately not (OSError, RuntimeError, ValueError) -- the set
    ``ExecuteOperatingCycle._dispatch_task`` catches and converts into a
    FAILED task result -- so it propagates all the way out of ``execute()``
    exactly like a real process crash (host restart, OOM kill) would,
    rather than a handled application-level task failure."""


class _CrashOnceThenRealOptionsExecutor:
    """Crashes (uncaught) the first time a nominated task type is dispatched,
    then behaves normally on every later call; ``OPTIONS_RESEARCH_EVIDENCE_
    CAPTURE`` always delegates to the real executor so a crash timed at that
    exact task genuinely exercises the real capture/checkpoint idempotency,
    not a fixture's own."""

    def __init__(self, real: ExistingCapabilityOperatingTaskExecutor, *, crash_on: str) -> None:
        self._real = real
        self._crash_on = crash_on
        self._crashed = False
        self.calls: list[str] = []

    def execute(
        self,
        *,
        task: DueTask,
        cycle_id: str,
        plan: object,
        as_of: datetime,
        executed_at: datetime,
    ) -> TaskExecution:
        self.calls.append(task.task_type.value)
        if task.task_type.value == self._crash_on and not self._crashed:
            self._crashed = True
            raise _SimulatedProcessCrash("fixture: simulated mid-cycle process crash")
        if task.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE:
            return self._real.execute(
                task=task,  # type: ignore[arg-type]
                cycle_id=cycle_id,
                plan=plan,  # type: ignore[arg-type]
                as_of=as_of,
                executed_at=executed_at,
            )
        return TaskExecution(
            OperatingTaskStatus.COMPLETED, (f"{task.task_type.value}-output",), (), True, 0
        )


def test_execute_operating_cycle_resumes_safely_after_a_simulated_crash_during_options_capture(
    postgres_engine: Engine,
) -> None:
    """Restart safety, proved for real: a process crash (uncaught exception
    propagating out of ``execute()``, not a caught application-level
    failure) that happens exactly while the options-capture task is being
    dispatched, inside its own genuine capture window, must not leave the
    cycle permanently unrecoverable (the genuine pre-fix defect: every later
    call raised ``OPERATING_HEALTH_MISSING`` forever) and must not duplicate
    the eventual checkpoint once a resumed call completes it."""

    quote_repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    quote_repository.save_plan(_options_plan())
    quote_repository.save_capability(_options_capability(credentials_available=True))

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-options-crash-resume-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=OPTIONS_SESSION_DAY,
        idempotency_key="postgres-options-crash-resume-plan-key",
    )
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    real_executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    crashing = _CrashOnceThenRealOptionsExecutor(
        real_executor, crash_on="OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
    )

    midnight = OPTIONS_SESSION_DAY
    cycle_id = "postgres-options-crash-resume-cycle"
    genuinely_within_window = OPTIONS_WINDOW_OPEN + timedelta(minutes=5)

    with pytest.raises(_SimulatedProcessCrash):
        ExecuteOperatingCycle(plan_repository, state_source, crashing).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=midnight,
            executed_at=genuinely_within_window,
            idempotency_key=f"{cycle_id}-key",
        )

    # The cycle row and its PLANNED/RUNNING events are durably committed
    # even though the simulated process "died" mid-task; nothing about the
    # crashed options task itself was ever persisted, and finalization
    # (health) genuinely never happened.
    assert plan_repository.cycle(cycle_id) is not None
    assert plan_repository.health(f"{cycle_id}-operating-health") is None
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 0

    # A fresh ``ExecuteOperatingCycle`` (a genuinely new instance, simulating
    # the restarted worker process) resumes cleanly -- no
    # ``OPERATING_HEALTH_MISSING`` -- and this time the (already-crashed-
    # once) executor lets the real options executor genuinely complete.
    resumed = ExecuteOperatingCycle(plan_repository, state_source, crashing).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=midnight,
        executed_at=genuinely_within_window + timedelta(minutes=1),
        idempotency_key=f"{cycle_id}-key",
    )
    assert resumed.health is not None
    options_result = next(
        item
        for item in resumed.tasks
        if item.task_type.value == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
    )
    assert options_result.status.value == "COMPLETED"
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 1
    assert plan_repository.health(f"{cycle_id}-operating-health") is not None

    # A third call is a genuinely pure replay: no further executor calls,
    # no duplicate checkpoint.
    calls_before_third = list(crashing.calls)
    third = ExecuteOperatingCycle(plan_repository, state_source, crashing).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=midnight,
        executed_at=genuinely_within_window + timedelta(minutes=2),
        idempotency_key=f"{cycle_id}-key",
    )
    assert third.idempotent_replay
    assert crashing.calls == calls_before_third
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 1


# --------------------------------------------------------------------------
# Options Capture Temporal Reevaluation Correctness v2: the genuine real-
# Postgres multi-session regression. Deliberately does NOT call
# ``_seed_prior_session_fully_captured`` -- the immediately preceding
# session (Friday 2026-08-07, within the default 10-day calendar lookback
# from Monday 2026-08-10) is left genuinely uncaptured, exactly reproducing
# the real 2026-08-11 production shape: a UTC-day cycle beginning hours
# before today's own window opens observes an already-missed prior session
# first.
# --------------------------------------------------------------------------


def test_execute_operating_cycle_reevaluates_past_a_genuinely_missed_prior_session(
    postgres_engine: Engine,
) -> None:
    """The exact real 2026-08-11 failure, reproduced and proved fixed
    end-to-end against genuine PostgreSQL, genuine XNYS calendar evidence,
    and the real (credential-absent) Alpaca executor path: a prior missed
    session (Friday 2026-08-07) must not freeze the task for the rest of
    the UTC day -- Monday 2026-08-10's own later window must still be
    discovered and executed exactly once, and Friday must never be
    backfilled."""

    quote_repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    options_plan = _options_plan()
    quote_repository.save_plan(options_plan)
    quote_repository.save_capability(_options_capability(credentials_available=True))
    checkpoint_repository = PostgresOptionsResearchOperationsRepository(postgres_engine)

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-options-missed-prior-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=OPTIONS_SESSION_DAY,
        idempotency_key="postgres-options-missed-prior-plan-key",
    )
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    real_executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    wrapper = _RealOptionsExecutorWrapper(real_executor)

    midnight = OPTIONS_SESSION_DAY
    cycle_id = "postgres-options-missed-prior-cycle"

    # T1: 2026-08-11T01:17Z's exact real-production shape -- shortly after
    # this UTC day begins, hours before its own window opens.
    morning = ExecuteOperatingCycle(plan_repository, state_source, wrapper).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=midnight,
        executed_at=midnight + timedelta(hours=1, minutes=17),
        idempotency_key=f"{cycle_id}-key",
    )
    assert "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" not in {
        item.task_type.value for item in morning.tasks
    }
    assert not any(call[0] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" for call in wrapper.calls)
    morning_options_due = next(
        item
        for item in morning.due_work.tasks
        if item.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE
    )
    assert "MISSED_CAPTURE_WINDOW" in morning_options_due.reasons
    assert "2026-08-07" in morning_options_due.source_evidence_ids
    assert "FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON" in morning_options_due.reasons
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 0

    # The prior (Friday) missed session legitimately alerts, delivered for
    # real against genuine Postgres -- exactly once.
    morning_delivered = _deliver_operating_cycle_alerts(
        engine=postgres_engine,
        result=morning,
        executed_at=midnight + timedelta(hours=1, minutes=17),
    )
    morning_missed = [
        item for item in morning_delivered if item["category"] == "OPTIONS_CAPTURE_WINDOW_MISSED"
    ]
    assert len(morning_missed) == 1
    assert morning_missed[0]["status"] == "DELIVERED"

    # T2: still before Monday's own window -- a fresh ``ExecuteOperatingCycle``
    # (simulating a supervised-worker restart) must still find this task
    # reevaluable, not a second frozen result.
    still_before = ExecuteOperatingCycle(
        plan_repository, state_source, _RealOptionsExecutorWrapper(real_executor)
    ).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=midnight,
        executed_at=OPTIONS_WINDOW_OPEN - timedelta(minutes=5),
        idempotency_key=f"{cycle_id}-key",
    )
    assert "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" not in {
        item.task_type.value for item in still_before.tasks
    }
    assert still_before.idempotent_replay

    # T3: Monday's own window is genuinely open.
    later_wrapper = _RealOptionsExecutorWrapper(
        ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    )
    within_window = ExecuteOperatingCycle(plan_repository, state_source, later_wrapper).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=midnight,
        executed_at=OPTIONS_WINDOW_OPEN + timedelta(minutes=5),
        idempotency_key=f"{cycle_id}-key",
    )
    options_result = next(
        item
        for item in within_window.tasks
        if item.task_type.value == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
    )
    assert options_result.status.value == "COMPLETED"
    assert not within_window.idempotent_replay
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 1

    # A genuine capture must never itself also read as a missed alert.
    within_window_delivered = _deliver_operating_cycle_alerts(
        engine=postgres_engine, result=within_window, executed_at=OPTIONS_WINDOW_OPEN
    )
    assert not any(
        item["category"] == "OPTIONS_CAPTURE_WINDOW_MISSED" for item in within_window_delivered
    )

    # T4/T5: later restarts are pure, cheap, idempotent replays.
    final_wrapper = _RealOptionsExecutorWrapper(
        ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    )
    after_capture = ExecuteOperatingCycle(plan_repository, state_source, final_wrapper).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=midnight,
        executed_at=OPTIONS_WINDOW_OPEN + timedelta(hours=3),
        idempotency_key=f"{cycle_id}-key",
    )
    assert after_capture.idempotent_replay
    assert not final_wrapper.calls
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 1

    # Friday 2026-08-07 was never captured -- honestly missed, never
    # backfilled -- the one retained checkpoint is Monday 2026-08-10's own,
    # never a reconstruction of Friday's missed session.
    retained_checkpoints = checkpoint_repository.checkpoints_for_plan(options_plan.plan_id)
    assert {item.session_id for item in retained_checkpoints} == {"2026-08-10"}


def test_todays_own_missed_session_becomes_terminal_with_no_later_window_in_horizon(
    postgres_engine: Engine,
) -> None:
    """The negative case Part 2.D/DoD #11 require: once today's own session
    has genuinely closed uncaptured and no later eligible session remains
    within this UTC-day cycle's own horizon (Friday -> the following
    Monday, well outside), the result must become terminal on the very
    first evaluation and every later poll must be a cheap, pure replay --
    never repeatedly re-snapshotting calendar/checkpoint evidence forever."""

    quote_repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    quote_repository.save_plan(_options_plan())
    quote_repository.save_capability(_options_capability(credentials_available=True))

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    friday = datetime(2026, 8, 7, tzinfo=UTC)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-options-terminal-no-horizon-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=friday,
        idempotency_key="postgres-options-terminal-no-horizon-plan-key",
    )
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    real_executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    cycle_id = "postgres-options-terminal-no-horizon-cycle"

    # First-ever evaluation happens well after Friday's own window closed.
    first = ExecuteOperatingCycle(
        plan_repository, state_source, _RealOptionsExecutorWrapper(real_executor)
    ).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=friday,
        executed_at=friday + timedelta(hours=23),
        idempotency_key=f"{cycle_id}-key",
    )
    options_result = next(
        item for item in first.tasks if item.task_type.value == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
    )
    assert options_result.status.value == "SKIPPED_NOT_DUE"
    options_due = next(
        item
        for item in first.due_work.tasks
        if item.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE
    )
    assert "MISSED_CAPTURE_WINDOW" in options_due.reasons
    assert "2026-08-07" in options_due.source_evidence_ids
    assert "FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON" not in options_due.reasons
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 0

    # A later poll the same UTC day is a pure, cheap fast-path replay --
    # every due task (including options) was already resolved on the very
    # first call, so no further calendar/checkpoint re-derivation happens.
    second_wrapper = _RealOptionsExecutorWrapper(
        ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    )
    second = ExecuteOperatingCycle(plan_repository, state_source, second_wrapper).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=friday,
        executed_at=friday + timedelta(hours=23, minutes=30),
        idempotency_key=f"{cycle_id}-key",
    )
    assert second.idempotent_replay
    assert not second_wrapper.calls
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 0


def test_options_reevaluation_survives_a_crash_on_an_unrelated_task_and_alerts_distinctly(
    postgres_engine: Engine,
) -> None:
    """Compound crash-recovery proof: a process crash on a wholly unrelated
    task (``CRYPTO_MARKET_PULSE``) while the options task is independently
    still deferred (a prior session sits genuinely missed, today's own
    window has not opened) must not corrupt the cycle, and each later
    genuinely distinct missed-session fact must still retain and deliver
    its own alert -- never raising ``OPERATIONAL_EVIDENCE_ID_CONFLICT`` when
    the operating cycle's own alert derivation is invoked more than once for
    the same cycle_id/category across the crash-recovery resumption."""

    quote_repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    quote_repository.save_plan(_options_plan())
    quote_repository.save_capability(_options_capability(credentials_available=True))

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-options-crash-alert-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=OPTIONS_SESSION_DAY,
        idempotency_key="postgres-options-crash-alert-plan-key",
    )
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    real_executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    midnight = OPTIONS_SESSION_DAY
    cycle_id = "postgres-options-crash-alert-cycle"

    crashing = _CrashOnceThenRealOptionsExecutor(real_executor, crash_on="CRYPTO_MARKET_PULSE")
    with pytest.raises(_SimulatedProcessCrash):
        ExecuteOperatingCycle(plan_repository, state_source, crashing).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=midnight,
            executed_at=midnight + timedelta(hours=1, minutes=17),
            idempotency_key=f"{cycle_id}-key",
        )
    assert plan_repository.health(f"{cycle_id}-operating-health") is None
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 0
    # A crash before the caller ever reaches the post-``execute()`` alert
    # step means no alert was ever attempted for this cycle yet -- nothing
    # to collide with on resume.
    assert _row_count(postgres_engine, "operational_alert_delivery") == 0

    # Resume: still hours before Monday's own window. The crashed task now
    # completes; the options task remains genuinely deferred (a prior
    # session -- Friday 2026-08-07 -- is still missed, but Monday's own
    # window is still ahead within this cycle's horizon).
    resumed = ExecuteOperatingCycle(plan_repository, state_source, crashing).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=midnight,
        executed_at=midnight + timedelta(hours=1, minutes=20),
        idempotency_key=f"{cycle_id}-key",
    )
    assert resumed.health is not None
    assert "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" not in {
        item.task_type.value for item in resumed.tasks
    }
    prior_day_alerts = _deliver_operating_cycle_alerts(
        engine=postgres_engine,
        result=resumed,
        executed_at=midnight + timedelta(hours=1, minutes=20),
    )
    prior_day_missed = [
        item for item in prior_day_alerts if item["category"] == "OPTIONS_CAPTURE_WINDOW_MISSED"
    ]
    assert len(prior_day_missed) == 1
    assert _row_count(postgres_engine, "operational_alert_delivery") == 1

    # Monday's own window opens and genuinely captures.
    within_window = ExecuteOperatingCycle(plan_repository, state_source, crashing).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=midnight,
        executed_at=OPTIONS_WINDOW_OPEN + timedelta(minutes=5),
        idempotency_key=f"{cycle_id}-key",
    )
    options_result = next(
        item
        for item in within_window.tasks
        if item.task_type.value == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
    )
    assert options_result.status.value == "COMPLETED"
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 1

    # A genuine capture delivers no new missed-window alert, and -- most
    # importantly -- does not raise ``OPERATIONAL_EVIDENCE_ID_CONFLICT``
    # against the already-delivered prior-day alert, even though this is a
    # second ``derive_operating_cycle_alert_candidates`` call for the exact
    # same cycle_id/category.
    within_window_alerts = _deliver_operating_cycle_alerts(
        engine=postgres_engine,
        result=within_window,
        executed_at=OPTIONS_WINDOW_OPEN + timedelta(minutes=5),
    )
    assert not any(
        item["category"] == "OPTIONS_CAPTURE_WINDOW_MISSED" for item in within_window_alerts
    )
    assert _row_count(postgres_engine, "operational_alert_delivery") == 1

    # Restart: pure idempotent replay, no duplicate checkpoint or alert.
    calls_before_restart = list(crashing.calls)
    restarted = ExecuteOperatingCycle(plan_repository, state_source, crashing).execute(
        cycle_id=cycle_id,
        plan_id=plan.plan_id,
        as_of=midnight,
        executed_at=OPTIONS_WINDOW_OPEN + timedelta(hours=1),
        idempotency_key=f"{cycle_id}-key",
    )
    assert restarted.idempotent_replay
    assert crashing.calls == calls_before_restart
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 1
    assert _row_count(postgres_engine, "operational_alert_delivery") == 1


def test_portfolio_snapshot_refresh_task_is_idempotent_and_restart_safe(
    postgres_engine: Engine,
) -> None:
    """Calling the same task twice with the same cycle identity -- e.g. a
    crash between the snapshot durably completing and the outer cycle
    recording the task result, then a resume -- must never duplicate a
    portfolio snapshot row."""

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-portfolio-refresh-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW,
        idempotency_key="postgres-portfolio-refresh-plan-key",
    )
    CreateCryptoPaperAccount(PostgresCryptoPaperRepository(postgres_engine)).execute(
        account_id=GENUINE_CRYPTO_PAPER_ACCOUNT_ID,
        opening_cash="1000",
        created_at=NOW,
        idempotency_key=f"{GENUINE_CRYPTO_PAPER_ACCOUNT_ID}-bootstrap",
    )
    CreateKalshiPaperAccount(PostgresKalshiOperatingRepository(postgres_engine)).execute(
        account_id=GENUINE_KALSHI_PAPER_ACCOUNT_ID,
        opening_cash="1000",
        created_at=NOW,
        idempotency_key=f"{GENUINE_KALSHI_PAPER_ACCOUNT_ID}-bootstrap",
    )

    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    task = _due_task(
        OperatingTaskType.PORTFOLIO_SNAPSHOT_REFRESH, cycle_id="cycle-portfolio-restart"
    )

    first = executor.execute(
        task=task, cycle_id="cycle-portfolio-restart", plan=plan, as_of=NOW, executed_at=NOW
    )
    assert first.status.value == "COMPLETED"
    assert first.changed is True
    assert first.output_identities

    second = executor.execute(
        task=task, cycle_id="cycle-portfolio-restart", plan=plan, as_of=NOW, executed_at=NOW
    )
    assert second.status.value == "REUSED"
    assert second.changed is False
    assert second.output_identities == first.output_identities

    assert _row_count(postgres_engine, "portfolio_snapshot") == 1
    portfolio_repository = PostgresPortfolioSnapshotRepository(postgres_engine)
    assert portfolio_repository.latest_portfolio_snapshot() is not None


def test_mandate_evaluation_task_fails_loudly_without_an_active_mandate(
    postgres_engine: Engine,
) -> None:
    """``plan_due_work`` already gates this task on an active mandate
    existing (UNAVAILABLE otherwise); reaching the executor without one
    would itself be a planning bug, so it must fail loudly rather than
    silently no-op or fabricate an evaluation."""

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-mandate-no-active-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW,
        idempotency_key="postgres-mandate-no-active-plan-key",
    )
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    task = _due_task(
        OperatingTaskType.MANDATE_EVALUATION_REBUILD, cycle_id="cycle-mandate-no-active"
    )

    with pytest.raises(OperatingCycleError, match="OPERATING_MANDATE_EVALUATION_NO_ACTIVE_MANDATE"):
        executor.execute(
            task=task, cycle_id="cycle-mandate-no-active", plan=plan, as_of=NOW, executed_at=NOW
        )


def _seed_eligible_opportunity(
    engine: Engine, *, projection_id: str, generated_at: datetime
) -> SharedOpportunityProjection:
    opportunity = SharedOpportunityProjection.create(
        projection_id=projection_id,
        venue=SharedVenue.CRYPTO,
        source_record_type="Test",
        source_record_id="src-1",
        source_canonical_hash="d" * 64,
        strategy_or_method_id="TEST_METHOD",
        instrument_id="BTC-USD",
        product_or_market_id="BTC-USD",
        direction="HYPOTHETICAL_LONG_SPOT_RESEARCH",
        generated_at=generated_at,
        valid_until=generated_at + timedelta(days=30),
        evidence_class="GENUINE_PUBLIC",
        freshness_seconds=60,
        freshness_limit_seconds=3600,
        expected_gross_return="0.02",
        expected_net_return="0.01",
        comparable_value="0.4",
        comparable_value_identity=None,
        uncertainty="0.3",
        maximum_loss="50",
        modeled_fees="1",
        modeled_slippage="1",
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital="100",
        maximum_sensible_research_allocation="200",
        source_maximum_allocation_fraction="0.5",
        expected_holding_days=5,
        estimated_lockup_days=5,
        supporting_evidence_ids=("ev-1",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(),
    )
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id=f"{projection_id}-set",
        created_at=generated_at,
        projections=(opportunity,),
    )
    PostgresSharedOpportunityRepository(engine).save_opportunity_bundle(
        idempotency_key=f"{projection_id}-bundle",
        opportunity_set=opportunity_set,
        projections=(opportunity,),
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    return opportunity


def test_mandate_evaluation_task_evaluates_eligible_opportunity_and_is_idempotent(
    postgres_engine: Engine,
) -> None:
    """Full wiring proof: a genuinely authored mandate plus a genuinely
    ELIGIBLE retained Opportunity produces exactly one persisted
    ``MandateEvaluation`` per opportunity, replays idempotently on a second
    call, and never touches any provider, order, or reservation table."""

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-mandate-eval-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW,
        idempotency_key="postgres-mandate-eval-plan-key",
    )
    CreateInvestmentMandate(PostgresInvestmentMandateRepository(postgres_engine)).execute(
        idempotency_key="postgres-mandate-eval-mandate-key",
        lineage_id=DEFAULT_MANDATE_LINEAGE_ID,
        effective_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=1),
        total_capital_allocated="1000",
        permitted_venues=("COINBASE",),
        permitted_market_families=("CRYPTO_SPOT",),
        disallowed_market_families=(),
        maximum_aggregate_exposure_fraction="0.5",
        maximum_per_instrument_fraction="0.5",
        maximum_venue_concentration_fraction="0.5",
        maximum_market_family_concentration_fraction="0.5",
        maximum_capital_lockup_days=30,
        autonomy_requirement=AutonomyRequirement.AUTONOMOUS_WITHIN_LIMITS,
        notes="fixture mandate for recurring-cycle integration test",
    )
    _seed_eligible_opportunity(
        postgres_engine, projection_id="postgres-mandate-eval-opportunity", generated_at=NOW
    )
    # A real cycle always runs PORTFOLIO_SNAPSHOT_REFRESH earlier in
    # TASK_ORDER, so a snapshot already exists by the time mandate
    # evaluation runs; seed the identical minimal (empty-balance, still
    # ``COMPLETE``) shape directly rather than re-running that task here.
    empty_snapshot = build_portfolio_snapshot(
        snapshot_id="postgres-mandate-eval-portfolio-snapshot",
        as_of=NOW,
        created_at=NOW,
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    PostgresPortfolioSnapshotRepository(postgres_engine).save_portfolio_snapshot(
        idempotency_key="postgres-mandate-eval-portfolio-snapshot-key",
        snapshot=empty_snapshot,
        balances=(),
        positions=(),
    )

    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    task = _due_task(OperatingTaskType.MANDATE_EVALUATION_REBUILD, cycle_id="cycle-mandate-eval")

    first = executor.execute(
        task=task, cycle_id="cycle-mandate-eval", plan=plan, as_of=NOW, executed_at=NOW
    )
    assert first.status.value == "COMPLETED"
    assert len(first.output_identities) == 1
    assert "MANDATE_ELIGIBLE_OPPORTUNITIES_EVALUATED:1" in first.warnings

    second = executor.execute(
        task=task, cycle_id="cycle-mandate-eval", plan=plan, as_of=NOW, executed_at=NOW
    )
    assert second.status.value == "REUSED"
    assert second.output_identities == first.output_identities

    assert _row_count(postgres_engine, "mandate_evaluation") == 1
    # Zero writes anywhere financial: no PAPER state/order/reservation table
    # touched -- this task only ever reads retained evidence and persists a
    # research-only MandateEvaluation row.
    for table in (
        "apex_crypto_paper_state_snapshot",
        "apex_kalshi_paper_state_snapshot",
        "cross_venue_paper_trial_proposal",
    ):
        assert _row_count(postgres_engine, table) == 0


def test_mandate_evaluation_task_survives_realistic_long_cycle_and_projection_ids(
    postgres_engine: Engine,
) -> None:
    """Regression test for the 2026-08-22 unattended-runtime incident.

    The live worker's real ``cycle_id`` (``{plan_id}-worker-{yyyymmdd}``,
    see ``deploy/apex-operating-cycle-worker.service``'s genuinely deployed
    ``--plan-id apex-operating-cycle-plan-crypto-book-evidence-v1``) and a
    real ``shared-crypto-...`` current-opportunity ``projection_id``
    (``adapters/shared_opportunity_sources.py``'s
    ``f"shared-crypto-{run.run_id}-{asset}-opportunity"`` shape, where
    ``run_id`` is itself the full ``cycle_id``) are each individually well
    within every domain/persistence bound (both comfortably under the
    ``_identity``/column ``String(300)`` ceiling). But
    ``MANDATE_EVALUATION_REBUILD`` spliced both raw strings into one
    ``idempotency_key`` -- the only task in this executor that folds two
    independently-unbounded identifiers together instead of a single
    already-bounded id plus a short literal -- and blew past
    ``mandate_evaluation.idempotency_key``'s ``VARCHAR(160)`` budget the
    first time a genuinely-ELIGIBLE crypto opportunity reached this task
    with this real naming shape. Before the fix this raises
    ``sqlalchemy.exc.DataError`` (``StringDataRightTruncation``) instead of
    completing -- exactly the live traceback."""

    real_plan_id = "apex-operating-cycle-plan-crypto-book-evidence-v1"
    long_cycle_id = f"{real_plan_id}-worker-20260822"
    assert len(long_cycle_id) == 65

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-mandate-eval-long-ids-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW,
        idempotency_key="postgres-mandate-eval-long-ids-plan-key",
    )
    CreateInvestmentMandate(PostgresInvestmentMandateRepository(postgres_engine)).execute(
        idempotency_key="postgres-mandate-eval-long-ids-mandate-key",
        lineage_id=DEFAULT_MANDATE_LINEAGE_ID,
        effective_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=1),
        total_capital_allocated="1000",
        permitted_venues=("COINBASE",),
        permitted_market_families=("CRYPTO_SPOT",),
        disallowed_market_families=(),
        maximum_aggregate_exposure_fraction="0.5",
        maximum_per_instrument_fraction="0.5",
        maximum_venue_concentration_fraction="0.5",
        maximum_market_family_concentration_fraction="0.5",
        maximum_capital_lockup_days=30,
        autonomy_requirement=AutonomyRequirement.AUTONOMOUS_WITHIN_LIMITS,
        notes="fixture mandate for the long cycle/projection id regression test",
    )
    long_projection_id = f"shared-crypto-{long_cycle_id}-crypto-pulse-discovery-eth-opportunity"
    assert len(long_projection_id) == 118
    _seed_eligible_opportunity(postgres_engine, projection_id=long_projection_id, generated_at=NOW)
    empty_snapshot = build_portfolio_snapshot(
        snapshot_id="postgres-mandate-eval-long-ids-portfolio-snapshot",
        as_of=NOW,
        created_at=NOW,
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    PostgresPortfolioSnapshotRepository(postgres_engine).save_portfolio_snapshot(
        idempotency_key="postgres-mandate-eval-long-ids-portfolio-snapshot-key",
        snapshot=empty_snapshot,
        balances=(),
        positions=(),
    )

    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    task = _due_task(OperatingTaskType.MANDATE_EVALUATION_REBUILD, cycle_id=long_cycle_id)

    first = executor.execute(
        task=task, cycle_id=long_cycle_id, plan=plan, as_of=NOW, executed_at=NOW
    )
    assert first.status.value == "COMPLETED"
    assert len(first.output_identities) == 1
    assert "MANDATE_ELIGIBLE_OPPORTUNITIES_EVALUATED:1" in first.warnings

    # Idempotent replay must still recognize the exact same evaluation --
    # proving the fix didn't just avoid the crash but preserved genuine
    # replay-detection semantics for this exact (cycle_id, projection_id)
    # pair.
    second = executor.execute(
        task=task, cycle_id=long_cycle_id, plan=plan, as_of=NOW, executed_at=NOW
    )
    assert second.status.value == "REUSED"
    assert second.output_identities == first.output_identities

    assert _row_count(postgres_engine, "mandate_evaluation") == 1


# --------------------------------------------------------------------------
# Crypto Universe Recurring Refresh & Dependency Health v1
# --------------------------------------------------------------------------


class _GenuineLookingCryptoUniverseSource(FixtureCoinbaseUniverseSource):
    """Deterministic and fully offline (no live network access), but
    classified as genuine (``HISTORICAL_PUBLIC``) evidence -- lets these
    persistence tests seed real-shaped, non-fixture crypto-universe evidence
    without a live Coinbase call. The real executor branch always uses the
    genuinely network-calling ``CoinbasePublicUniverseSource``, whose own
    ``evidence_class`` is also ``HISTORICAL_PUBLIC``
    (``coinbase_universe_public.py``) -- this fixture only swaps out the
    transport, never the evidence classification."""

    evidence_class = EvidenceClass.HISTORICAL_PUBLIC


class _ChangedGenuineCryptoUniverseSource(_GenuineLookingCryptoUniverseSource):
    """Same genuine classification, one product genuinely swapped for a new,
    high-volume listing -- simulates a real provider catalog change (a new
    listing displacing the lowest-volume incumbent) without any live
    network access."""

    _PRODUCTS = (
        *_GenuineLookingCryptoUniverseSource._PRODUCTS[:-1],
        ("NEWCOIN-USD", "NEWCOIN", "USD", "999999999", "SPOT"),
    )


def _seed_universe_snapshot(
    repository: PostgresCryptoMarketPulseRepository,
    source: FixtureCoinbaseUniverseSource,
    *,
    identity: str,
    created_at: datetime,
):
    """Exercises the identical, existing, already-reviewed application
    services the real ``CRYPTO_UNIVERSE_REFRESH`` executor branch composes
    (``CapturePublicCryptoProducts`` -> ``CaptureCryptoUniverseObservations``
    -> ``MaterializeCryptoUniverse``), against a deterministic offline
    source, to build genuine-shaped retained universe evidence for tests
    without ever touching a live provider."""

    capture = CapturePublicCryptoProducts(repository, source).execute(
        capture_id=f"{identity}-capture", requested_at=created_at
    )
    # Matches the real executor's own interval derivation exactly
    # (``completed_utc_day(as_of)`` and ``MAX_CANDLES_PER_REQUEST`` days) so
    # a test that pre-seeds evidence under the identity the executor itself
    # would derive also matches on interval bounds, not just capture/
    # snapshot identity -- otherwise a genuine ``CRYPTO_OBSERVATION_
    # IDENTITY_CONFLICT`` (an interval mismatch under the same observation
    # id) is the honest, correct outcome, not a test bug to paper over.
    interval_end = created_at.replace(hour=0, minute=0, second=0, microsecond=0)
    CaptureCryptoUniverseObservations(repository, source).execute(
        capture_id=capture.capture_id,
        interval_start=interval_end - timedelta(days=MAX_CANDLES_PER_REQUEST),
        interval_end=interval_end,
        requested_at=created_at,
    )
    return MaterializeCryptoUniverse(repository).execute(
        snapshot_id=f"{identity}-snapshot", capture_id=capture.capture_id, created_at=created_at
    )


def test_crypto_universe_refresh_capability_is_genuinely_reported_supported(
    postgres_engine: Engine,
) -> None:
    """Before this milestone, ``crypto_universe_refresh_supported`` was an
    unconditional hardcoded ``False`` regardless of retained evidence. It
    must now genuinely reflect that Apex's own bounded, credential-free
    Coinbase product-catalog capability exists -- proven here against
    completely empty retained state (no universe evidence at all yet), so
    the assertion cannot be accidentally satisfied by evidence presence
    instead of genuine capability support."""

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    state = state_source.snapshot(as_of=NOW)
    assert state.crypto_universe_refresh_supported is True
    assert state.crypto_universe_id is None
    assert state.crypto_universe_created_at is None


def test_operating_state_resolves_latest_genuine_universe_and_excludes_fixture_evidence(
    postgres_engine: Engine,
) -> None:
    """A retained FIXTURE-evidence-class universe snapshot must never leak
    into the real recurring-cycle due-state, even when it is chronologically
    the most recent retained row -- exactly the real condition this
    milestone found live in the dev database (an earlier acceptance run's
    fixture snapshot was chronologically the latest retained row)."""

    crypto_repository = PostgresCryptoMarketPulseRepository(postgres_engine)
    genuine = _seed_universe_snapshot(
        crypto_repository,
        _GenuineLookingCryptoUniverseSource(),
        identity="genuine-earlier",
        created_at=NOW - timedelta(days=2),
    )
    fixture = _seed_universe_snapshot(
        crypto_repository,
        FixtureCoinbaseUniverseSource(),
        identity="fixture-later",
        created_at=NOW - timedelta(days=1),
    )
    assert fixture.created_at > genuine.created_at  # fixture is chronologically latest

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    state = state_source.snapshot(as_of=NOW)
    assert state.crypto_universe_id == genuine.snapshot_id
    assert state.crypto_universe_hash == genuine.canonical_hash
    assert state.crypto_universe_created_at == genuine.created_at


def test_crypto_universe_refresh_task_is_idempotent_and_restart_safe_without_reaching_provider(
    postgres_engine: Engine,
) -> None:
    """Pre-seeds the exact capture/snapshot identity the executor itself
    would derive for this cycle_id, so the real executor can only genuinely
    succeed here by reusing that retained evidence -- proving restart safety
    (e.g. a crash after durable completion, then a resumed call) without
    ever needing a live Coinbase request. ``CapturePublicCryptoProducts``/
    ``CaptureCryptoUniverseObservations`` would raise loudly against the
    real network-calling source if this executor ever tried to actually
    reach the provider here, so a clean ``REUSED``/zero-``network_requests``
    result is direct proof the provider was never touched."""

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-universe-restart-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW,
        idempotency_key="postgres-universe-restart-plan-key",
    )
    crypto_repository = PostgresCryptoMarketPulseRepository(postgres_engine)
    cycle_id = "cycle-universe-restart"
    seeded = _seed_universe_snapshot(
        crypto_repository,
        _GenuineLookingCryptoUniverseSource(),
        identity=f"{cycle_id}-crypto-universe",
        created_at=NOW,
    )

    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    task = _due_task(OperatingTaskType.CRYPTO_UNIVERSE_REFRESH, cycle_id=cycle_id)

    first = executor.execute(task=task, cycle_id=cycle_id, plan=plan, as_of=NOW, executed_at=NOW)
    assert first.status.value == "REUSED"
    assert first.changed is False
    assert first.network_requests == 0
    assert first.output_identities == (seeded.source_capture_id, seeded.snapshot_id)

    second = executor.execute(task=task, cycle_id=cycle_id, plan=plan, as_of=NOW, executed_at=NOW)
    assert second == first

    assert _row_count(postgres_engine, "crypto_universe_snapshot") == 1
    assert _row_count(postgres_engine, "crypto_public_product_capture") == 1


def test_crypto_universe_refresh_resumes_safely_after_a_crash_between_capture_and_materialize(
    postgres_engine: Engine,
) -> None:
    """A crash genuinely between the product-catalog capture durably
    completing and observation-capture/materialization running must be
    resumable without ever re-fetching the already-retained catalog --
    proven at the exact application-service layer the real executor
    composes (``CapturePublicCryptoProducts`` ->
    ``CaptureCryptoUniverseObservations`` -> ``MaterializeCryptoUniverse``),
    using a request-counting wrapper around a deterministic offline source
    so "the catalog was never re-requested" is directly observed rather than
    inferred."""

    class _CountingUniverseSource(_GenuineLookingCryptoUniverseSource):
        def __init__(self) -> None:
            self.catalog_requests = 0

        def capture_products(self, **kwargs: object):  # type: ignore[override]
            self.catalog_requests += 1
            return super().capture_products(**kwargs)  # type: ignore[arg-type]

    crypto_repository = PostgresCryptoMarketPulseRepository(postgres_engine)
    source = _CountingUniverseSource()
    identity = "crash-mid-refresh"

    # Phase 1 ("before the crash"): only the catalog capture durably
    # completes -- paginated, so genuinely more than one page request is
    # expected here; what matters is that this count never grows again.
    capture = CapturePublicCryptoProducts(crypto_repository, source).execute(
        capture_id=f"{identity}-capture", requested_at=NOW
    )
    catalog_requests_after_capture = source.catalog_requests
    assert catalog_requests_after_capture > 0
    assert _row_count(postgres_engine, "crypto_universe_snapshot") == 0

    # Phase 2 ("resume after the crash"): observations and materialization
    # were never durably completed, so both must genuinely run now -- but
    # the already-retained catalog must never be re-fetched.
    interval_end = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    CaptureCryptoUniverseObservations(crypto_repository, source).execute(
        capture_id=capture.capture_id,
        interval_start=interval_end - timedelta(days=MAX_CANDLES_PER_REQUEST),
        interval_end=interval_end,
        requested_at=NOW,
    )
    resumed_snapshot = MaterializeCryptoUniverse(crypto_repository).execute(
        snapshot_id=f"{identity}-snapshot", capture_id=capture.capture_id, created_at=NOW
    )
    assert source.catalog_requests == catalog_requests_after_capture  # not re-fetched
    assert resumed_snapshot.source_capture_id == capture.capture_id
    assert _row_count(postgres_engine, "crypto_universe_snapshot") == 1
    assert _row_count(postgres_engine, "crypto_public_product_capture") == 1

    # A second "resume" (e.g. the next 5-minute tick observing the same
    # already-resolved cycle) must be fully idempotent too.
    replayed_snapshot = MaterializeCryptoUniverse(crypto_repository).execute(
        snapshot_id=f"{identity}-snapshot", capture_id=capture.capture_id, created_at=NOW
    )
    assert replayed_snapshot.canonical_hash == resumed_snapshot.canonical_hash
    assert _row_count(postgres_engine, "crypto_universe_snapshot") == 1


def test_crypto_universe_refresh_not_due_within_cadence_never_blocks_a_due_pulse(
    postgres_engine: Engine,
) -> None:
    """The genuine fix's core dependency-semantics claim, proven live: once
    a valid, fresh (within the seven-day cadence) genuine universe snapshot
    is retained, CRYPTO_UNIVERSE_REFRESH itself is correctly NOT_DUE, and
    -- unlike before this milestone, when the capability gap made this
    permanently UNAVAILABLE -- that NOT_DUE state must never again block a
    genuinely due CRYPTO_MARKET_PULSE."""

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-universe-not-due-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW - timedelta(days=30),
        idempotency_key="postgres-universe-not-due-plan-key",
    )
    crypto_repository = PostgresCryptoMarketPulseRepository(postgres_engine)
    _seed_universe_snapshot(
        crypto_repository,
        _GenuineLookingCryptoUniverseSource(),
        identity="cadence-fresh",
        created_at=NOW - timedelta(days=6),
    )
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    due = PlanOperatingDueWork(plan_repository, state_source).execute(
        cycle_id="cycle-universe-not-due", plan_id=plan.plan_id, as_of=NOW
    )
    by_type = {task.task_type: task for task in due.tasks}
    universe_task = by_type[OperatingTaskType.CRYPTO_UNIVERSE_REFRESH]
    pulse_task = by_type[OperatingTaskType.CRYPTO_MARKET_PULSE]
    assert universe_task.disposition is DueDisposition.NOT_DUE
    assert "SEVEN_DAY_UNIVERSE_CADENCE_NOT_DUE" in universe_task.reasons
    assert pulse_task.disposition is not DueDisposition.BLOCKED
    assert "DUE_UNIVERSE_REFRESH_UNAVAILABLE" not in pulse_task.reasons


def test_crypto_universe_refresh_is_due_once_past_seven_day_cadence(
    postgres_engine: Engine,
) -> None:
    """Symmetric proof of the other side of the same cadence: retained
    genuine universe evidence older than seven days must be genuinely DUE
    again, not silently treated as still-fresh forever."""

    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="postgres-universe-due-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW - timedelta(days=30),
        idempotency_key="postgres-universe-due-plan-key",
    )
    crypto_repository = PostgresCryptoMarketPulseRepository(postgres_engine)
    _seed_universe_snapshot(
        crypto_repository,
        _GenuineLookingCryptoUniverseSource(),
        identity="cadence-stale",
        created_at=NOW - timedelta(days=8),
    )
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    due = PlanOperatingDueWork(plan_repository, state_source).execute(
        cycle_id="cycle-universe-due", plan_id=plan.plan_id, as_of=NOW
    )
    universe_task = next(
        task for task in due.tasks if task.task_type is OperatingTaskType.CRYPTO_UNIVERSE_REFRESH
    )
    assert universe_task.disposition is DueDisposition.DUE
    assert "SEVEN_DAY_UNIVERSE_CADENCE_DUE" in universe_task.reasons


def test_universe_snapshot_is_immutable_and_a_changed_catalog_is_a_new_snapshot(
    postgres_engine: Engine,
) -> None:
    """Two point-in-time proofs together: (1) a once-retained universe
    snapshot's own content/hash never changes just because a later, newer
    snapshot also exists -- genuine point-in-time evidence, not a mutable
    "current membership" row a later write could quietly alter; (2) a
    genuinely changed provider catalog (a new listing displacing the
    lowest-volume incumbent) produces a distinct, deterministic snapshot
    with different eligible-product membership, not a silently-reused
    stale result."""

    crypto_repository = PostgresCryptoMarketPulseRepository(postgres_engine)
    older = _seed_universe_snapshot(
        crypto_repository,
        _GenuineLookingCryptoUniverseSource(),
        identity="pit-older",
        created_at=NOW - timedelta(days=10),
    )
    changed = _seed_universe_snapshot(
        crypto_repository,
        _ChangedGenuineCryptoUniverseSource(),
        identity="pit-changed",
        created_at=NOW - timedelta(days=1),
    )

    reread = crypto_repository.universe_snapshot(older.snapshot_id)
    assert reread is not None
    assert reread.canonical_hash == older.canonical_hash
    assert reread.eligible_product_ids == older.eligible_product_ids

    assert changed.canonical_hash != older.canonical_hash
    assert changed.considered_product_ids != older.considered_product_ids
    assert "NEWCOIN-USD" in changed.considered_product_ids
    assert "NEWCOIN-USD" not in older.considered_product_ids
