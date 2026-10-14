from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

import apex.adapters.operating_cycle_tasks as operating_tasks
from apex.adapters.operating_cycle_tasks import ExistingCapabilityOperatingTaskExecutor
from apex.adapters.xnys_calendar import ReviewedXNYSCalendar
from apex.application.operating_cycle import (
    ExecuteOperatingCycle,
    SetOperatingCycleControl,
    SetPaperActivation,
    derive_operating_cycle_alert_candidates,
)
from apex.domain.crypto_market_pulse import RefreshState
from apex.domain.crypto_universe import OpportunityStatus
from apex.domain.operating_cycle import (
    COUNTERFACTUAL_FOLLOWUP_DEFERRED_FOR_OPTIONS_PRIORITY,
    COUNTERFACTUAL_FOLLOWUP_OPTIONS_GUARD_INTERVAL,
    DueDisposition,
    DueTask,
    OperatingControlState,
    OperatingCycleError,
    OperatingCyclePlan,
    OperatingHealthState,
    OperatingTaskResult,
    OperatingTaskStatus,
    OperatingTaskType,
    PaperActivationEvent,
    PaperActivationState,
    PaperContinuationHealthState,
    RetainedOperatingState,
    TaskExecution,
    build_operating_brief,
    build_operating_health,
    compute_paper_continuation_health,
    counterfactual_followup_deferred_for_options_priority,
    effective_paper_activation,
    options_capture_awaits_same_day_reevaluation,
    options_capture_due_task,
    plan_due_work,
    terminal_cycle_state,
)
from apex.domain.operational_reliability import AlertSeverity
from apex.domain.options_research_operations import (
    OptionsCaptureWindowPolicy,
    determine_options_capture_due_state,
)

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)


def _plan() -> OperatingCyclePlan:
    return OperatingCyclePlan.create(
        plan_id="operating-plan-v1",
        crypto_pulse_plan_id="genuine-crypto-pulse-plan-v1",
        actor="fixture-operator",
        created_at=NOW - timedelta(days=1),
    )


def _state(**overrides: object) -> RetainedOperatingState:
    values: dict[str, object] = {
        "captured_at": NOW,
        "crypto_universe_id": "universe-1",
        "crypto_universe_hash": "u" * 64,
        "crypto_universe_created_at": NOW,
        "crypto_pulse_id": "pulse-1",
        "crypto_pulse_hash": "p" * 64,
        "crypto_pulse_as_of": NOW.replace(hour=0),
        "total_market_snapshot_id": "context-1",
        "total_market_hash": "c" * 64,
        "total_market_source_batch_id": "batch-1",
        "securities_batch_id": "batch-1",
        "securities_batch_hash": "b" * 64,
        "securities_last_period_end": NOW - timedelta(days=1),
        "kalshi_evaluation_ids": ("kalshi-1",),
        "kalshi_evidence_hash": "k" * 64,
        "kalshi_latest_at": NOW - timedelta(seconds=60),
        "shared_set_id": "shared-1",
        "shared_set_hash": "s" * 64,
        "shared_crypto_pulse_id": "pulse-1",
        "shared_kalshi_evaluation_ids": ("kalshi-1",),
        "enriched_set_id": "enriched-1",
        "enriched_set_hash": "e" * 64,
        "enriched_shared_set_id": "shared-1",
        "enriched_context_snapshot_id": "context-1",
        "allocation_id": "allocation-1",
        "allocation_hash": "a" * 64,
        "latest_brief_as_of": NOW,
        "schema_revision": "0063_cross_venue_allocation",
    }
    values.update(overrides)
    return RetainedOperatingState.create(**values)


def test_no_tasks_due_is_deterministic_and_ordered() -> None:
    first = plan_due_work(plan=_plan(), cycle_id="cycle-none-due", as_of=NOW, retained=_state())
    second = plan_due_work(plan=_plan(), cycle_id="cycle-none-due", as_of=NOW, retained=_state())
    assert first == second
    # PAPER trial execution manages continuously open positions rather than only
    # reacting to changed inputs, so it stays DUE every cycle once an allocation
    # decision exists, unlike the pure rebuild-if-changed tasks. Portfolio
    # snapshot refresh is likewise unconditionally due every cycle, and so is
    # counterfactual follow-up refresh (Counterfactual Follow-Up Recurring
    # Integration v1) -- its own genuinely-due backlog and Kalshi request
    # budget are entirely the executor's concern, never this planning layer's.
    # Counterfactual outcome refresh (Counterfactual Outcome Feedback v1) is
    # unconditionally due every cycle for the identical reason, immediately
    # after it in ``TASK_ORDER``.
    # Strategy PAPER trial execution (Recurring Strategy PAPER Orchestration
    # v1) is likewise unconditionally due every cycle -- unlike the older
    # CRYPTO/KALSHI PAPER trial tasks, it has no dependency on an allocation
    # decision at all (see plan_due_work's own comment on this task type).
    # Mandate evaluation is the one exception that is neither DUE nor NOT_DUE
    # here: the fixture ``_state()`` never authors a mandate, so it fails
    # closed to UNAVAILABLE exactly like a missing provider capability.
    always_due_types = {
        OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION,
        OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION,
        OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION,
        OperatingTaskType.STRATEGY_TRIAL_OUTCOME_ASSESSMENT,
        OperatingTaskType.PORTFOLIO_SNAPSHOT_REFRESH,
        OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH,
        OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH,
        OperatingTaskType.EVIDENCE_GAP_RECOVERY,
    }
    for item in first.tasks:
        if item.task_type is OperatingTaskType.MANDATE_EVALUATION_REBUILD:
            assert item.disposition is DueDisposition.UNAVAILABLE
        elif item.task_type in always_due_types:
            assert item.disposition is DueDisposition.DUE
        else:
            assert item.disposition is DueDisposition.NOT_DUE
    assert tuple(item.task_type for item in first.tasks) == first.dependency_order


def test_new_plan_preserves_unrelated_bindings_and_only_changes_pulse_reference() -> None:
    """Genuine Crypto Book Evidence Activation v1 registers a brand new
    ``OperatingCyclePlan`` revision to correct which crypto pulse sub-plan
    the recurring cycle is bound to, rather than mutating or reusing
    ``apex-operating-cycle-plan-v1``'s identity. That correction must change
    only the plan's identity, its bound crypto pulse plan, its actor, and its
    creation time -- every other task binding (cadences, triggers, freshness
    policies, request bounds, failure/retry policy, timezone, boundary hour)
    the factory derives must stay byte-for-byte identical, proving the
    correction cannot silently drift Kalshi refresh behavior, Shared
    Opportunity policy, Total Market context, allocation policy, or any other
    unrelated task binding."""

    created_at = NOW - timedelta(days=1)
    old_plan = OperatingCyclePlan.create(
        plan_id="apex-operating-cycle-plan-v1",
        crypto_pulse_plan_id="crypto-market-pulse-genuine-daily-v1",
        actor="codex-operator",
        created_at=created_at,
    )
    new_plan = OperatingCyclePlan.create(
        plan_id="apex-operating-cycle-plan-crypto-book-evidence-v1",
        crypto_pulse_plan_id="crypto-market-pulse-genuine-public-book-2026-08-02-v1",
        actor="claude-operator",
        created_at=created_at,
    )
    assert new_plan.plan_id != old_plan.plan_id
    assert new_plan.crypto_pulse_plan_id != old_plan.crypto_pulse_plan_id
    assert new_plan.canonical_hash != old_plan.canonical_hash
    assert new_plan.crypto_pulse_plan_id == "crypto-market-pulse-genuine-public-book-2026-08-02-v1"

    deliberately_changed = {
        "plan_id",
        "crypto_pulse_plan_id",
        "actor",
        "created_at",
        "canonical_hash",
    }
    for field in fields(OperatingCyclePlan):
        if field.name in deliberately_changed:
            continue
        assert getattr(new_plan, field.name) == getattr(old_plan, field.name), (
            f"unrelated binding {field.name!r} drifted on the corrected plan revision"
        )


def test_research_operations_task_refreshes_agenda_without_automatic_experiment() -> None:
    due = plan_due_work(
        plan=_plan(),
        cycle_id="cycle-research-operations",
        as_of=NOW,
        retained=_state(
            schema_revision="0073_live_preflight",
            research_agenda_ids=(),
            research_proposal_awaiting_review_ids=("proposal-review",),
            research_approved_proposal_ids=("proposal-approved",),
        ),
    )
    task = next(
        item
        for item in due.tasks
        if item.task_type is OperatingTaskType.RESEARCH_OPERATIONS_REFRESH
    )
    assert task.disposition is DueDisposition.DUE
    assert task.network_behavior == ("RETAINED_EVIDENCE_ONLY_NO_AUTOMATIC_EXPERIMENT_EXECUTION")
    source = Path("src/apex/adapters/operating_cycle_tasks.py").read_text()
    assert "RefreshResearchAgenda" in source
    assert "GenerateResearchProposals" in source
    assert "RunGovernedResearchExperiment" not in source


def test_all_tasks_due_and_downstream_rebuild_after_source_change() -> None:
    retained = _state(
        crypto_universe_created_at=NOW - timedelta(days=7),
        crypto_universe_refresh_supported=True,
        crypto_pulse_as_of=NOW - timedelta(days=1),
        total_market_source_batch_id="batch-old",
        kalshi_latest_at=NOW - timedelta(minutes=10),
        kalshi_refresh_supported=True,
        options_capture_due_status="DUE",
        shared_crypto_pulse_id="pulse-old",
        shared_kalshi_evaluation_ids=("kalshi-old",),
        enriched_shared_set_id="shared-old",
        enriched_context_snapshot_id="context-old",
        latest_brief_as_of=NOW - timedelta(days=1),
        mandate_lineage_has_any_version=True,
        active_mandate_id="mandate-active-1",
        active_mandate_lineage_id="default",
        active_mandate_version=1,
        active_mandate_hash="m" * 64,
    )
    due = plan_due_work(plan=_plan(), cycle_id="cycle-all-due", as_of=NOW, retained=retained)
    dispositions = {item.task_type.value: item.disposition for item in due.tasks}
    assert all(
        disposition is DueDisposition.DUE
        for task_type, disposition in dispositions.items()
        if task_type not in {"PERFORMANCE_ATTRIBUTION_REBUILD", "COUNTERFACTUAL_FOLLOWUP_REFRESH"}
    )
    assert dispositions["PERFORMANCE_ATTRIBUTION_REBUILD"] is DueDisposition.NOT_DUE
    # Scheduling/Operational Priority for Recurring Follow-Up vs.
    # Time-Critical Options Capture: ``options_capture_due_status="DUE"``
    # above means the options window is open right now -- opportunistic
    # follow-up correctly yields instead of also being DUE, never BLOCKED
    # (nothing is wrong; it simply resumes next cycle).
    assert dispositions["COUNTERFACTUAL_FOLLOWUP_REFRESH"] is DueDisposition.NOT_DUE
    followup_task = next(
        item
        for item in due.tasks
        if item.task_type is OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH
    )
    assert followup_task.reasons == (COUNTERFACTUAL_FOLLOWUP_DEFERRED_FOR_OPTIONS_PRIORITY,)


def test_performance_attribution_is_due_only_for_changed_retained_paper_evidence() -> None:
    due = plan_due_work(
        plan=_plan(),
        cycle_id="cycle-paper-evidence",
        as_of=NOW,
        retained=_state(
            paper_evidence_ids=("alpaca-instruction-1",),
            paper_evidence_hash="a" * 64,
            unattributed_paper_evidence_ids=("alpaca-instruction-1",),
        ),
    )
    task = next(
        item
        for item in due.tasks
        if item.task_type is OperatingTaskType.PERFORMANCE_ATTRIBUTION_REBUILD
    )
    assert task.disposition is DueDisposition.DUE
    assert task.network_behavior == "RETAINED_PAPER_EVIDENCE_ONLY_ZERO_PROVIDER_REQUESTS"
    assert task.dependency_types == ()


def test_unavailable_source_blocks_only_dependents_and_brief_remains_due() -> None:
    retained = _state(
        crypto_universe_created_at=NOW - timedelta(days=8),
        crypto_pulse_as_of=NOW - timedelta(days=1),
        kalshi_latest_at=NOW - timedelta(minutes=10),
        latest_brief_as_of=NOW - timedelta(days=1),
    )
    due = plan_due_work(plan=_plan(), cycle_id="cycle-blocked", as_of=NOW, retained=retained)
    dispositions = {item.task_type.value: item.disposition for item in due.tasks}
    assert dispositions["CRYPTO_UNIVERSE_REFRESH"] is DueDisposition.UNAVAILABLE
    assert dispositions["CRYPTO_MARKET_PULSE"] is DueDisposition.BLOCKED
    assert dispositions["TOTAL_MARKET_CONTEXT_REFRESH"] is DueDisposition.NOT_DUE
    assert dispositions["OPERATING_BRIEF_BUILD"] is DueDisposition.DUE


def test_brief_and_health_are_deterministic_and_authority_free() -> None:
    due = plan_due_work(plan=_plan(), cycle_id="cycle-summary", as_of=NOW, retained=_state())
    task = due.tasks[0]
    result = OperatingTaskResult.create(
        cycle_id="cycle-summary",
        task=task,
        execution=TaskExecution(
            OperatingTaskStatus.UNAVAILABLE,
            (),
            task.source_evidence_ids,
            False,
            0,
            ("NO_PROVIDER_CAPABILITY",),
        ),
        started_at=NOW,
        completed_at=NOW,
    )
    brief = build_operating_brief(
        cycle_id="cycle-summary",
        as_of=NOW,
        retained=_state(),
        task_results=(result,),
        total_market_state="PARTIAL_CONTEXT",
        opportunity_counts=(("ELIGIBLE", 1), ("REJECTED", 2)),
        unallocated_cash="750",
        largest_crypto_score_changes=("BTC:0.1",),
    )
    assert brief == build_operating_brief(
        cycle_id="cycle-summary",
        as_of=NOW,
        retained=_state(),
        task_results=(result,),
        total_market_state="PARTIAL_CONTEXT",
        opportunity_counts=(("ELIGIBLE", 1), ("REJECTED", 2)),
        unallocated_cash="750",
        largest_crypto_score_changes=("BTC:0.1",),
    )
    health = build_operating_health(
        cycle_id="cycle-summary",
        as_of=NOW,
        retained=_state(),
        task_results=(result,),
        control_state=OperatingControlState.ENABLED,
    )
    assert health.state is OperatingHealthState.DEGRADED
    assert health.authority == health.capital_authority == health.order_authority == "NONE"
    assert terminal_cycle_state((result,)).value == "COMPLETED_WITH_WARNINGS"


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        (NOW - timedelta(days=6, hours=23, minutes=59), DueDisposition.NOT_DUE),
        (NOW - timedelta(days=7), DueDisposition.UNAVAILABLE),
    ],
)
def test_crypto_universe_seven_day_boundary(created_at: datetime, expected: DueDisposition) -> None:
    due = plan_due_work(
        plan=_plan(),
        cycle_id=f"cycle-boundary-{expected.value.lower()}",
        as_of=NOW,
        retained=_state(crypto_universe_created_at=created_at),
    )
    assert due.tasks[0].disposition is expected


def test_exact_cli_and_read_only_api_surface() -> None:
    from apex.cli import parser
    from apex.operator_api import create_app

    command_names = {
        name
        for action in parser()._actions
        if action.dest == "command" and action.choices is not None
        for name in action.choices
    }
    assert {
        "operating-plan-inspect",
        "operating-due-plan",
        "operating-cycle-execute",
        "operating-cycle-replay",
        "operating-cycle-inspect",
        "operating-task-inspect",
        "operating-cycle-pause",
        "operating-cycle-resume",
        "operating-brief-inspect",
        "operating-health-inspect",
    }.issubset(command_names)
    routes = {
        (route.path, tuple(sorted(getattr(route, "methods", ()) or ())))
        for route in create_app().routes
    }
    assert ("/api/v1/operating-cycle/workspace", ("GET",)) in routes
    assert ("/api/v1/operating-cycle/cycles/{cycle_id}", ("GET",)) in routes


def test_existing_executor_delegates_current_crypto_and_kalshi_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    executor = ExistingCapabilityOperatingTaskExecutor(engine, SimpleNamespace())  # type: ignore[arg-type]
    plan = _plan()
    calls: dict[str, object] = {}

    class FakePulseRefresh:
        def __init__(self, *_: object) -> None:
            pass

        def execute(self, **_: object) -> object:
            return SimpleNamespace(
                refresh=SimpleNamespace(refresh_id="cycle-current-crypto-pulse"),
                pulse=SimpleNamespace(pulse_id="current-pulse"),
                summary=None,
                state=RefreshState.COMPLETED,
                checkpoints=(),
                failures=(),
                network_requests_performed=0,
            )

    class FakeCurrentCrypto:
        def __init__(self, _: object) -> None:
            pass

        def execute(self, **values: object) -> object:
            calls["crypto"] = values
            return SimpleNamespace(
                run=SimpleNamespace(run_id=values["run_id"]),
                opportunities=(
                    SimpleNamespace(
                        opportunity_id=f"{values['run_id']}-opportunity",
                        status=OpportunityStatus.REJECTED,
                    ),
                ),
            )

    class FakeHistoryCapture:
        def __init__(self, *_: object) -> None:
            pass

        def execute(self, **values: object) -> object:
            calls["history"] = values
            return SimpleNamespace(
                observations=(SimpleNamespace(observation_id="current-history"),),
                request_count=30,
                idempotent_replay=False,
            )

    monkeypatch.setattr(operating_tasks, "ExecuteCryptoMarketPulseRefresh", FakePulseRefresh)
    monkeypatch.setattr(operating_tasks, "RunCurrentCryptoRelativeStrength", FakeCurrentCrypto)
    monkeypatch.setattr(operating_tasks, "RunCurrentCryptoReversalRegime", FakeCurrentCrypto)
    monkeypatch.setattr(operating_tasks, "CaptureCryptoCalibrationHistory", FakeHistoryCapture)
    executor._crypto = SimpleNamespace(  # type: ignore[assignment]
        refresh_plan=lambda _: SimpleNamespace(
            plan_id="current-crypto-pulse-plan", version=1, universe_snapshot_id="current-universe"
        ),
        universe_snapshot=lambda _: SimpleNamespace(
            snapshot_id="current-universe",
            source_capture_id="current-products",
            eligible_product_ids=("BTC-USD",),
            evidence_class=SimpleNamespace(),
        ),
        observations=lambda: (),
    )
    executor._crypto_stateful = SimpleNamespace(  # type: ignore[assignment]
        specifications=lambda: (),
        experiment_runs=lambda: (),
    )
    crypto = executor.execute(
        task=operating_tasks.DueTask.create(
            task_type=OperatingTaskType.CRYPTO_MARKET_PULSE,
            disposition=DueDisposition.DUE,
            reasons=("DAILY_CRYPTO_PULSE_DUE",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id="cycle-current",
            network_behavior="BOUNDED_PUBLIC_GETS",
        ),
        cycle_id="cycle-current",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert calls["crypto"] == {
        "run_id": "cycle-current-crypto-reversal-regime-v1",
        "pulse_id": "current-pulse",
        "generated_at": NOW,
    }
    assert calls["history"] == {
        "history_id": "current-universe-calibration-20260730-v1",
        "universe_snapshot_id": "current-universe",
        "interval_start": datetime(2024, 7, 30, tzinfo=UTC),
        "interval_end": datetime(2026, 7, 30, tzinfo=UTC),
        "requested_at": NOW,
    }
    assert "current-history" in crypto.output_identities
    assert crypto.network_requests == 30
    assert "CRYPTO_CALIBRATION_HISTORY_CAPTURED" in crypto.warnings
    # The bound pulse plan is version 1 (no CURRENT_PUBLIC_ORDER_BOOK request),
    # so PUBLIC_BOOK_CHECKPOINTS:0 must be traceable to that exact plan
    # binding on the retained task result itself, not left as a bare zero.
    assert "PUBLIC_BOOK_CHECKPOINTS:0" in crypto.warnings
    assert (
        "PUBLIC_BOOK_CAPTURE_NOT_REQUESTED_BY_BOUND_PULSE_PLAN:current-crypto-pulse-plan:v1"
        in crypto.warnings
    )
    assert "cycle-current-crypto-relative-strength-v1" in crypto.output_identities
    assert "cycle-current-crypto-reversal-regime-v1" in crypto.output_identities
    assert "cycle-current-crypto-relative-strength-v1-opportunity" in crypto.output_identities
    assert "cycle-current-crypto-reversal-regime-v1-opportunity" in crypto.output_identities
    assert "CRYPTO_CURRENT_STRATEGY_RETAINED_EVIDENCE_ONLY" in crypto.warnings

    class FakeKalshiRefresh:
        def __init__(self, **_: object) -> None:
            pass

        def execute(self, _: object) -> object:
            return SimpleNamespace(
                value=SimpleNamespace(
                    refresh_id="cycle-current-kalshi-refresh",
                    evaluation_ids=(),
                    warnings=(),
                ),
                idempotent_replay=False,
            )

    class FakeThresholdScan:
        def __init__(self, **_: object) -> None:
            pass

        def scan(self, request: object) -> object:
            calls["kalshi"] = request
            return SimpleNamespace(
                scan=SimpleNamespace(
                    scan_id=request.scan_id,
                    relationship_count=2,
                    candidate_count=0,
                ),
                evaluations=(SimpleNamespace(evaluation_id="related-evaluation"),),
            )

    class FakeRetainedSource:
        def __init__(self, _: object) -> None:
            pass

        def exhaustive_event_set_evaluations(self) -> tuple[object, ...]:
            return (
                SimpleNamespace(
                    evaluation_id="exhaustive-evaluation",
                    exhaustiveness_verified=True,
                    source_eligible=False,
                ),
            )

    capture_store = SimpleNamespace(select_capture_candidates=lambda **_: ())
    import apex.adapters.postgres as postgres

    monkeypatch.setattr(
        postgres, "postgres_bounded_orderbook_capture_store", lambda _: capture_store
    )
    monkeypatch.setattr(postgres, "postgres_public_binary_complement_store", lambda _: object())
    monkeypatch.setattr(postgres, "postgres_public_market_discovery_store", lambda _: object())
    monkeypatch.setattr(postgres, "postgres_public_threshold_ladder_store", lambda _: object())
    monkeypatch.setattr(operating_tasks, "ExecuteKalshiCurrentRefresh", FakeKalshiRefresh)
    monkeypatch.setattr(operating_tasks, "ScanPublicThresholdLadders", FakeThresholdScan)
    monkeypatch.setattr(operating_tasks, "RetainedSharedOpportunitySource", FakeRetainedSource)
    executor._kalshi = SimpleNamespace(  # type: ignore[assignment]
        latest_capability=lambda: SimpleNamespace(public_refresh_available=True),
        opportunities_for_refresh=lambda _: (),
    )
    kalshi = executor.execute(
        task=operating_tasks.DueTask.create(
            task_type=OperatingTaskType.KALSHI_MARKET_REFRESH,
            disposition=DueDisposition.DUE,
            reasons=("KALSHI_REFRESH_DUE",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id="cycle-current",
            network_behavior="BOUNDED_PUBLIC_GETS",
        ),
        cycle_id="cycle-current",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    request = calls["kalshi"]
    assert request.scan_id == "cycle-current-kalshi-related-market-v1"  # type: ignore[attr-defined]
    assert request.maximum_pages == 3  # type: ignore[attr-defined]
    assert request.event_count == 10  # type: ignore[attr-defined]
    assert request.market_limit == 150  # type: ignore[attr-defined]
    assert request.maximum_relationships == 100  # type: ignore[attr-defined]
    assert request.rounds == 2  # type: ignore[attr-defined]
    assert request.maximum_bundle_quantity == Decimal(10)  # type: ignore[attr-defined]
    assert "related-evaluation" in kalshi.output_identities
    assert "exhaustive-evaluation" in kalshi.output_identities
    assert "KALSHI_RELATED_RELATIONSHIPS:2" in kalshi.warnings
    assert "KALSHI_EXHAUSTIVE_EVENT_SETS:1" in kalshi.warnings
    assert kalshi.network_requests == 0
    engine.dispose()


class _MemoryRepository:
    def __init__(self, plan: OperatingCyclePlan) -> None:
        self.retained_plan = plan
        self.keys: dict[str, str] = {}
        self.controls: list[object] = []
        self.control_keys: dict[str, str] = {}
        self.paper_activations: list[object] = []
        self.paper_activation_keys: dict[str, str] = {}
        self.runs: dict[str, object] = {}
        self.due: dict[str, object] = {}
        self.events: dict[str, list[object]] = {}
        self.results: dict[str, object] = {}
        self.brief_values: dict[str, object] = {}
        self.health_values: dict[str, object] = {}

    def plan(self, plan_id: str) -> object | None:
        return self.retained_plan if plan_id == self.retained_plan.plan_id else None

    def plans(self) -> tuple[object, ...]:
        return (self.retained_plan,)

    def save_plan(self, **_: object) -> bool:
        return False

    def control_events(self, plan_id: str) -> tuple[object, ...]:
        return tuple(item for item in self.controls if item.plan_id == plan_id)  # type: ignore[attr-defined]

    def save_control_event(self, *, idempotency_key: str, event: object) -> bool:
        prior = self.control_keys.get(event.event_id)  # type: ignore[attr-defined]
        if prior is not None:
            return True
        self.controls.append(event)
        self.control_keys[event.event_id] = idempotency_key  # type: ignore[attr-defined]
        return False

    def paper_activation_events(self, plan_id: str) -> tuple[object, ...]:
        return tuple(item for item in self.paper_activations if item.plan_id == plan_id)  # type: ignore[attr-defined]

    def save_paper_activation_event(self, *, idempotency_key: str, event: object) -> bool:
        prior = self.paper_activation_keys.get(event.event_id)  # type: ignore[attr-defined]
        if prior is not None:
            return True
        self.paper_activations.append(event)
        self.paper_activation_keys[event.event_id] = idempotency_key  # type: ignore[attr-defined]
        return False

    def cycle(self, cycle_id: str) -> object | None:
        return self.runs.get(cycle_id)

    def cycle_idempotency_key(self, cycle_id: str) -> str | None:
        return self.keys.get(cycle_id)

    def cycle_for_plan_as_of(self, plan_id: str, as_of: datetime) -> object | None:
        return next(
            (
                value
                for value in self.runs.values()
                if value.plan_id == plan_id and value.as_of == as_of  # type: ignore[attr-defined]
            ),
            None,
        )

    def cycles(self) -> tuple[object, ...]:
        return tuple(self.runs.values())

    def save_cycle(self, *, idempotency_key: str, cycle: object, due_work: object) -> bool:
        self.runs[cycle.cycle_id] = cycle  # type: ignore[attr-defined]
        self.keys[cycle.cycle_id] = idempotency_key  # type: ignore[attr-defined]
        self.due[cycle.cycle_id] = due_work  # type: ignore[attr-defined]
        return False

    def due_work(self, cycle_id: str) -> object | None:
        return self.due.get(cycle_id)

    def cycle_events(self, cycle_id: str) -> tuple[object, ...]:
        return tuple(self.events.get(cycle_id, ()))

    def save_cycle_event(self, event: object) -> bool:
        self.events.setdefault(event.cycle_id, []).append(event)  # type: ignore[attr-defined]
        return False

    def task_results(self, cycle_id: str) -> tuple[object, ...]:
        return tuple(
            value
            for value in self.results.values()
            if value.cycle_id == cycle_id  # type: ignore[attr-defined]
        )

    def task_result(self, task_result_id: str) -> object | None:
        return self.results.get(task_result_id)

    def save_task_result(self, result: object) -> bool:
        self.results[result.task_result_id] = result  # type: ignore[attr-defined]
        return False

    def brief(self, brief_id: str) -> object | None:
        return self.brief_values.get(brief_id)

    def briefs(self) -> tuple[object, ...]:
        return tuple(self.brief_values.values())

    def save_brief(self, brief: object) -> bool:
        self.brief_values[brief.brief_id] = brief  # type: ignore[attr-defined]
        return False

    def health(self, health_id: str) -> object | None:
        return self.health_values.get(health_id)

    def health_summaries(self) -> tuple[object, ...]:
        return tuple(self.health_values.values())

    def save_health(self, health: object) -> bool:
        self.health_values[health.health_id] = health  # type: ignore[attr-defined]
        return False


class _StateSource:
    def __init__(self, retained: RetainedOperatingState) -> None:
        self.retained = retained
        self.evaluated_at_calls: list[datetime | None] = []

    def snapshot(
        self, *, as_of: datetime, evaluated_at: datetime | None = None
    ) -> RetainedOperatingState:
        self.evaluated_at_calls.append(evaluated_at)
        return self.retained

    def brief_inputs(
        self,
    ) -> tuple[str | None, tuple[tuple[str, int], ...], str | None, tuple[str, ...]]:
        return "PARTIAL_CONTEXT", (("ELIGIBLE", 0),), "1000", ()


class _FailingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, *, task: object, **_: object) -> TaskExecution:
        self.calls.append(task.task_type.value)  # type: ignore[attr-defined]
        if task.task_type.value == "CRYPTO_MARKET_PULSE":  # type: ignore[attr-defined]
            raise OSError("fixture source failure")
        return TaskExecution(OperatingTaskStatus.COMPLETED, ("fixture-output",), (), True, 0)


def test_partial_failure_replay_conflict_pause_and_resume() -> None:
    plan = _plan()
    retained = _state(
        crypto_universe_created_at=NOW - timedelta(days=7),
        crypto_universe_refresh_supported=True,
        crypto_pulse_as_of=NOW - timedelta(days=1),
        total_market_source_batch_id="batch-old",
        kalshi_latest_at=NOW - timedelta(minutes=10),
        kalshi_refresh_supported=True,
        shared_crypto_pulse_id="pulse-old",
        latest_brief_as_of=NOW - timedelta(days=1),
    )
    repository = _MemoryRepository(plan)
    source = _StateSource(retained)
    executor = _FailingExecutor()
    service = ExecuteOperatingCycle(repository, source, executor)  # type: ignore[arg-type]
    result = service.execute(
        cycle_id="cycle-partial-fixture",
        plan_id=plan.plan_id,
        as_of=NOW,
        executed_at=NOW,
        idempotency_key="cycle-partial-key",
    )
    assert result.state.value == "PARTIAL"
    assert any(item.status is OperatingTaskStatus.BLOCKED for item in result.tasks)
    calls = tuple(executor.calls)
    replay = service.execute(
        cycle_id="cycle-partial-fixture",
        plan_id=plan.plan_id,
        as_of=NOW,
        executed_at=NOW,
        idempotency_key="cycle-partial-key",
    )
    assert replay.idempotent_replay
    assert tuple(executor.calls) == calls
    with pytest.raises(ValueError, match="OPERATING_CYCLE_IDENTITY_CONFLICT"):
        service.execute(
            cycle_id="cycle-partial-fixture",
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key="conflicting-cycle-key",
        )

    control = SetOperatingCycleControl(repository)  # type: ignore[arg-type]
    control.execute(
        event_id="pause-fixture",
        plan_id=plan.plan_id,
        state=OperatingControlState.PAUSED,
        reason="fixture pause",
        actor="fixture",
        occurred_at=NOW,
        idempotency_key="pause-fixture-key",
    )
    paused_source = _StateSource(_state(captured_at=NOW + timedelta(minutes=1)))
    paused = ExecuteOperatingCycle(repository, paused_source, executor).execute(  # type: ignore[arg-type]
        cycle_id="cycle-paused-fixture",
        plan_id=plan.plan_id,
        as_of=NOW + timedelta(minutes=1),
        executed_at=NOW + timedelta(minutes=1),
        idempotency_key="cycle-paused-key",
    )
    assert paused.state.value == "SKIPPED_PAUSED"
    assert not paused.tasks
    control.execute(
        event_id="resume-fixture",
        plan_id=plan.plan_id,
        state=OperatingControlState.ENABLED,
        reason="fixture resume",
        actor="fixture",
        occurred_at=NOW + timedelta(minutes=2),
        idempotency_key="resume-fixture-key",
    )
    assert repository.controls[-1].state is OperatingControlState.ENABLED  # type: ignore[attr-defined]


_CRYPTO_PULSE_DEPENDENT_TASK_TYPES = (
    OperatingTaskType.SHARED_OPPORTUNITY_REBUILD,
    OperatingTaskType.CONTEXT_ENRICHMENT_REBUILD,
    OperatingTaskType.RESEARCH_ALLOCATION_REBUILD,
    OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION,
    OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION,
    OperatingTaskType.MANDATE_EVALUATION_REBUILD,
)


def _crypto_pulse_dependent_state() -> RetainedOperatingState:
    """Exactly the shape ``CryptoResearchConflict Root-Cause & Autonomous
    Recovery v1`` needs: every one of the six real 2026-08-16 dependents of
    ``CRYPTO_MARKET_PULSE`` (transitively, through ``SHARED_OPPORTUNITY_
    REBUILD``/``RESEARCH_ALLOCATION_REBUILD``) genuinely due, not merely
    ``SKIPPED_NOT_DUE`` -- so a BLOCKED assertion actually exercises
    ``_dispatch_task``'s dependency check rather than a due-disposition
    short-circuit that would pass for an unrelated reason."""

    return _state(
        crypto_universe_created_at=NOW - timedelta(days=7),
        crypto_universe_refresh_supported=True,
        crypto_pulse_as_of=NOW - timedelta(days=1),
        total_market_source_batch_id="batch-old",
        kalshi_latest_at=NOW - timedelta(minutes=10),
        kalshi_refresh_supported=True,
        shared_crypto_pulse_id="pulse-old",
        latest_brief_as_of=NOW - timedelta(days=1),
        # MANDATE_EVALUATION_REBUILD is UNAVAILABLE (not DUE) with no active
        # mandate at all -- an independent precondition unrelated to
        # ``CRYPTO_MARKET_PULSE`` -- so it must be authored here for that
        # dependent's BLOCKED/COMPLETED disposition below to actually
        # exercise the crypto-pulse dependency check rather than this
        # unrelated gate.
        mandate_lineage_has_any_version=True,
        active_mandate_id="mandate-1",
        active_mandate_lineage_id="default",
        active_mandate_version=1,
    )


def test_crypto_market_pulse_failure_blocks_exactly_its_six_real_dependents() -> None:
    """Part 7 (Dependency Cascade) of ``CryptoResearchConflict Root-Cause &
    Autonomous Recovery v1``: a genuine, uncaught ``CRYPTO_MARKET_PULSE``
    failure must still correctly block precisely the six task types the real
    2026-08-16 cycle showed ``OPERATING_TASK_DEPENDENCY_BLOCKED`` for -- this
    milestone's fix lives entirely inside the crypto persistence layer and
    must not touch ``_dispatch_task``'s generic dependency safety at all."""

    plan = _plan()
    repository = _MemoryRepository(plan)
    source = _StateSource(_crypto_pulse_dependent_state())
    executor = _FailingExecutor()
    result = ExecuteOperatingCycle(repository, source, executor).execute(  # type: ignore[arg-type]
        cycle_id="cycle-crypto-pulse-dependents-blocked",
        plan_id=plan.plan_id,
        as_of=NOW,
        executed_at=NOW,
        idempotency_key="cycle-crypto-pulse-dependents-blocked-key",
    )
    by_type = {item.task_type: item for item in result.tasks}
    pulse_result = by_type[OperatingTaskType.CRYPTO_MARKET_PULSE]
    assert pulse_result.status is OperatingTaskStatus.FAILED
    assert pulse_result.error_code == "OSError"
    for task_type in _CRYPTO_PULSE_DEPENDENT_TASK_TYPES:
        dependent = by_type[task_type]
        assert dependent.status is OperatingTaskStatus.BLOCKED, task_type
        assert dependent.error_code == "OPERATING_TASK_DEPENDENCY_BLOCKED", task_type
        assert "DEPENDENCY_FAILED" in dependent.warning_codes, task_type


def test_crypto_market_pulse_success_lets_its_six_real_dependents_proceed() -> None:
    """The other half of the same proof: once ``CRYPTO_MARKET_PULSE``
    genuinely COMPLETEs (exactly what migration 0100 makes true for the
    retained 2026-08-16 shape), none of its six real dependents is BLOCKED
    by a stale ``DEPENDENCY_FAILED`` -- every one of them actually ran."""

    plan = _plan()
    repository = _MemoryRepository(plan)
    source = _StateSource(_crypto_pulse_dependent_state())
    executor = _AllCompleteExecutor()
    result = ExecuteOperatingCycle(repository, source, executor).execute(  # type: ignore[arg-type]
        cycle_id="cycle-crypto-pulse-dependents-unblocked",
        plan_id=plan.plan_id,
        as_of=NOW,
        executed_at=NOW,
        idempotency_key="cycle-crypto-pulse-dependents-unblocked-key",
    )
    by_type = {item.task_type: item for item in result.tasks}
    assert by_type[OperatingTaskType.CRYPTO_MARKET_PULSE].status is OperatingTaskStatus.COMPLETED
    for task_type in _CRYPTO_PULSE_DEPENDENT_TASK_TYPES:
        dependent = by_type[task_type]
        assert dependent.status is OperatingTaskStatus.COMPLETED, task_type
        assert dependent.error_code != "OPERATING_TASK_DEPENDENCY_BLOCKED", task_type


def test_set_operating_cycle_control_exact_replay_reuses_ordinal() -> None:
    """SetOperatingCycleControl retained the same recomputed-ordinal defect that
    was fixed in SetPaperActivation: it always derived a new event's ordinal
    from ``len(events) + 1`` even on an exact replay of an already-retained
    event_id, which changed that replay's canonical_hash and false-positived it
    as a conflict.  This proves the same retained-event replay pattern now
    holds for pause/resume control events too. (Conflict rejection on a
    genuinely mismatched replay is proven at the PostgreSQL persistence layer,
    matching how the sibling SetPaperActivation fixture/persistence tests
    split that coverage.)"""

    plan = _plan()
    repository = _MemoryRepository(plan)
    control = SetOperatingCycleControl(repository)  # type: ignore[arg-type]

    paused, replay = control.execute(
        event_id="control-replay-pause-v1",
        plan_id=plan.plan_id,
        state=OperatingControlState.PAUSED,
        reason="operator pause",
        actor="fixture-operator",
        occurred_at=NOW,
        idempotency_key="control-replay-pause-key",
    )
    assert not replay
    assert paused.ordinal == 1

    # Exact replay: same event_id, same payload, same idempotency key must
    # return the retained event unchanged -- no recomputed ordinal, no
    # duplicate row.
    replayed_paused, replayed = control.execute(
        event_id="control-replay-pause-v1",
        plan_id=plan.plan_id,
        state=OperatingControlState.PAUSED,
        reason="operator pause",
        actor="fixture-operator",
        occurred_at=NOW,
        idempotency_key="control-replay-pause-key",
    )
    assert replayed
    assert replayed_paused == paused
    assert replayed_paused.ordinal == 1
    assert len(repository.control_events(plan.plan_id)) == 1

    # A later, genuinely distinct resume event still receives the correct next
    # ordinal after the replay.
    resumed, replay = control.execute(
        event_id="control-replay-resume-v1",
        plan_id=plan.plan_id,
        state=OperatingControlState.ENABLED,
        reason="operator resume",
        actor="fixture-operator",
        occurred_at=NOW + timedelta(minutes=1),
        idempotency_key="control-replay-resume-key",
    )
    assert not replay
    assert resumed.ordinal == 2
    assert len(repository.control_events(plan.plan_id)) == 2


def test_paper_activation_event_requires_positive_ordinal_and_exact_identities() -> None:
    event = PaperActivationEvent.create(
        event_id="paper-activation-fixture",
        plan_id="operating-plan-v1",
        ordinal=1,
        state=PaperActivationState.ENABLED,
        reason="genuine activation acceptance",
        actor="fixture-operator",
        occurred_at=NOW,
    )
    assert event.state is PaperActivationState.ENABLED
    assert event.authority == "NONE"
    with pytest.raises(OperatingCycleError, match="OPERATING_PAPER_ACTIVATION_ORDINAL_INVALID"):
        PaperActivationEvent.create(
            event_id="paper-activation-bad-ordinal",
            plan_id="operating-plan-v1",
            ordinal=0,
            state=PaperActivationState.ENABLED,
            reason="genuine activation acceptance",
            actor="fixture-operator",
            occurred_at=NOW,
        )
    with pytest.raises(OperatingCycleError, match="OPERATING_PAPER_ACTIVATION_REASON_INVALID"):
        PaperActivationEvent.create(
            event_id="paper-activation-bad-reason",
            plan_id="operating-plan-v1",
            ordinal=1,
            state=PaperActivationState.ENABLED,
            reason="   ",
            actor="fixture-operator",
            occurred_at=NOW,
        )


def test_effective_paper_activation_folds_latest_event_defaulting_disabled() -> None:
    assert effective_paper_activation(()) is False
    enabled = PaperActivationEvent.create(
        event_id="activate-1",
        plan_id="operating-plan-v1",
        ordinal=1,
        state=PaperActivationState.ENABLED,
        reason="begin bounded genuine PAPER evidence accumulation",
        actor="fixture-operator",
        occurred_at=NOW,
    )
    assert effective_paper_activation((enabled,)) is True
    disabled = PaperActivationEvent.create(
        event_id="deactivate-1",
        plan_id="operating-plan-v1",
        ordinal=2,
        state=PaperActivationState.DISABLED,
        reason="pause pending review",
        actor="fixture-operator",
        occurred_at=NOW + timedelta(hours=1),
    )
    # The latest retained event always wins, in either direction.
    assert effective_paper_activation((enabled, disabled)) is False
    reenabled = PaperActivationEvent.create(
        event_id="activate-2",
        plan_id="operating-plan-v1",
        ordinal=3,
        state=PaperActivationState.ENABLED,
        reason="resume after review",
        actor="fixture-operator",
        occurred_at=NOW + timedelta(hours=2),
    )
    assert effective_paper_activation((enabled, disabled, reenabled)) is True


def test_set_paper_activation_enables_disables_and_stays_scoped_to_exact_plan() -> None:
    plan = _plan()
    repository = _MemoryRepository(plan)
    service = SetPaperActivation(repository)  # type: ignore[arg-type]

    activation, replay = service.execute(
        event_id="paper-activate-fixture",
        plan_id=plan.plan_id,
        state=PaperActivationState.ENABLED,
        reason="begin bounded genuine PAPER evidence accumulation",
        actor="fixture-operator",
        occurred_at=NOW,
        idempotency_key="paper-activate-fixture-key",
    )
    assert not replay
    assert activation.ordinal == 1
    assert effective_paper_activation(repository.paper_activation_events(plan.plan_id)) is True
    # A different plan_id must never observe this plan's activation evidence.
    assert repository.paper_activation_events("some-other-plan") == ()

    deactivation, replay = service.execute(
        event_id="paper-deactivate-fixture",
        plan_id=plan.plan_id,
        state=PaperActivationState.DISABLED,
        reason="pause pending operator review",
        actor="fixture-operator",
        occurred_at=NOW + timedelta(hours=1),
        idempotency_key="paper-deactivate-fixture-key",
    )
    assert not replay
    assert deactivation.ordinal == 2
    assert effective_paper_activation(repository.paper_activation_events(plan.plan_id)) is False

    with pytest.raises(OperatingCycleError, match="OPERATING_PLAN_NOT_FOUND"):
        service.execute(
            event_id="paper-activate-unknown-plan",
            plan_id="plan-does-not-exist",
            state=PaperActivationState.ENABLED,
            reason="should never be accepted",
            actor="fixture-operator",
            occurred_at=NOW,
            idempotency_key="paper-activate-unknown-plan-key",
        )


def test_execute_operating_cycle_folds_paper_activation_into_executor_plan() -> None:
    """Proves the new fold: ExecuteOperatingCycle never mutates or replaces the
    retained plan, but the *effective* plan.paper_eligible it hands to the task
    executor reflects the latest retained PaperActivationEvent for that exact
    plan_id -- False until an operator explicitly enables it, matching exactly
    what evaluate_paper_eligibility / KalshiPaperEligibility.evaluate read as
    operating_cycle_enabled."""

    plan = _plan()
    retained = _state()
    repository = _MemoryRepository(plan)
    source = _StateSource(retained)

    seen_plans: list[object] = []

    class _CapturingExecutor:
        def execute(self, *, task: object, plan: object, **_: object) -> TaskExecution:
            seen_plans.append(plan)
            return TaskExecution(OperatingTaskStatus.COMPLETED, ("fixture-output",), (), True, 0)

    executor = _CapturingExecutor()
    service = ExecuteOperatingCycle(repository, source, executor)  # type: ignore[arg-type]

    first = service.execute(
        cycle_id="cycle-paper-inactive",
        plan_id=plan.plan_id,
        as_of=NOW,
        executed_at=NOW,
        idempotency_key="cycle-paper-inactive-key",
    )
    assert first.state.value in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}
    assert seen_plans, "the executor must have been invoked for at least one due task"
    assert all(item.paper_eligible is False for item in seen_plans)  # type: ignore[attr-defined]
    assert first.health.paper_eligible is False
    # Retained plan identity/hash are untouched by the fold -- only the
    # in-memory effective copy handed to the executor differs.
    assert repository.retained_plan.paper_eligible is False

    SetPaperActivation(repository).execute(  # type: ignore[arg-type]
        event_id="paper-activate-for-cycle",
        plan_id=plan.plan_id,
        state=PaperActivationState.ENABLED,
        reason="begin bounded genuine PAPER evidence accumulation",
        actor="fixture-operator",
        occurred_at=NOW,
        idempotency_key="paper-activate-for-cycle-key",
    )
    seen_plans.clear()
    later = NOW + timedelta(minutes=1)
    later_service = ExecuteOperatingCycle(  # type: ignore[arg-type]
        repository, _StateSource(_state(captured_at=later)), executor
    )
    second = later_service.execute(
        cycle_id="cycle-paper-active",
        plan_id=plan.plan_id,
        as_of=later,
        executed_at=later,
        idempotency_key="cycle-paper-active-key",
    )
    assert seen_plans
    assert all(item.paper_eligible is True for item in seen_plans)  # type: ignore[attr-defined]
    assert second.health.paper_eligible is True
    assert repository.retained_plan.paper_eligible is False


def test_operating_paper_activation_cli_and_api_surface_registered() -> None:
    from apex.cli import parser
    from apex.operator_api import create_app

    command_names = {
        name
        for action in parser()._actions
        if action.dest == "command" and action.choices is not None
        for name in action.choices
    }
    assert {
        "operating-paper-activate",
        "operating-paper-deactivate",
        "operating-paper-activation-inspect",
    }.issubset(command_names)
    routes = {
        (route.path, tuple(sorted(getattr(route, "methods", ()) or ())))
        for route in create_app().routes
    }
    assert ("/api/v1/operating-cycle/plans/{plan_id}/paper-activation", ("POST",)) in routes
    assert ("/api/v1/operating-cycle/plans/{plan_id}", ("GET",)) in routes


def test_operating_cycle_worker_cli_surface_registered() -> None:
    """The supervised worker composes the existing exact planner and the
    existing SupervisedOperatingRunner lease/heartbeat machinery -- no new
    scheduler, daemon, or lease mechanism -- so it must be reachable through
    the same exact CLI surface as every other operating-cycle command."""

    from apex.cli import parser

    command_names = {
        name
        for action in parser()._actions
        if action.dest == "command" and action.choices is not None
        for name in action.choices
    }
    assert "operating-cycle-worker" in command_names


def test_operating_cycle_worker_rejects_unknown_plan_before_touching_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apex.cli import _run_supervised_operating_cycle_worker

    # Migration Compatibility & Deployment Guard v1 added a schema-
    # compatibility check that now runs before this one -- see
    # tests/test_schema_compatibility.py for its own dedicated ordering
    # proofs. Stubbed out here (engine=None below has nothing real to
    # check against) so this test keeps proving what it always proved:
    # the plan-existence check, one statement later.
    monkeypatch.setattr(
        "apex.adapters.schema_compatibility_postgres.require_compatible_schema",
        lambda engine, *, worker_name: None,
    )

    class _NoPlanRepository:
        def plan(self, plan_id: str) -> object | None:
            return None

    with pytest.raises(ValueError, match="OPERATING_PLAN_NOT_FOUND"):
        _run_supervised_operating_cycle_worker(
            engine=None,
            repository=_NoPlanRepository(),
            source=None,
            plan_id="plan-does-not-exist",
            runner_id="runner-fixture",
            run_instance_id="runner-fixture-instance-1",
            once=True,
            poll_seconds=150,
            max_cycles=1,
            authority={},
        )


@pytest.mark.parametrize(
    ("poll_seconds", "max_cycles"),
    [
        (59, 1),
        (181, 1),  # above SupervisedOperatingRunner.LEASE_SECONDS (180)
        (150, 0),
        (150, 101),
    ],
)
def test_operating_cycle_worker_rejects_out_of_bound_arguments(
    poll_seconds: int, max_cycles: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apex.cli import _run_supervised_operating_cycle_worker

    # See the identical comment in
    # test_operating_cycle_worker_rejects_unknown_plan_before_touching_executor
    # above -- this keeps testing the pre-existing bounds-validation
    # ordering, now one statement further into the function.
    monkeypatch.setattr(
        "apex.adapters.schema_compatibility_postgres.require_compatible_schema",
        lambda engine, *, worker_name: None,
    )

    class _KnownPlanRepository:
        def plan(self, plan_id: str) -> object | None:
            return object()

    with pytest.raises(ValueError, match="operating cycle worker"):
        _run_supervised_operating_cycle_worker(
            engine=None,
            repository=_KnownPlanRepository(),
            source=None,
            plan_id="plan-fixture",
            runner_id="runner-fixture",
            run_instance_id="runner-fixture-instance-1",
            once=True,
            poll_seconds=poll_seconds,
            max_cycles=max_cycles,
            authority={},
        )


def test_operating_daily_checkpoint_cli_surface_registered() -> None:
    """The daily checkpoint is a read-only composition over existing retained
    surfaces reached through the same exact-ID CLI pattern as every other
    operating-cycle command -- no new dashboard."""

    from apex.cli import parser

    command_names = {
        name
        for action in parser()._actions
        if action.dest == "command" and action.choices is not None
        for name in action.choices
    }
    assert "operating-daily-checkpoint-inspect" in command_names

    checkpoint_parser = next(
        action
        for action in parser()._actions
        if action.dest == "command" and action.choices is not None
    ).choices["operating-daily-checkpoint-inspect"]
    required = {
        action.option_strings[0] for action in checkpoint_parser._actions if action.required
    }
    assert required == {"--plan-id", "--cycle-id"}


def test_operating_daily_checkpoint_rejects_unknown_plan_before_touching_cycle() -> None:
    from apex.cli import _build_operating_daily_checkpoint

    class _NoPlanRepository:
        def plan(self, plan_id: str) -> object | None:
            return None

    with pytest.raises(ValueError, match="OPERATING_PLAN_NOT_FOUND"):
        _build_operating_daily_checkpoint(
            engine=None,
            repository=_NoPlanRepository(),
            plan_id="plan-does-not-exist",
            cycle_id="cycle-fixture",
            runner_id="runner-fixture",
        )


def test_operating_daily_checkpoint_rejects_cycle_from_a_different_plan() -> None:
    """A cycle_id that exists but belongs to a different plan_id must fail
    closed rather than silently reporting another plan's evidence -- the same
    exact-plan-and-cycle scoping the rest of this milestone relies on."""

    from apex.cli import _build_operating_daily_checkpoint

    class _MismatchedCycle:
        plan_id = "some-other-plan"

    class _CrossPlanRepository:
        def plan(self, plan_id: str) -> object | None:
            return SimpleNamespace(plan_id=plan_id)

        def cycle(self, cycle_id: str) -> object | None:
            return _MismatchedCycle()

    with pytest.raises(ValueError, match="OPERATING_CYCLE_NOT_FOUND_FOR_EXACT_PLAN"):
        _build_operating_daily_checkpoint(
            engine=None,
            repository=_CrossPlanRepository(),
            plan_id="plan-fixture",
            cycle_id="cycle-belongs-elsewhere",
            runner_id="runner-fixture",
        )


# --------------------------------------------------------------------------
# Operating Daily Checkpoint Inspect Canonicalization Fix v1: the checkpoint
# crashed on every real invocation with ``ValueValidationError: canonical
# data cannot contain binary floats``. Root cause: ``coverage_percentage``
# was computed as ``round(100 * observed / considered, 2)`` -- ``observed``/
# ``considered`` are plain ``int`` counts, and ``int / int`` is true,
# binary-float division in Python 3, so this presentation-only figure was a
# genuine Python ``float`` by the time ``_print_autopilot`` tried to render
# it through the strict, unweakened ``canonical_json_bytes``. Fixed by
# ``_followup_coverage_percentage``, which never constructs a float at all.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("observed", "considered", "expected"),
    (
        (11, 1666, Decimal("0.66")),  # the exact retained values that crashed production
        (0, 5, Decimal("0.00")),
        (5, 5, Decimal("100.00")),
        (1, 3, Decimal("33.33")),
    ),
)
def test_followup_coverage_percentage_returns_authoritative_decimal(
    observed: int, considered: int, expected: Decimal
) -> None:
    from apex.cli import _followup_coverage_percentage

    result = _followup_coverage_percentage(observed=observed, considered=considered)
    assert isinstance(result, Decimal)
    assert not isinstance(result, float)
    assert result == expected


def test_followup_coverage_percentage_returns_none_when_nothing_considered() -> None:
    from apex.cli import _followup_coverage_percentage

    assert _followup_coverage_percentage(observed=0, considered=0) is None


def test_followup_coverage_percentage_preserves_precision_of_the_original_formula() -> None:
    """The fix changes *type* (float -> Decimal), never the rounded value,
    for realistic inputs: cross-check against the original
    ``round(100 * observed / considered, 2)`` formula, executed literally,
    across a spread of non-adversarial (observed, considered) pairs."""

    from apex.cli import _followup_coverage_percentage

    for observed, considered in ((11, 1666), (46, 71), (25, 1666), (1, 7), (3, 8), (0, 40)):
        original_float_value = round(100 * observed / considered, 2)
        fixed_value = _followup_coverage_percentage(observed=observed, considered=considered)
        assert fixed_value == Decimal(str(original_float_value))


def test_followup_coverage_percentage_is_deterministic_across_calls() -> None:
    from apex.cli import _followup_coverage_percentage
    from apex.domain.values import canonical_json_bytes

    first = _followup_coverage_percentage(observed=11, considered=1666)
    second = _followup_coverage_percentage(observed=11, considered=1666)
    assert first == second
    assert canonical_json_bytes({"v": first}) == canonical_json_bytes({"v": second})


def test_old_checkpoint_shape_with_a_bare_binary_float_crashes_canonical_json_bytes() -> None:
    """Reproduces the exact real production failure: a fixture shaped like
    ``_build_operating_daily_checkpoint``'s own ``counterfactual_followup``
    section, with ``coverage_percentage`` set to the real retained value
    this milestone found in production (``round(100 * 11 / 1666, 2) ==
    0.66``, a genuine Python ``float``). Proves the *old* shape genuinely
    violates the canonical-data contract via the real
    ``canonical_json_bytes``/``_canonicalize`` code path -- the same one
    ``_print_autopilot`` uses to render every CLI command's output."""

    from apex.domain.values import ValueValidationError, canonical_json_bytes

    offending_value = round(100 * 11 / 1666, 2)
    assert isinstance(offending_value, float)  # the actual retained-shape defect

    old_shape_checkpoint = {
        "checkpoint_id": "fixture-daily-checkpoint",
        "counterfactual_followup": {
            "policy_version": "FOLLOWUP_POLICY_V2",
            "total_pairs": 1666,
            "observed_pairs": 11,
            "coverage_percentage": offending_value,
        },
    }
    with pytest.raises(ValueValidationError, match="canonical data cannot contain binary floats"):
        canonical_json_bytes(old_shape_checkpoint)


def test_fixed_checkpoint_shape_with_authoritative_decimal_succeeds() -> None:
    """Same real fixture shape as above, with the fix applied
    (``_followup_coverage_percentage`` instead of raw float division) --
    must succeed, must render the same human-readable percentage (``0.66``)
    the broken float would have shown, and must do so deterministically."""

    from apex.cli import _followup_coverage_percentage
    from apex.domain.values import canonical_json_bytes

    fixed_shape_checkpoint = {
        "checkpoint_id": "fixture-daily-checkpoint",
        "counterfactual_followup": {
            "policy_version": "FOLLOWUP_POLICY_V2",
            "total_pairs": 1666,
            "observed_pairs": 11,
            "coverage_percentage": _followup_coverage_percentage(observed=11, considered=1666),
        },
    }
    rendered = canonical_json_bytes(fixed_shape_checkpoint)
    assert b'"coverage_percentage":"0.66"' in rendered
    assert rendered == canonical_json_bytes(fixed_shape_checkpoint)


# --------------------------------------------------------------------------
# OPTIONS_RESEARCH_EVIDENCE_CAPTURE: due-disposition mapping and operating
# separation from crypto/Kalshi (Fresh Options Evidence Operations and
# Structure Research Baseline v1)
# --------------------------------------------------------------------------


def _options_task(due: object) -> object:
    return next(
        item
        for item in due.tasks
        if item.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE
    )


class TestOptionsResearchEvidenceCaptureDueMapping:
    def test_due_status_due_maps_to_due_disposition(self) -> None:
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-options-due",
            as_of=NOW,
            retained=_state(
                options_capture_due_status="DUE",
                options_capture_plan_id="options-plan-1",
                options_capture_session_id="2026-07-30",
            ),
        )
        task = _options_task(due)
        assert task.disposition is DueDisposition.DUE
        assert "DUE" in task.reasons
        assert task.dependency_types == ()

    def test_due_status_unavailable_maps_to_unavailable_disposition(self) -> None:
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-options-unavailable",
            as_of=NOW,
            retained=_state(options_capture_due_status="UNAVAILABLE"),
        )
        task = _options_task(due)
        assert task.disposition is DueDisposition.UNAVAILABLE

    def test_never_computed_maps_to_not_due_not_a_crash(self) -> None:
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-options-uncomputed",
            as_of=NOW,
            retained=_state(options_capture_due_status=None),
        )
        task = _options_task(due)
        assert task.disposition is DueDisposition.NOT_DUE
        assert task.reasons == ("OPTIONS_CAPTURE_DUE_STATE_NOT_YET_RESOLVED",)

    @pytest.mark.parametrize("status", ["MISSED_CAPTURE_WINDOW", "ALREADY_CAPTURED", "NOT_DUE"])
    def test_other_statuses_map_to_not_due(self, status: str) -> None:
        due = plan_due_work(
            plan=_plan(),
            cycle_id=f"cycle-options-{status.lower()}",
            as_of=NOW,
            retained=_state(
                options_capture_due_status=status,
                options_capture_session_id="2026-07-29",
                options_capture_reasons=("FIXTURE_REASON",),
            ),
        )
        task = _options_task(due)
        assert task.disposition is DueDisposition.NOT_DUE
        assert status in task.reasons
        assert "FIXTURE_REASON" in task.reasons

    def test_options_task_source_evidence_carries_plan_and_session(self) -> None:
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-options-evidence",
            as_of=NOW,
            retained=_state(
                options_capture_due_status="DUE",
                options_capture_plan_id="options-plan-exact",
                options_capture_session_id="2026-07-30",
            ),
        )
        task = _options_task(due)
        assert "options-plan-exact" in task.source_evidence_ids
        assert "2026-07-30" in task.source_evidence_ids

    def test_options_task_ordinal_is_stable_and_dependency_order_matches_task_order(self) -> None:
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-options-order",
            as_of=NOW,
            retained=_state(options_capture_due_status="DUE"),
        )
        assert tuple(item.task_type for item in due.tasks) == due.dependency_order
        task = _options_task(due)
        # Between KALSHI_MARKET_REFRESH (ordinal 4) and SHARED_OPPORTUNITY_REBUILD.
        kalshi = next(
            item for item in due.tasks if item.task_type is OperatingTaskType.KALSHI_MARKET_REFRESH
        )
        shared = next(
            item
            for item in due.tasks
            if item.task_type is OperatingTaskType.SHARED_OPPORTUNITY_REBUILD
        )
        assert kalshi.ordinal < task.ordinal < shared.ordinal


class TestOptionsCaptureHorizonReasonAndReevaluationPredicate:
    """Options Capture Temporal Reevaluation Correctness v2: pins the exact
    horizon math ``options_capture_due_task`` uses to decide whether a
    ``NOT_DUE`` disposition is provisional, and the pure predicate
    (``options_capture_awaits_same_day_reevaluation``) that alone consumes
    it -- independent of the full multi-session cycle-level tests above."""

    def test_missed_with_a_future_window_within_horizon_is_reevaluable(self) -> None:
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-horizon-within",
            as_of=NOW,  # 2026-07-30T12:00Z; horizon end is 2026-07-31T00:00Z
            retained=_state(
                options_capture_due_status="MISSED_CAPTURE_WINDOW",
                options_capture_session_id="2026-07-29",
                options_capture_next_window_open_at=datetime(2026, 7, 30, 20, 0, tzinfo=UTC),
            ),
        )
        task = _options_task(due)
        assert "FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON" in task.reasons
        assert options_capture_awaits_same_day_reevaluation(task) is True

    def test_missed_with_the_next_window_on_a_later_utc_day_is_terminal(self) -> None:
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-horizon-outside",
            as_of=NOW,
            retained=_state(
                options_capture_due_status="MISSED_CAPTURE_WINDOW",
                options_capture_session_id="2026-07-30",
                options_capture_next_window_open_at=datetime(2026, 7, 31, 20, 0, tzinfo=UTC),
            ),
        )
        task = _options_task(due)
        assert "FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON" not in task.reasons
        assert options_capture_awaits_same_day_reevaluation(task) is False

    def test_missed_with_no_known_future_window_at_all_is_terminal(self) -> None:
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-horizon-none",
            as_of=NOW,
            retained=_state(
                options_capture_due_status="MISSED_CAPTURE_WINDOW",
                options_capture_session_id="2026-07-30",
                options_capture_next_window_open_at=None,
            ),
        )
        task = _options_task(due)
        assert "FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON" not in task.reasons
        assert options_capture_awaits_same_day_reevaluation(task) is False

    def test_exact_horizon_end_boundary_is_not_within_this_cycle(self) -> None:
        """The horizon is ``[start-of-as_of-day, start-of-as_of-day + 1
        day)`` -- a next window landing exactly on that boundary belongs to
        the *next* UTC day's own cycle, never this one."""

        horizon_end = datetime(2026, 7, 31, tzinfo=UTC)
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-horizon-exact-boundary",
            as_of=NOW,
            retained=_state(
                options_capture_due_status="MISSED_CAPTURE_WINDOW",
                options_capture_session_id="2026-07-30",
                options_capture_next_window_open_at=horizon_end,
            ),
        )
        task = _options_task(due)
        assert "FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON" not in task.reasons

        just_inside = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-horizon-just-inside",
            as_of=NOW,
            retained=_state(
                options_capture_due_status="MISSED_CAPTURE_WINDOW",
                options_capture_session_id="2026-07-30",
                options_capture_next_window_open_at=horizon_end - timedelta(microseconds=1),
            ),
        )
        assert "FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON" in _options_task(just_inside).reasons

    def test_due_disposition_never_carries_the_horizon_reason(self) -> None:
        """A next-window fact is irrelevant once this task is itself DUE --
        the horizon reason is only ever meaningful for a NOT_DUE outcome."""

        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-horizon-due",
            as_of=NOW,
            retained=_state(
                options_capture_due_status="DUE",
                options_capture_session_id="2026-07-30",
                options_capture_next_window_open_at=datetime(2026, 7, 30, 20, 0, tzinfo=UTC),
            ),
        )
        task = _options_task(due)
        assert "FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON" not in task.reasons
        assert options_capture_awaits_same_day_reevaluation(task) is False

    def test_already_captured_with_a_next_window_outside_horizon_is_terminal(self) -> None:
        """The realistic shape: XNYS never places two eligible sessions
        inside one still-open UTC day, so ALREADY_CAPTURED's own next
        window (necessarily some later day) never falls within horizon."""

        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-horizon-already-captured",
            as_of=NOW,
            retained=_state(
                options_capture_due_status="ALREADY_CAPTURED",
                options_capture_session_id="2026-07-30",
                options_capture_next_window_open_at=datetime(2026, 7, 31, 20, 0, tzinfo=UTC),
            ),
        )
        task = _options_task(due)
        assert "FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON" not in task.reasons
        assert options_capture_awaits_same_day_reevaluation(task) is False

    def test_predicate_ignores_every_other_task_type(self) -> None:
        other = DueTask.create(
            task_type=OperatingTaskType.CRYPTO_MARKET_PULSE,
            disposition=DueDisposition.NOT_DUE,
            reasons=("FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id="cycle-predicate-other-type",
            network_behavior="fixture",
        )
        assert options_capture_awaits_same_day_reevaluation(other) is False

    def test_options_capture_due_task_requires_as_of_to_be_timezone_aware(self) -> None:
        with pytest.raises(OperatingCycleError):
            options_capture_due_task(
                cycle_id="cycle-naive-as-of",
                as_of=datetime(2026, 7, 30, 12),  # naive, deliberately
                retained=_state(options_capture_due_status="MISSED_CAPTURE_WINDOW"),
            )


class _OptionsOnlyFailingExecutor:
    """Fails exactly the options task; completes every other due task --
    proves a provider/options-data failure never blocks or is blocked by
    crypto/Kalshi (independent ``dependency_types=()``, milestone: "Preserve
    exact operating separation")."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, *, task: object, **_: object) -> TaskExecution:
        self.calls.append(task.task_type.value)  # type: ignore[attr-defined]
        if task.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE:  # type: ignore[attr-defined]
            raise RuntimeError("fixture: genuine options capture failure")
        return TaskExecution(OperatingTaskStatus.COMPLETED, ("fixture-output",), (), True, 0)


class TestOptionsFailureIsolation:
    def test_options_capture_failure_does_not_block_crypto_or_kalshi(self) -> None:
        plan = _plan()
        retained = _state(
            crypto_universe_created_at=NOW - timedelta(days=7),
            crypto_universe_refresh_supported=True,
            crypto_pulse_as_of=NOW - timedelta(days=1),
            total_market_source_batch_id="batch-old",
            kalshi_latest_at=NOW - timedelta(minutes=10),
            kalshi_refresh_supported=True,
            options_capture_due_status="DUE",
            options_capture_plan_id="options-plan-1",
            options_capture_session_id="2026-07-30",
        )
        repository = _MemoryRepository(plan)
        source = _StateSource(retained)
        executor = _OptionsOnlyFailingExecutor()
        service = ExecuteOperatingCycle(repository, source, executor)  # type: ignore[arg-type]
        result = service.execute(
            cycle_id="cycle-options-isolated",
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key="cycle-options-isolated-key",
        )
        by_type = {item.task_type.value: item for item in result.tasks}
        assert by_type["OPTIONS_RESEARCH_EVIDENCE_CAPTURE"].status is OperatingTaskStatus.FAILED
        assert by_type["CRYPTO_UNIVERSE_REFRESH"].status is OperatingTaskStatus.COMPLETED
        assert by_type["CRYPTO_MARKET_PULSE"].status is OperatingTaskStatus.COMPLETED
        assert by_type["KALSHI_MARKET_REFRESH"].status is OperatingTaskStatus.COMPLETED
        # SHARED_OPPORTUNITY_REBUILD depends on crypto pulse + Kalshi
        # (never on options), so it must complete too.
        assert by_type["SHARED_OPPORTUNITY_REBUILD"].status is OperatingTaskStatus.COMPLETED


# --------------------------------------------------------------------------
# Recurring Operating Health & Unattended Evidence Closure v1: a single
# long-lived cycle (fixed ``as_of``/``cycle_id``, exactly the shape the real
# supervised worker uses) must genuinely reevaluate the options-capture task
# later the same UTC day, execute it exactly once, and stay safe across a
# simulated worker restart -- proved through the real
# ``ExecuteOperatingCycle``/``_resume_cycle`` orchestration, not only by
# calling the due-state function directly.
# --------------------------------------------------------------------------


class _TimeVaryingOptionsStateSource:
    """Every field except the options-capture ones stays fixed (mirroring
    ``_state()``'s own stable defaults); the options fields alone depend on
    ``evaluated_at``, the same way ``PostgresOperatingStateSource`` genuinely
    behaves against a real XNYS session and a real retained checkpoint."""

    def __init__(self, *, as_of: datetime, window_open: datetime, window_close: datetime) -> None:
        self._as_of = as_of
        self._window_open = window_open
        self._window_close = window_close
        self.captured_session_ids: set[str] = set()
        self.evaluated_at_calls: list[datetime | None] = []

    def snapshot(
        self, *, as_of: datetime, evaluated_at: datetime | None = None
    ) -> RetainedOperatingState:
        self.evaluated_at_calls.append(evaluated_at)
        now = as_of if evaluated_at is None else evaluated_at
        # This single-session fixture models exactly one XNYS session, so
        # the only instant a *future* window genuinely exists is before that
        # one session's own window opens -- mirroring exactly what a real
        # ``determine_options_capture_due_state`` would report when no
        # further session evidence is in range (see
        # ``_MultiSessionOptionsStateSource`` below for the genuinely
        # multi-session case this alone cannot represent).
        next_window_open_at = self._window_open if now < self._window_open else None
        if now < self._window_open:
            status, reasons = "NOT_DUE", ("BEFORE_CAPTURE_WINDOW",)
        elif now <= self._window_close:
            if "2026-07-30" in self.captured_session_ids:
                status, reasons = "ALREADY_CAPTURED", ("ALREADY_CAPTURED_THIS_SESSION",)
            else:
                status, reasons = "DUE", ("WITHIN_POST_CLOSE_CAPTURE_WINDOW",)
        else:
            status, reasons = "MISSED_CAPTURE_WINDOW", ("CAPTURE_WINDOW_ALREADY_PASSED",)
        return _state(
            captured_at=self._as_of,
            latest_brief_as_of=None,
            options_capture_plan_id="options-plan-1",
            options_capture_due_status=status,
            options_capture_session_id="2026-07-30",
            options_capture_window_open_at=self._window_open,
            options_capture_window_close_at=self._window_close,
            options_capture_reasons=reasons,
            options_capture_next_window_open_at=next_window_open_at,
        )

    def brief_inputs(
        self,
    ) -> tuple[str | None, tuple[tuple[str, int], ...], str | None, tuple[str, ...]]:
        return "PARTIAL_CONTEXT", (("ELIGIBLE", 0),), "1000", ()


class _GenericCompletingExecutor:
    """Completes every task type generically; the options task additionally
    marks its session captured on the shared state source, so a second
    genuine capture attempt would observe ``ALREADY_CAPTURED`` -- exactly
    the real ``AlpacaOptionsResearchEvidenceCapture``/checkpoint contract,
    without a database."""

    def __init__(self, state_source: _TimeVaryingOptionsStateSource) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self._state_source = state_source

    def execute(self, *, task: object, executed_at: datetime, **_: object) -> TaskExecution:
        task_type = task.task_type.value  # type: ignore[attr-defined]
        self.calls.append((task_type, executed_at))
        if task.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE:  # type: ignore[attr-defined]
            self._state_source.captured_session_ids.add("2026-07-30")
            return TaskExecution(
                OperatingTaskStatus.COMPLETED,
                ("options-forward-evidence-checkpoint-2026-07-30",),
                (),
                True,
                3,
            )
        return TaskExecution(OperatingTaskStatus.COMPLETED, (f"{task_type}-output",), (), True, 0)


class TestOptionsCaptureIntraDayReevaluation:
    def test_not_due_transitions_to_due_then_stays_exactly_once_across_simulated_restarts(
        self,
    ) -> None:
        as_of = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        window_open = as_of + timedelta(hours=20)
        window_close = window_open + timedelta(minutes=15)
        state_source = _TimeVaryingOptionsStateSource(
            as_of=as_of, window_open=window_open, window_close=window_close
        )
        executor = _GenericCompletingExecutor(state_source)
        repository = _MemoryRepository(_plan())
        plan_id = _plan().plan_id
        cycle_id = "cycle-options-intraday"

        # Morning: a genuine worker poll well before today's capture window.
        morning = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(hours=9),
            idempotency_key=f"{cycle_id}-key",
        )
        assert "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" not in {
            item.task_type.value for item in morning.tasks
        }
        assert not any(call[0] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" for call in executor.calls)
        options_due_task = next(
            item
            for item in morning.due_work.tasks
            if item.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE
        )
        assert options_due_task.disposition is DueDisposition.NOT_DUE

        # Later, the same UTC day, during the valid capture window -- a
        # genuinely new ``ExecuteOperatingCycle`` instance (over the exact
        # same repository/state source) simulates a supervised-worker
        # restart between polls.
        within_window = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=window_open + timedelta(minutes=5),
            idempotency_key=f"{cycle_id}-key",
        )
        options_result = next(
            item
            for item in within_window.tasks
            if item.task_type.value == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
        )
        assert options_result.status is OperatingTaskStatus.COMPLETED
        assert not within_window.idempotent_replay
        options_calls = [c for c in executor.calls if c[0] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"]
        assert len(options_calls) == 1
        # Every other already-persisted task from the morning call was
        # reused, never re-executed, on this later call.
        assert sum(1 for c in executor.calls if c[0] == "PORTFOLIO_SNAPSHOT_REFRESH") == 1

        # Later still, a third poll (another simulated restart) after the
        # capture already completed this session -- no duplicate checkpoint,
        # no duplicate provider capture, a genuinely pure replay.
        after_capture = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=window_close + timedelta(minutes=1),
            idempotency_key=f"{cycle_id}-key",
        )
        assert after_capture.idempotent_replay
        final_options_calls = [
            c for c in executor.calls if c[0] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
        ]
        assert len(final_options_calls) == 1
        final_options_result = next(
            item
            for item in after_capture.tasks
            if item.task_type.value == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
        )
        assert final_options_result.output_identities == options_result.output_identities
        assert len(repository.results) == len(after_capture.tasks)


# --------------------------------------------------------------------------
# Counterfactual Follow-Up Backlog Throughput v1: proves ``TaskExecution.
# awaits_intraday_continuation`` correctly leaves ``COUNTERFACTUAL_FOLLOWUP_
# REFRESH`` unresolved across multiple same-UTC-day ticks (mirroring
# ``TestOptionsCaptureIntraDayReevaluation`` above exactly, at the pure
# dispatch-loop level -- ``SyncFollowUpObservations``'s own bounded-round
# behavior is proven separately, against real Postgres, in
# ``tests/persistence/test_postgres_counterfactual_followup_recurring.py``).
# --------------------------------------------------------------------------


class _FollowUpContinuingExecutor:
    """Completes every task type generically; ``COUNTERFACTUAL_FOLLOWUP_
    REFRESH`` reports ``awaits_intraday_continuation=True`` until
    ``rounds_to_drain`` genuine dispatches have happened, then ``False`` --
    simulating a real bounded round that keeps genuinely filling its own
    ``sample_limit`` until the backlog it represents is actually exhausted,
    without a database."""

    def __init__(self, *, rounds_to_drain: int) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self._rounds_to_drain = rounds_to_drain
        self._followup_dispatches = 0

    def execute(self, *, task: object, executed_at: datetime, **_: object) -> TaskExecution:
        task_type = task.task_type.value  # type: ignore[attr-defined]
        self.calls.append((task_type, executed_at))
        if task.task_type is OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH:  # type: ignore[attr-defined]
            self._followup_dispatches += 1
            awaits = self._followup_dispatches < self._rounds_to_drain
            return TaskExecution(
                OperatingTaskStatus.COMPLETED,
                (f"decision_followup_observation_round_{self._followup_dispatches}",),
                (),
                True,
                0,
                awaits_intraday_continuation=awaits,
            )
        return TaskExecution(OperatingTaskStatus.COMPLETED, (f"{task_type}-output",), (), True, 0)


class TestCounterfactualFollowUpIntradayContinuation:
    def test_multiple_rounds_stay_unresolved_then_freeze_and_replay(self) -> None:
        as_of = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        executor = _FollowUpContinuingExecutor(rounds_to_drain=3)
        repository = _MemoryRepository(_plan())
        state_source = _StateSource(_state(captured_at=as_of, latest_brief_as_of=None))
        plan_id = _plan().plan_id
        cycle_id = "cycle-followup-intraday"

        # Tick 1 (00:00Z): round 1 genuinely fills its budget -- stays
        # unresolved rather than freezing at round 1's count.
        tick1 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of,
            idempotency_key=f"{cycle_id}-key",
        )
        assert "COUNTERFACTUAL_FOLLOWUP_REFRESH" not in {
            item.task_type.value for item in tick1.tasks
        }
        followup_calls = [c for c in executor.calls if c[0] == "COUNTERFACTUAL_FOLLOWUP_REFRESH"]
        assert len(followup_calls) == 1
        # Every other unconditionally-due task resolved normally on tick 1
        # and must never be re-dispatched by a later tick below.
        assert sum(1 for c in executor.calls if c[0] == "PORTFOLIO_SNAPSHOT_REFRESH") == 1

        # Tick 2 (00:05Z, a simulated restart -- new ExecuteOperatingCycle
        # instance over the same repository/state source): round 2 also
        # fills its budget -- still unresolved.
        tick2 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=5),
            idempotency_key=f"{cycle_id}-key",
        )
        assert "COUNTERFACTUAL_FOLLOWUP_REFRESH" not in {
            item.task_type.value for item in tick2.tasks
        }
        followup_calls = [c for c in executor.calls if c[0] == "COUNTERFACTUAL_FOLLOWUP_REFRESH"]
        assert len(followup_calls) == 2
        assert sum(1 for c in executor.calls if c[0] == "PORTFOLIO_SNAPSHOT_REFRESH") == 1

        # Tick 3 (00:10Z): round 3 genuinely drains the backlog -- freezes
        # today's result for this task type.
        tick3 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=10),
            idempotency_key=f"{cycle_id}-key",
        )
        followup_result = next(
            item
            for item in tick3.tasks
            if item.task_type.value == "COUNTERFACTUAL_FOLLOWUP_REFRESH"
        )
        assert followup_result.status is OperatingTaskStatus.COMPLETED
        assert followup_result.output_identities == ("decision_followup_observation_round_3",)
        assert not tick3.idempotent_replay
        followup_calls = [c for c in executor.calls if c[0] == "COUNTERFACTUAL_FOLLOWUP_REFRESH"]
        assert len(followup_calls) == 3

        # Tick 4 (00:15Z): everything -- including follow-up -- is already
        # resolved for today. A pure idempotent replay: zero new dispatches
        # of anything, including follow-up.
        tick4 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=15),
            idempotency_key=f"{cycle_id}-key",
        )
        assert tick4.idempotent_replay
        final_followup_calls = [
            c for c in executor.calls if c[0] == "COUNTERFACTUAL_FOLLOWUP_REFRESH"
        ]
        assert len(final_followup_calls) == 3
        final_followup_result = next(
            item
            for item in tick4.tasks
            if item.task_type.value == "COUNTERFACTUAL_FOLLOWUP_REFRESH"
        )
        assert final_followup_result.output_identities == followup_result.output_identities
        assert len(repository.results) == len(tick4.tasks)
        # Downstream COUNTERFACTUAL_OUTCOME_REFRESH (declares a real
        # same-cycle dependency on follow-up -- see ``plan_due_work``) was
        # never blocked by follow-up staying unresolved across ticks 1-2: it
        # resolved on tick 1, exactly once, like every other task.
        assert sum(1 for c in executor.calls if c[0] == "COUNTERFACTUAL_OUTCOME_REFRESH") == 1
        outcome_result = next(
            item for item in tick1.tasks if item.task_type.value == "COUNTERFACTUAL_OUTCOME_REFRESH"
        )
        assert outcome_result.status is OperatingTaskStatus.COMPLETED


# --------------------------------------------------------------------------
# Recurring Strategy PAPER Intraday Execution Correction v1: unlike
# COUNTERFACTUAL_FOLLOWUP_REFRESH above, which eventually drains a bounded
# per-day backlog and freezes, STRATEGY_PAPER_TRIAL_EXECUTION manages a
# continuously open PAPER position -- there is no "backlog drained" terminal
# state for one UTC day, so it must report ``awaits_intraday_continuation``
# on every genuine dispatch, all day, every tick, until the next UTC-day
# rollover starts a fresh cycle_id. Proves the shared scheduler mechanism
# (already proven generically above) sustains that "never drains" shape
# without disturbing any sibling always-DUE task's own once-per-day
# semantics -- the real executor branch's own signal is proven separately,
# against a real Postgres database, in
# tests/persistence/test_postgres_strategy_paper_trial_recurring.py.
# --------------------------------------------------------------------------


class _StrategyPaperLifecycleExecutor:
    """Completes every task type generically; STRATEGY_PAPER_TRIAL_EXECUTION
    reports ``awaits_intraday_continuation=True`` on every single dispatch,
    simulating the fix under test (never a bounded number of rounds the way
    ``_FollowUpContinuingExecutor`` above simulates a draining backlog)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self._strategy_dispatches = 0

    def execute(self, *, task: object, executed_at: datetime, **_: object) -> TaskExecution:
        task_type = task.task_type.value  # type: ignore[attr-defined]
        self.calls.append((task_type, executed_at))
        if task.task_type is OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION:  # type: ignore[attr-defined]
            self._strategy_dispatches += 1
            return TaskExecution(
                OperatingTaskStatus.COMPLETED,
                (f"strategy-paper-round-{self._strategy_dispatches}",),
                (),
                True,
                0,
                (f"STRATEGY_PAPER_DISPATCH_ROUND:{self._strategy_dispatches}",),
                None,
                False,
                awaits_intraday_continuation=True,
            )
        return TaskExecution(OperatingTaskStatus.COMPLETED, (f"{task_type}-output",), (), True, 0)


class TestStrategyPaperTrialIntradayContinuation:
    def test_never_drains_and_redispatches_every_same_day_tick_without_disturbing_siblings(
        self,
    ) -> None:
        as_of = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        executor = _StrategyPaperLifecycleExecutor()
        repository = _MemoryRepository(_plan())
        state_source = _StateSource(_state(captured_at=as_of, latest_brief_as_of=None))
        plan_id = _plan().plan_id
        cycle_id = "cycle-strategy-paper-intraday"

        # Tick 1 (00:00Z): genuinely dispatched, but -- unlike every ordinary
        # always-DUE task -- never frozen into a persisted terminal result,
        # exactly like a still-draining COUNTERFACTUAL_FOLLOWUP_REFRESH round.
        tick1 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of,
            idempotency_key=f"{cycle_id}-key",
        )
        assert "STRATEGY_PAPER_TRIAL_EXECUTION" not in {
            item.task_type.value for item in tick1.tasks
        }
        strategy_calls = [c for c in executor.calls if c[0] == "STRATEGY_PAPER_TRIAL_EXECUTION"]
        assert len(strategy_calls) == 1
        assert sum(1 for c in executor.calls if c[0] == "PORTFOLIO_SNAPSHOT_REFRESH") == 1

        # Tick 2 (00:05Z, a simulated restart -- new ExecuteOperatingCycle
        # instance over the same repository/state source, same cycle_id):
        # this is the core regression proof -- under the pre-fix executor
        # (no ``awaits_intraday_continuation``), ``_resume_cycle`` would find
        # STRATEGY_PAPER_TRIAL_EXECUTION already in ``by_type`` from tick 1
        # and ``continue`` past it without ever calling the executor again,
        # so this count would stay frozen at 1 forever.
        tick2 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=5),
            idempotency_key=f"{cycle_id}-key",
        )
        assert "STRATEGY_PAPER_TRIAL_EXECUTION" not in {
            item.task_type.value for item in tick2.tasks
        }
        strategy_calls = [c for c in executor.calls if c[0] == "STRATEGY_PAPER_TRIAL_EXECUTION"]
        assert len(strategy_calls) == 2
        # Every ordinary always-DUE sibling task resolved once on tick 1 and
        # must never be re-dispatched by a later tick: this fix is scoped to
        # exactly one task type, not a general loosening of the replay rule.
        assert sum(1 for c in executor.calls if c[0] == "PORTFOLIO_SNAPSHOT_REFRESH") == 1
        assert sum(1 for c in executor.calls if c[0] == "COUNTERFACTUAL_OUTCOME_REFRESH") == 1

        # Tick 3 (00:10Z): still genuinely redispatched -- no bounded number
        # of rounds, unlike follow-up's backlog-drain shape.
        tick3 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=10),
            idempotency_key=f"{cycle_id}-key",
        )
        strategy_calls = [c for c in executor.calls if c[0] == "STRATEGY_PAPER_TRIAL_EXECUTION"]
        assert len(strategy_calls) == 3
        # Framework-level bookkeeping: no NEW terminal task result, cycle
        # event, brief, or health was written this tick (everything else was
        # already resolved on tick 1) -- byte-identical to how a mid-drain
        # follow-up tick already behaves -- even though the executor was
        # genuinely invoked a third time.
        assert tick3.idempotent_replay
        assert sum(1 for c in executor.calls if c[0] == "PORTFOLIO_SNAPSHOT_REFRESH") == 1

        # A brand new UTC day (a genuinely new cycle_id): the pattern starts
        # over from a clean slate, exactly like every other task type.
        next_day = as_of + timedelta(days=1)
        next_state_source = _StateSource(_state(captured_at=next_day, latest_brief_as_of=None))
        tick_next_day = ExecuteOperatingCycle(repository, next_state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id="cycle-strategy-paper-intraday-day2",
            plan_id=plan_id,
            as_of=next_day,
            executed_at=next_day,
            idempotency_key="cycle-strategy-paper-intraday-day2-key",
        )
        assert "STRATEGY_PAPER_TRIAL_EXECUTION" not in {
            item.task_type.value for item in tick_next_day.tasks
        }
        strategy_calls = [c for c in executor.calls if c[0] == "STRATEGY_PAPER_TRIAL_EXECUTION"]
        assert len(strategy_calls) == 4


# --------------------------------------------------------------------------
# STRATEGY_TRIAL_OUTCOME_ASSESSMENT: scheduling and failure isolation
# (Strategy PAPER Trial Outcome Assessment v1, requirement 13)
# --------------------------------------------------------------------------


class _OutcomeAssessmentFailingExecutor(_StrategyPaperLifecycleExecutor):
    """The PAPER lifecycle keeps succeeding every tick; outcome assessment
    raises every time it is dispatched."""

    def execute(self, *, task: object, executed_at: datetime, **kwargs: object) -> TaskExecution:
        if task.task_type is OperatingTaskType.STRATEGY_TRIAL_OUTCOME_ASSESSMENT:  # type: ignore[attr-defined]
            self.calls.append(("STRATEGY_TRIAL_OUTCOME_ASSESSMENT", executed_at))
            raise RuntimeError("INJECTED_OUTCOME_ASSESSMENT_FAILURE")
        return super().execute(task=task, executed_at=executed_at, **kwargs)


class TestStrategyTrialOutcomeAssessmentScheduling:
    def test_due_every_cycle_with_no_dependency_and_no_network(self) -> None:
        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-outcome-assess", as_of=NOW, retained=_state()
        )
        task = _task(due, OperatingTaskType.STRATEGY_TRIAL_OUTCOME_ASSESSMENT)
        assert task.disposition is DueDisposition.DUE
        assert task.dependency_types == ()
        assert task.network_behavior == "LOCAL_RETAINED_READS_ONLY_NO_PROVIDER_REQUEST"

    def test_runs_immediately_after_trial_lifecycle_processing(self) -> None:
        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-outcome-order", as_of=NOW, retained=_state()
        )
        assert tuple(item.task_type for item in due.tasks) == due.dependency_order
        lifecycle = _task(due, OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION)
        assessment = _task(due, OperatingTaskType.STRATEGY_TRIAL_OUTCOME_ASSESSMENT)
        assert assessment.ordinal == lifecycle.ordinal + 1

    def test_nothing_but_the_informational_brief_depends_on_it(self) -> None:
        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-outcome-deps", as_of=NOW, retained=_state()
        )
        dependents = {
            item.task_type
            for item in due.tasks
            if OperatingTaskType.STRATEGY_TRIAL_OUTCOME_ASSESSMENT in item.dependency_types
        }
        assert dependents == {OperatingTaskType.OPERATING_BRIEF_BUILD}

    def test_assessment_failure_never_stops_paper_lifecycle_ticks(self) -> None:
        as_of = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        executor = _OutcomeAssessmentFailingExecutor()
        repository = _MemoryRepository(_plan())
        state_source = _StateSource(_state(captured_at=as_of, latest_brief_as_of=None))
        cycle_id = "cycle-outcome-assessment-failing"

        def tick(minutes: int):  # type: ignore[no-untyped-def]
            return ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
                cycle_id=cycle_id,
                plan_id=_plan().plan_id,
                as_of=as_of,
                executed_at=as_of + timedelta(minutes=minutes),
                idempotency_key=f"{cycle_id}-key",
            )

        first = tick(0)
        by_type = {item.task_type.value: item for item in first.tasks}
        assert by_type["STRATEGY_TRIAL_OUTCOME_ASSESSMENT"].status is OperatingTaskStatus.FAILED
        # The lifecycle is untouched: it is still continuing (no terminal
        # row), and every sibling reached its ordinary outcome.
        assert "STRATEGY_PAPER_TRIAL_EXECUTION" not in by_type
        assert by_type["PORTFOLIO_SNAPSHOT_REFRESH"].status is OperatingTaskStatus.COMPLETED
        assert by_type["COUNTERFACTUAL_OUTCOME_REFRESH"].status is OperatingTaskStatus.COMPLETED
        assert by_type["OPERATING_BRIEF_BUILD"].status is OperatingTaskStatus.COMPLETED
        assert not any(item.status is OperatingTaskStatus.BLOCKED for item in first.tasks)

        # Later same-day ticks: the PAPER lifecycle is dispatched every time
        # (a close/fill/reconciliation can still happen); the failed
        # assessment stays frozen for the day and is never retried in a way
        # that could interleave with it.
        tick(5)
        tick(10)
        calls = [name for name, _ in executor.calls]
        assert calls.count("STRATEGY_PAPER_TRIAL_EXECUTION") == 3
        assert calls.count("STRATEGY_TRIAL_OUTCOME_ASSESSMENT") == 1


# --------------------------------------------------------------------------
# CRYPTO_PAPER_TRIAL_EXECUTION / KALSHI_PAPER_TRIAL_EXECUTION: intraday
# continuation (CRYPTO/KALSHI PAPER Intraday Lifecycle Recovery v1). Both
# older PAPER-trial tasks had the identical missing-``awaits_intraday_
# continuation`` gap ``_StrategyPaperLifecycleExecutor``/
# ``TestStrategyPaperTrialIntradayContinuation`` above already proved and
# fixed for the newer strategy-driven path -- these mirror that exact shape
# for each older task type independently, since neither is structurally
# identical enough to the other to share one fake (CRYPTO's due-ness depends
# on an allocation decision, KALSHI's does too, but each dispatches through
# its own executor branch).
# --------------------------------------------------------------------------


class _CryptoPaperLifecycleExecutor:
    """Completes every task type generically; CRYPTO_PAPER_TRIAL_EXECUTION
    reports ``awaits_intraday_continuation=True`` on every single dispatch,
    exactly mirroring ``_StrategyPaperLifecycleExecutor`` above."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self._dispatches = 0

    def execute(self, *, task: object, executed_at: datetime, **_: object) -> TaskExecution:
        task_type = task.task_type.value  # type: ignore[attr-defined]
        self.calls.append((task_type, executed_at))
        if task.task_type is OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION:  # type: ignore[attr-defined]
            self._dispatches += 1
            return TaskExecution(
                OperatingTaskStatus.COMPLETED,
                (f"crypto-paper-round-{self._dispatches}",),
                (),
                True,
                0,
                (f"CRYPTO_PAPER_DISPATCH_ROUND:{self._dispatches}",),
                None,
                False,
                awaits_intraday_continuation=True,
            )
        return TaskExecution(OperatingTaskStatus.COMPLETED, (f"{task_type}-output",), (), True, 0)


class TestCryptoPaperTrialIntradayContinuation:
    def test_never_drains_and_redispatches_every_same_day_tick_without_disturbing_siblings(
        self,
    ) -> None:
        as_of = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        executor = _CryptoPaperLifecycleExecutor()
        repository = _MemoryRepository(_plan())
        state_source = _StateSource(_state(captured_at=as_of, latest_brief_as_of=None))
        plan_id = _plan().plan_id
        cycle_id = "cycle-crypto-paper-intraday"

        tick1 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of,
            idempotency_key=f"{cycle_id}-key",
        )
        assert "CRYPTO_PAPER_TRIAL_EXECUTION" not in {item.task_type.value for item in tick1.tasks}
        crypto_calls = [c for c in executor.calls if c[0] == "CRYPTO_PAPER_TRIAL_EXECUTION"]
        assert len(crypto_calls) == 1
        assert sum(1 for c in executor.calls if c[0] == "PORTFOLIO_SNAPSHOT_REFRESH") == 1

        # Same cycle_id, a simulated restart (new ExecuteOperatingCycle
        # instance, same repository/state source) -- the core regression
        # proof: pre-fix, ``_resume_cycle`` would find
        # CRYPTO_PAPER_TRIAL_EXECUTION already in ``by_type`` from tick 1 and
        # skip it forever, so this count would stay frozen at 1.
        tick2 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=5),
            idempotency_key=f"{cycle_id}-key",
        )
        assert "CRYPTO_PAPER_TRIAL_EXECUTION" not in {item.task_type.value for item in tick2.tasks}
        crypto_calls = [c for c in executor.calls if c[0] == "CRYPTO_PAPER_TRIAL_EXECUTION"]
        assert len(crypto_calls) == 2
        # Every ordinary always-DUE sibling -- including the OTHER older
        # PAPER-trial task type -- resolved once on tick 1 and must never be
        # re-dispatched by a later tick: this fix is scoped to exactly one
        # task type, not a general loosening of the replay rule.
        assert sum(1 for c in executor.calls if c[0] == "PORTFOLIO_SNAPSHOT_REFRESH") == 1
        assert sum(1 for c in executor.calls if c[0] == "KALSHI_PAPER_TRIAL_EXECUTION") == 1

        # Tick 3: still genuinely redispatched -- no bounded number of
        # rounds, unlike follow-up's backlog-drain shape.
        tick3 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=10),
            idempotency_key=f"{cycle_id}-key",
        )
        crypto_calls = [c for c in executor.calls if c[0] == "CRYPTO_PAPER_TRIAL_EXECUTION"]
        assert len(crypto_calls) == 3
        assert tick3.idempotent_replay

        # A brand new UTC day (a genuinely new cycle_id): starts over clean.
        next_day = as_of + timedelta(days=1)
        next_state_source = _StateSource(_state(captured_at=next_day, latest_brief_as_of=None))
        tick_next_day = ExecuteOperatingCycle(repository, next_state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id="cycle-crypto-paper-intraday-day2",
            plan_id=plan_id,
            as_of=next_day,
            executed_at=next_day,
            idempotency_key="cycle-crypto-paper-intraday-day2-key",
        )
        assert "CRYPTO_PAPER_TRIAL_EXECUTION" not in {
            item.task_type.value for item in tick_next_day.tasks
        }
        crypto_calls = [c for c in executor.calls if c[0] == "CRYPTO_PAPER_TRIAL_EXECUTION"]
        assert len(crypto_calls) == 4


class _CryptoPaperFailingExecutor:
    """Every task type completes generically except
    CRYPTO_PAPER_TRIAL_EXECUTION, which always raises -- proves the shared
    fail-closed policy (Phase D) is unaffected by the continuation fix: a
    genuine exception must still freeze the task FAILED for the rest of the
    UTC day, never auto-retried by a later tick the way a success is."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, *, task: object, **_: object) -> TaskExecution:
        task_type = task.task_type.value  # type: ignore[attr-defined]
        self.calls.append(task_type)
        if task.task_type is OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION:  # type: ignore[attr-defined]
            raise RuntimeError("CRYPTO_PAPER_TRIAL_EXECUTION_SIMULATED_FAILURE")
        return TaskExecution(OperatingTaskStatus.COMPLETED, (f"{task_type}-output",), (), True, 0)


class TestCryptoPaperTrialFailureSemantics:
    def test_genuine_exception_freezes_failed_for_rest_of_day(self) -> None:
        as_of = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        executor = _CryptoPaperFailingExecutor()
        repository = _MemoryRepository(_plan())
        state_source = _StateSource(_state(captured_at=as_of, latest_brief_as_of=None))
        plan_id = _plan().plan_id
        cycle_id = "cycle-crypto-paper-failure"

        tick1 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of,
            idempotency_key=f"{cycle_id}-key",
        )
        failed = next(
            item
            for item in tick1.tasks
            if item.task_type is OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION
        )
        assert failed.status is OperatingTaskStatus.FAILED
        assert executor.calls.count("CRYPTO_PAPER_TRIAL_EXECUTION") == 1

        tick2 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=5),
            idempotency_key=f"{cycle_id}-key",
        )
        # Frozen: a genuine exception must never be auto-retried by a later
        # same-day tick, unlike a successful dispatch.
        assert executor.calls.count("CRYPTO_PAPER_TRIAL_EXECUTION") == 1
        assert tick2.idempotent_replay


class _KalshiPaperLifecycleExecutor:
    """Completes every task type generically; KALSHI_PAPER_TRIAL_EXECUTION
    reports ``awaits_intraday_continuation=True`` on every single dispatch --
    the KALSHI counterpart of ``_CryptoPaperLifecycleExecutor`` above."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self._dispatches = 0

    def execute(self, *, task: object, executed_at: datetime, **_: object) -> TaskExecution:
        task_type = task.task_type.value  # type: ignore[attr-defined]
        self.calls.append((task_type, executed_at))
        if task.task_type is OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION:  # type: ignore[attr-defined]
            self._dispatches += 1
            return TaskExecution(
                OperatingTaskStatus.COMPLETED,
                (f"kalshi-paper-round-{self._dispatches}",),
                (),
                True,
                0,
                (f"KALSHI_PAPER_DISPATCH_ROUND:{self._dispatches}",),
                None,
                False,
                awaits_intraday_continuation=True,
            )
        return TaskExecution(OperatingTaskStatus.COMPLETED, (f"{task_type}-output",), (), True, 0)


class TestKalshiPaperTrialIntradayContinuation:
    def test_never_drains_and_redispatches_every_same_day_tick_without_disturbing_siblings(
        self,
    ) -> None:
        as_of = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        executor = _KalshiPaperLifecycleExecutor()
        repository = _MemoryRepository(_plan())
        state_source = _StateSource(_state(captured_at=as_of, latest_brief_as_of=None))
        plan_id = _plan().plan_id
        cycle_id = "cycle-kalshi-paper-intraday"

        tick1 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of,
            idempotency_key=f"{cycle_id}-key",
        )
        assert "KALSHI_PAPER_TRIAL_EXECUTION" not in {item.task_type.value for item in tick1.tasks}
        kalshi_calls = [c for c in executor.calls if c[0] == "KALSHI_PAPER_TRIAL_EXECUTION"]
        assert len(kalshi_calls) == 1
        assert sum(1 for c in executor.calls if c[0] == "PORTFOLIO_SNAPSHOT_REFRESH") == 1

        # Same cycle_id, a simulated restart -- the core regression proof:
        # pre-fix, this count would stay frozen at 1 forever.
        tick2 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=5),
            idempotency_key=f"{cycle_id}-key",
        )
        assert "KALSHI_PAPER_TRIAL_EXECUTION" not in {item.task_type.value for item in tick2.tasks}
        kalshi_calls = [c for c in executor.calls if c[0] == "KALSHI_PAPER_TRIAL_EXECUTION"]
        assert len(kalshi_calls) == 2
        assert sum(1 for c in executor.calls if c[0] == "PORTFOLIO_SNAPSHOT_REFRESH") == 1
        assert sum(1 for c in executor.calls if c[0] == "CRYPTO_PAPER_TRIAL_EXECUTION") == 1

        tick3 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=10),
            idempotency_key=f"{cycle_id}-key",
        )
        kalshi_calls = [c for c in executor.calls if c[0] == "KALSHI_PAPER_TRIAL_EXECUTION"]
        assert len(kalshi_calls) == 3
        assert tick3.idempotent_replay

        next_day = as_of + timedelta(days=1)
        next_state_source = _StateSource(_state(captured_at=next_day, latest_brief_as_of=None))
        tick_next_day = ExecuteOperatingCycle(repository, next_state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id="cycle-kalshi-paper-intraday-day2",
            plan_id=plan_id,
            as_of=next_day,
            executed_at=next_day,
            idempotency_key="cycle-kalshi-paper-intraday-day2-key",
        )
        assert "KALSHI_PAPER_TRIAL_EXECUTION" not in {
            item.task_type.value for item in tick_next_day.tasks
        }
        kalshi_calls = [c for c in executor.calls if c[0] == "KALSHI_PAPER_TRIAL_EXECUTION"]
        assert len(kalshi_calls) == 4


class _KalshiPaperFailingExecutor:
    """The KALSHI counterpart of ``_CryptoPaperFailingExecutor`` above."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, *, task: object, **_: object) -> TaskExecution:
        task_type = task.task_type.value  # type: ignore[attr-defined]
        self.calls.append(task_type)
        if task.task_type is OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION:  # type: ignore[attr-defined]
            raise RuntimeError("KALSHI_PAPER_TRIAL_EXECUTION_SIMULATED_FAILURE")
        return TaskExecution(OperatingTaskStatus.COMPLETED, (f"{task_type}-output",), (), True, 0)


class TestKalshiPaperTrialFailureSemantics:
    def test_genuine_exception_freezes_failed_for_rest_of_day(self) -> None:
        as_of = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        executor = _KalshiPaperFailingExecutor()
        repository = _MemoryRepository(_plan())
        state_source = _StateSource(_state(captured_at=as_of, latest_brief_as_of=None))
        plan_id = _plan().plan_id
        cycle_id = "cycle-kalshi-paper-failure"

        tick1 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of,
            idempotency_key=f"{cycle_id}-key",
        )
        failed = next(
            item
            for item in tick1.tasks
            if item.task_type is OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        )
        assert failed.status is OperatingTaskStatus.FAILED
        assert executor.calls.count("KALSHI_PAPER_TRIAL_EXECUTION") == 1

        tick2 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=5),
            idempotency_key=f"{cycle_id}-key",
        )
        assert executor.calls.count("KALSHI_PAPER_TRIAL_EXECUTION") == 1
        assert tick2.idempotent_replay


class _MutableOptionsWindowStateSource:
    """Every field stays fixed at ``_state()``'s own defaults except
    ``options_capture_next_window_open_at``, mutated directly by the test
    between ticks to simulate a same-day options-capture window becoming
    imminent partway through a multi-round follow-up continuation, then
    receding again."""

    def __init__(self, *, as_of: datetime) -> None:
        self._as_of = as_of
        self.next_window_open_at: datetime | None = None
        self.snapshot_calls = 0

    def snapshot(
        self, *, as_of: datetime, evaluated_at: datetime | None = None
    ) -> RetainedOperatingState:
        self.snapshot_calls += 1
        return _state(
            captured_at=self._as_of,
            latest_brief_as_of=None,
            options_capture_next_window_open_at=self.next_window_open_at,
        )

    def brief_inputs(
        self,
    ) -> tuple[str | None, tuple[tuple[str, int], ...], str | None, tuple[str, ...]]:
        return "PARTIAL_CONTEXT", (("ELIGIBLE", 0),), "1000", ()


class TestCounterfactualFollowUpIntradayContinuationYieldsToOptionsPriority:
    def test_imminent_options_window_pauses_continuation_then_resumes_once_it_passes(
        self,
    ) -> None:
        as_of = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        executor = _FollowUpContinuingExecutor(rounds_to_drain=3)
        repository = _MemoryRepository(_plan())
        state_source = _MutableOptionsWindowStateSource(as_of=as_of)
        plan_id = _plan().plan_id
        cycle_id = "cycle-followup-yields-to-options"

        # Tick 1 (00:00Z): no options window anywhere nearby -- follow-up's
        # round 1 genuinely fills its budget, stays unresolved as usual.
        state_source.next_window_open_at = as_of + timedelta(hours=6)
        ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of,
            idempotency_key=f"{cycle_id}-key",
        )
        assert [c for c in executor.calls if c[0] == "COUNTERFACTUAL_FOLLOWUP_REFRESH"]

        # Tick 2 (00:05Z): a same-day options-capture window has since
        # become imminent (well inside the 15-minute guard interval). This
        # cycle's original ``due_work`` -- frozen at tick 1 -- still says
        # follow-up is unconditionally DUE, so this only proves anything if
        # the continuation branch genuinely re-derives fresh reality rather
        # than trusting that stale disposition.
        state_source.next_window_open_at = as_of + timedelta(minutes=5, seconds=30)
        tick2 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=5),
            idempotency_key=f"{cycle_id}-key",
        )
        assert "COUNTERFACTUAL_FOLLOWUP_REFRESH" not in {
            item.task_type.value for item in tick2.tasks
        }
        followup_calls = [c for c in executor.calls if c[0] == "COUNTERFACTUAL_FOLLOWUP_REFRESH"]
        # Yielded, not dispatched: still exactly the one round from tick 1.
        assert len(followup_calls) == 1

        # Tick 3 (00:10Z): the window is still imminent -- still yielding.
        state_source.next_window_open_at = as_of + timedelta(seconds=30)
        ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=10),
            idempotency_key=f"{cycle_id}-key",
        )
        assert len([c for c in executor.calls if c[0] == "COUNTERFACTUAL_FOLLOWUP_REFRESH"]) == 1

        # Tick 4 (00:20Z): the window has passed with nothing else upcoming
        # -- follow-up resumes continuation exactly where it left off.
        state_source.next_window_open_at = None
        tick4 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=20),
            idempotency_key=f"{cycle_id}-key",
        )
        assert "COUNTERFACTUAL_FOLLOWUP_REFRESH" not in {
            item.task_type.value for item in tick4.tasks
        }
        assert len([c for c in executor.calls if c[0] == "COUNTERFACTUAL_FOLLOWUP_REFRESH"]) == 2

        # Tick 5 (00:25Z): round 3 genuinely drains the backlog -- freezes.
        tick5 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=as_of + timedelta(minutes=25),
            idempotency_key=f"{cycle_id}-key",
        )
        followup_result = next(
            item
            for item in tick5.tasks
            if item.task_type.value == "COUNTERFACTUAL_FOLLOWUP_REFRESH"
        )
        assert followup_result.status is OperatingTaskStatus.COMPLETED
        assert len([c for c in executor.calls if c[0] == "COUNTERFACTUAL_FOLLOWUP_REFRESH"]) == 3


# --------------------------------------------------------------------------
# Options Capture Temporal Reevaluation Correctness v2: the single-session
# ``_TimeVaryingOptionsStateSource`` above cannot represent a genuinely
# uncaptured *prior* session still sitting in ``past`` alongside a still-
# upcoming session in ``future`` -- exactly the real 2026-08-11 production
# failure (a UTC-day cycle beginning at 01:17Z, hours after America/
# New_York's own evening rollover, observed 2026-08-10's already-closed
# window as MISSED before 2026-08-11's own window ever opened, then froze
# that verdict for the rest of the day). This fixture models TWO real,
# consecutive, genuine XNYS sessions via the actual reviewed calendar and
# the actual ``determine_options_capture_due_state`` function -- never a
# hand-rolled reimplementation of its priority rules -- so these tests
# exercise the identical real due-state computation production uses.
# --------------------------------------------------------------------------


class _MultiSessionOptionsStateSource:
    """Every field except the options-capture ones stays fixed (mirroring
    ``_state()``'s own stable defaults). The options fields are resolved by
    calling the real, independently-tested ``determine_options_capture_due_
    state`` against real Monday 2026-08-10 / Tuesday 2026-08-11 XNYS
    session evidence -- the same function ``PostgresOperatingStateSource``
    genuinely calls in production (via ``resolve_options_capture_due_
    decision``), so a bug in the due-state function itself, not only in how
    the operating cycle consumes it, would also be caught here."""

    def __init__(self) -> None:
        self._sessions = ReviewedXNYSCalendar(
            start_date="2026-08-01", end_date="2026-08-15"
        ).sessions
        self._policy = OptionsCaptureWindowPolicy.create()
        self.captured_session_ids: set[str] = set()
        self.evaluated_at_calls: list[datetime | None] = []

    def snapshot(
        self, *, as_of: datetime, evaluated_at: datetime | None = None
    ) -> RetainedOperatingState:
        self.evaluated_at_calls.append(evaluated_at)
        now = as_of if evaluated_at is None else evaluated_at
        decision = determine_options_capture_due_state(
            sessions=self._sessions,
            policy=self._policy,
            now=now,
            capability_supported=True,
            captured_session_ids=frozenset(self.captured_session_ids),
        )
        return _state(
            captured_at=as_of,
            latest_brief_as_of=None,
            options_capture_plan_id="options-plan-multi-session-1",
            options_capture_due_status=decision.status.value,
            options_capture_session_id=decision.session_id,
            options_capture_window_open_at=decision.window_open_at,
            options_capture_window_close_at=decision.window_close_at,
            options_capture_reasons=decision.reasons,
            options_capture_next_window_open_at=decision.next_capture_window_open_at,
        )

    def brief_inputs(
        self,
    ) -> tuple[str | None, tuple[tuple[str, int], ...], str | None, tuple[str, ...]]:
        return "PARTIAL_CONTEXT", (("ELIGIBLE", 0),), "1000", ()


class _MultiSessionCompletingExecutor:
    """Completes every task type generically; the options task marks
    Tuesday 2026-08-11 -- and only 2026-08-11, never Monday 2026-08-10 --
    captured on the shared state source, exactly mirroring the real
    ``AlpacaOptionsResearchEvidenceCapture``/checkpoint contract's identity
    (one retained checkpoint per genuinely fresh session)."""

    def __init__(self, state_source: _MultiSessionOptionsStateSource) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self._state_source = state_source

    def execute(self, *, task: object, executed_at: datetime, **_: object) -> TaskExecution:
        task_type = task.task_type.value  # type: ignore[attr-defined]
        self.calls.append((task_type, executed_at))
        if task.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE:  # type: ignore[attr-defined]
            self._state_source.captured_session_ids.add("2026-08-11")
            return TaskExecution(
                OperatingTaskStatus.COMPLETED,
                ("options-forward-evidence-checkpoint-2026-08-11",),
                (),
                True,
                3,
            )
        return TaskExecution(OperatingTaskStatus.COMPLETED, (f"{task_type}-output",), (), True, 0)


class TestOptionsCaptureMultiSessionTemporalReevaluation:
    """The mandatory multi-session regression (Options Capture Temporal
    Reevaluation Correctness v2): Monday 2026-08-10 remains genuinely
    uncaptured (exactly like the real, honestly-missed production session --
    never backfilled here either) while Tuesday 2026-08-11's own cycle
    walks through T1..T5 of the exact real 2026-08-11 timeline through the
    real ``ExecuteOperatingCycle``/``_resume_cycle`` orchestration. This
    test fails against the pre-fix behavior: before this milestone,
    ``options_capture_awaits_same_day_reevaluation`` never recognized
    ``MISSED_CAPTURE_WINDOW`` (for the *prior* session) as provisional, so
    T1 would immediately freeze a permanent ``SKIPPED_NOT_DUE`` result and
    T3's genuine capture would never be reached."""

    def test_prior_day_missed_session_does_not_freeze_a_later_eligible_window(self) -> None:
        as_of = datetime(2026, 8, 11, tzinfo=UTC)
        cycle_id = "cycle-options-multi-session"
        state_source = _MultiSessionOptionsStateSource()
        executor = _MultiSessionCompletingExecutor(state_source)
        repository = _MemoryRepository(_plan())
        plan_id = _plan().plan_id

        # T1: 2026-08-11T01:17Z -- the exact real production instant. Local
        # America/New_York time is still 2026-08-10 evening; the due-state
        # function correctly reports MISSED_CAPTURE_WINDOW for 2026-08-10
        # (its own established priority, unchanged), but a later, genuinely
        # eligible window (2026-08-11) is still ahead within this UTC-day
        # cycle's own horizon -- so this must remain reevaluable, not be
        # persisted as a frozen terminal result.
        t1 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=datetime(2026, 8, 11, 1, 17, tzinfo=UTC),
            idempotency_key=f"{cycle_id}-key",
        )
        assert "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" not in {
            item.task_type.value for item in t1.tasks
        }
        assert not any(call[0] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" for call in executor.calls)
        t1_options_due = next(
            item
            for item in t1.due_work.tasks
            if item.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE
        )
        assert t1_options_due.disposition is DueDisposition.NOT_DUE
        assert "MISSED_CAPTURE_WINDOW" in t1_options_due.reasons
        assert "2026-08-10" in t1_options_due.source_evidence_ids
        assert "FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON" in t1_options_due.reasons
        # The legitimate prior-session alert fires at cycle creation, exactly
        # matching the real retained 2026-08-11T01:17Z production evidence.
        t1_alerts = derive_operating_cycle_alert_candidates(t1)
        missed_alert = next(
            item for item in t1_alerts if item.category == "OPTIONS_CAPTURE_WINDOW_MISSED"
        )
        assert "2026-08-10" in missed_alert.source_evidence_ids

        # T2: 2026-08-11T19:55Z -- still before today's own window. Still no
        # capture; still genuinely reevaluable, not a second frozen result.
        t2 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=datetime(2026, 8, 11, 19, 55, tzinfo=UTC),
            idempotency_key=f"{cycle_id}-key",
        )
        assert "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" not in {
            item.task_type.value for item in t2.tasks
        }
        assert not any(call[0] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" for call in executor.calls)
        assert t2.idempotent_replay

        # T3: 2026-08-11T20:05Z -- Tuesday's own session is now genuinely
        # DUE. A fresh ``ExecuteOperatingCycle`` instance (simulating a
        # supervised-worker restart) must discover and execute it exactly
        # once.
        t3 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=datetime(2026, 8, 11, 20, 5, tzinfo=UTC),
            idempotency_key=f"{cycle_id}-key",
        )
        t3_options_result = next(
            item for item in t3.tasks if item.task_type.value == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
        )
        assert t3_options_result.status is OperatingTaskStatus.COMPLETED
        assert not t3.idempotent_replay
        options_calls = [c for c in executor.calls if c[0] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"]
        assert len(options_calls) == 1
        # No missed-window candidate this call -- a genuine capture must
        # never itself also read as a missed alert.
        t3_alerts = derive_operating_cycle_alert_candidates(t3)
        assert not any(item.category == "OPTIONS_CAPTURE_WINDOW_MISSED" for item in t3_alerts)

        # T4: 2026-08-11T20:10Z -- still inside the window, another poll: no
        # duplicate provider capture, no duplicate checkpoint.
        t4 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=datetime(2026, 8, 11, 20, 10, tzinfo=UTC),
            idempotency_key=f"{cycle_id}-key",
        )
        assert t4.idempotent_replay
        assert len([c for c in executor.calls if c[0] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"]) == 1

        # T5: restart/recreate the runner well after the window closed --
        # the same completed 2026-08-11 session remains terminal/idempotent.
        t5 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=datetime(2026, 8, 11, 23, 0, tzinfo=UTC),
            idempotency_key=f"{cycle_id}-key",
        )
        assert t5.idempotent_replay
        final_options_result = next(
            item for item in t5.tasks if item.task_type.value == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
        )
        assert final_options_result.output_identities == t3_options_result.output_identities
        assert len([c for c in executor.calls if c[0] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"]) == 1

        # 2026-08-10 was never captured -- honestly missed, never backfilled.
        assert state_source.captured_session_ids == {"2026-08-11"}

    def test_today_own_window_also_missed_becomes_terminal_and_alerts_distinctly(self) -> None:
        """Scenario D + the compound case Part 9 requires: when TODAY's own
        session *also* closes genuinely uncaptured (e.g. the host was down
        through the entire window), it must become terminal once no later
        eligible window remains this cycle, and its own missed-window alert
        must be retained distinctly from the earlier prior-day alert --
        never colliding on the same alert identity."""

        as_of = datetime(2026, 8, 11, tzinfo=UTC)
        cycle_id = "cycle-options-multi-session-both-missed"
        state_source = _MultiSessionOptionsStateSource()
        executor = _MultiSessionCompletingExecutor(state_source)
        repository = _MemoryRepository(_plan())
        plan_id = _plan().plan_id

        t1 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=datetime(2026, 8, 11, 1, 17, tzinfo=UTC),
            idempotency_key=f"{cycle_id}-key",
        )
        t1_alerts = derive_operating_cycle_alert_candidates(t1)
        prior_day_alert = next(
            item for item in t1_alerts if item.category == "OPTIONS_CAPTURE_WINDOW_MISSED"
        )
        assert "2026-08-10" in prior_day_alert.source_evidence_ids

        # The host never polls during 2026-08-11's own window at all; the
        # next poll happens after it has already closed.
        t2 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=datetime(2026, 8, 11, 20, 20, tzinfo=UTC),
            idempotency_key=f"{cycle_id}-key",
        )
        options_result = next(
            item for item in t2.tasks if item.task_type.value == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
        )
        assert options_result.status is OperatingTaskStatus.SKIPPED_NOT_DUE
        assert not t2.idempotent_replay
        t2_options_due = next(
            item
            for item in t2.due_work.tasks
            if item.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE
        )
        assert "MISSED_CAPTURE_WINDOW" in t2_options_due.reasons
        assert "2026-08-11" in t2_options_due.source_evidence_ids
        # No later session within this cycle's own UTC-day horizon: terminal.
        assert "FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON" not in t2_options_due.reasons

        t2_alerts = derive_operating_cycle_alert_candidates(t2)
        today_alert = next(
            item for item in t2_alerts if item.category == "OPTIONS_CAPTURE_WINDOW_MISSED"
        )
        assert "2026-08-11" in today_alert.source_evidence_ids
        # Two genuinely distinct missed sessions -- never the same message.
        assert today_alert.message != prior_day_alert.message

        # A further poll is a cheap, pure, terminal replay.
        t3 = ExecuteOperatingCycle(repository, state_source, executor).execute(  # type: ignore[arg-type]
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            executed_at=datetime(2026, 8, 11, 23, 0, tzinfo=UTC),
            idempotency_key=f"{cycle_id}-key",
        )
        assert t3.idempotent_replay
        assert not any(call[0] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" for call in executor.calls)
        assert state_source.captured_session_ids == set()


# --------------------------------------------------------------------------
# PORTFOLIO_SNAPSHOT_REFRESH / MANDATE_EVALUATION_REBUILD: due-disposition
# mapping, operating separation, and evaluated_at threading (Recurring
# Operating Cycle Integration v2)
# --------------------------------------------------------------------------


def _task(due: object, task_type: OperatingTaskType) -> object:
    return next(item for item in due.tasks if item.task_type is task_type)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# STRATEGY_PAPER_TRIAL_EXECUTION: due-disposition mapping and operating
# separation (Recurring Strategy PAPER Orchestration v1)
# --------------------------------------------------------------------------


class TestStrategyPaperTrialExecutionDueMapping:
    def test_unconditionally_due_every_cycle(self) -> None:
        # Unlike CRYPTO_PAPER_TRIAL_EXECUTION/KALSHI_PAPER_TRIAL_EXECUTION,
        # this task has no dependency on a Cross-Venue-Allocation decision
        # at all -- its own candidates are sourced independently (see the
        # executor branch's own docstring).
        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-strategy-due", as_of=NOW, retained=_state()
        )
        task = _task(due, OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION)
        assert task.disposition is DueDisposition.DUE
        assert task.dependency_types == ()
        assert task.source_evidence_ids == ()

    def test_due_even_when_no_allocation_decision_exists(self) -> None:
        # The older CRYPTO/KALSHI PAPER trial tasks are BLOCKED without an
        # allocation_id -- this one is structurally independent of that
        # pipeline and must never be blocked by its absence.
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-strategy-no-allocation",
            as_of=NOW,
            retained=_state(allocation_id=None, allocation_hash=None),
        )
        crypto_trial = _task(due, OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION)
        strategy_trial = _task(due, OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION)
        assert crypto_trial.disposition is DueDisposition.BLOCKED
        assert strategy_trial.disposition is DueDisposition.DUE

    def test_ordinal_after_kalshi_paper_trial_and_before_portfolio_snapshot(self) -> None:
        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-strategy-order", as_of=NOW, retained=_state()
        )
        assert tuple(item.task_type for item in due.tasks) == due.dependency_order
        kalshi_trial = _task(due, OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION)
        strategy_trial = _task(due, OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION)
        portfolio = _task(due, OperatingTaskType.PORTFOLIO_SNAPSHOT_REFRESH)
        assert kalshi_trial.ordinal < strategy_trial.ordinal < portfolio.ordinal

    def test_operating_brief_build_depends_on_it_transitively(self) -> None:
        # OPERATING_BRIEF_BUILD depends on every task in TASK_ORDER[:-1]
        # generically -- confirms the new task type was actually added to
        # TASK_ORDER, not just the enum.
        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-strategy-brief-dep", as_of=NOW, retained=_state()
        )
        brief = _task(due, OperatingTaskType.OPERATING_BRIEF_BUILD)
        assert OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION in brief.dependency_types


class TestPortfolioSnapshotRefreshDueMapping:
    def test_unconditionally_due_every_cycle(self) -> None:
        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-portfolio-due", as_of=NOW, retained=_state()
        )
        task = _task(due, OperatingTaskType.PORTFOLIO_SNAPSHOT_REFRESH)
        assert task.disposition is DueDisposition.DUE
        assert task.dependency_types == ()

    def test_ordinal_between_kalshi_paper_trial_and_mandate_evaluation(self) -> None:
        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-portfolio-order", as_of=NOW, retained=_state()
        )
        assert tuple(item.task_type for item in due.tasks) == due.dependency_order
        kalshi_trial = _task(due, OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION)
        portfolio = _task(due, OperatingTaskType.PORTFOLIO_SNAPSHOT_REFRESH)
        mandate = _task(due, OperatingTaskType.MANDATE_EVALUATION_REBUILD)
        assert kalshi_trial.ordinal < portfolio.ordinal < mandate.ordinal


class TestMandateEvaluationRebuildDueMapping:
    def test_no_mandate_ever_authored_is_unavailable(self) -> None:
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-mandate-never-created",
            as_of=NOW,
            retained=_state(mandate_lineage_has_any_version=False, active_mandate_id=None),
        )
        task = _task(due, OperatingTaskType.MANDATE_EVALUATION_REBUILD)
        assert task.disposition is DueDisposition.UNAVAILABLE
        assert "MANDATE_LINEAGE_NEVER_CREATED" in task.reasons

    def test_mandate_exists_but_not_yet_effective_is_unavailable(self) -> None:
        """A mandate lineage with a version authored for a *future*
        ``effective_at`` must fail exactly as closed as no mandate at all --
        never treated as permissive in the interim."""

        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-mandate-not-effective",
            as_of=NOW,
            retained=_state(mandate_lineage_has_any_version=True, active_mandate_id=None),
        )
        task = _task(due, OperatingTaskType.MANDATE_EVALUATION_REBUILD)
        assert task.disposition is DueDisposition.UNAVAILABLE
        assert "MANDATE_LINEAGE_EXISTS_BUT_NOT_YET_EFFECTIVE" in task.reasons

    def test_active_mandate_but_no_shared_set_is_blocked(self) -> None:
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-mandate-blocked",
            as_of=NOW,
            retained=_state(
                mandate_lineage_has_any_version=True,
                active_mandate_id="mandate-1",
                shared_set_id=None,
            ),
        )
        task = _task(due, OperatingTaskType.MANDATE_EVALUATION_REBUILD)
        assert task.disposition is DueDisposition.BLOCKED
        assert "SHARED_OPPORTUNITY_SET_REQUIRED" in task.reasons

    def test_active_mandate_and_shared_set_is_due(self) -> None:
        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-mandate-due",
            as_of=NOW,
            retained=_state(
                mandate_lineage_has_any_version=True,
                active_mandate_id="mandate-1",
                active_mandate_lineage_id="default",
                active_mandate_version=1,
                shared_set_id="shared-1",
            ),
        )
        task = _task(due, OperatingTaskType.MANDATE_EVALUATION_REBUILD)
        assert task.disposition is DueDisposition.DUE
        assert task.dependency_types == (OperatingTaskType.SHARED_OPPORTUNITY_REBUILD,)
        assert "mandate-1" in task.source_evidence_ids
        assert "shared-1" in task.source_evidence_ids


class _PortfolioMandateOnlyFailingExecutor:
    """Fails exactly PORTFOLIO_SNAPSHOT_REFRESH and MANDATE_EVALUATION_REBUILD;
    completes every other due task -- proves neither new task blocks or is
    blocked by crypto/Kalshi/options (``dependency_types=()`` for the
    portfolio task), while still respecting MANDATE_EVALUATION_REBUILD's
    genuine dependency on SHARED_OPPORTUNITY_REBUILD."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, *, task: object, **_: object) -> TaskExecution:
        self.calls.append(task.task_type.value)  # type: ignore[attr-defined]
        if task.task_type in (  # type: ignore[attr-defined]
            OperatingTaskType.PORTFOLIO_SNAPSHOT_REFRESH,
            OperatingTaskType.MANDATE_EVALUATION_REBUILD,
        ):
            raise RuntimeError("fixture: genuine portfolio/mandate failure")
        return TaskExecution(OperatingTaskStatus.COMPLETED, ("fixture-output",), (), True, 0)


class TestPortfolioMandateFailureIsolation:
    def _retained(self) -> RetainedOperatingState:
        return _state(
            crypto_universe_created_at=NOW - timedelta(days=7),
            crypto_universe_refresh_supported=True,
            crypto_pulse_as_of=NOW - timedelta(days=1),
            total_market_source_batch_id="batch-old",
            kalshi_latest_at=NOW - timedelta(minutes=10),
            kalshi_refresh_supported=True,
            options_capture_due_status="NOT_DUE",
            mandate_lineage_has_any_version=True,
            active_mandate_id="mandate-1",
            active_mandate_lineage_id="default",
            active_mandate_version=1,
        )

    def test_portfolio_and_mandate_failures_do_not_block_crypto_kalshi_or_options(self) -> None:
        plan = _plan()
        repository = _MemoryRepository(plan)
        source = _StateSource(self._retained())
        executor = _PortfolioMandateOnlyFailingExecutor()
        service = ExecuteOperatingCycle(repository, source, executor)  # type: ignore[arg-type]
        result = service.execute(
            cycle_id="cycle-portfolio-mandate-isolated",
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key="cycle-portfolio-mandate-isolated-key",
        )
        by_type = {item.task_type.value: item for item in result.tasks}
        assert by_type["PORTFOLIO_SNAPSHOT_REFRESH"].status is OperatingTaskStatus.FAILED
        assert by_type["MANDATE_EVALUATION_REBUILD"].status is OperatingTaskStatus.FAILED
        assert by_type["CRYPTO_UNIVERSE_REFRESH"].status is OperatingTaskStatus.COMPLETED
        assert by_type["CRYPTO_MARKET_PULSE"].status is OperatingTaskStatus.COMPLETED
        assert by_type["KALSHI_MARKET_REFRESH"].status is OperatingTaskStatus.COMPLETED
        assert by_type["SHARED_OPPORTUNITY_REBUILD"].status is OperatingTaskStatus.COMPLETED

    def test_shared_opportunity_failure_genuinely_blocks_mandate_evaluation(self) -> None:
        """The one real dependency MANDATE_EVALUATION_REBUILD has: unlike
        the portfolio task, a failed SHARED_OPPORTUNITY_REBUILD this cycle
        must propagate to BLOCKED rather than attempting to evaluate against
        stale or absent Opportunity evidence."""

        class _SharedOpportunityFailingExecutor:
            def execute(self, *, task: object, **_: object) -> TaskExecution:
                if task.task_type is OperatingTaskType.SHARED_OPPORTUNITY_REBUILD:  # type: ignore[attr-defined]
                    raise RuntimeError("fixture: genuine shared-opportunity failure")
                return TaskExecution(
                    OperatingTaskStatus.COMPLETED, ("fixture-output",), (), True, 0
                )

        plan = _plan()
        repository = _MemoryRepository(plan)
        source = _StateSource(self._retained())
        service = ExecuteOperatingCycle(
            repository,
            source,
            _SharedOpportunityFailingExecutor(),  # type: ignore[arg-type]
        )
        result = service.execute(
            cycle_id="cycle-shared-failure-blocks-mandate",
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key="cycle-shared-failure-blocks-mandate-key",
        )
        by_type = {item.task_type.value: item for item in result.tasks}
        assert by_type["SHARED_OPPORTUNITY_REBUILD"].status is OperatingTaskStatus.FAILED
        assert by_type["MANDATE_EVALUATION_REBUILD"].status is OperatingTaskStatus.BLOCKED
        assert "DEPENDENCY_FAILED" in by_type["MANDATE_EVALUATION_REBUILD"].warning_codes
        # Portfolio refresh has no such dependency and still completes.
        assert by_type["PORTFOLIO_SNAPSHOT_REFRESH"].status is OperatingTaskStatus.COMPLETED


# --------------------------------------------------------------------------
# COUNTERFACTUAL_FOLLOWUP_REFRESH: due-disposition mapping and failure
# isolation (Counterfactual Follow-Up Recurring Integration v1). The genuine
# execution behavior (reusing SyncFollowUpObservations, the Kalshi request
# budget, restart-safe idempotency) is proven against real PostgreSQL in
# tests/persistence/test_postgres_counterfactual_followup_recurring.py --
# this section covers only the pure scheduling contract.
# --------------------------------------------------------------------------


class TestCounterfactualFollowUpRefreshDueMapping:
    def test_due_every_cycle_by_default(self) -> None:
        """Due whenever nothing about the options-capture task makes it
        defer (Part: Scheduling/Operational Priority for Recurring Follow-Up
        vs. Time-Critical Options Capture) -- ``_state()``'s own defaults
        report options as ``NOT_DUE`` with no known next-window fact, so
        neither deferral condition can fire.  See
        ``TestCounterfactualFollowUpDeferredForOptionsPriority`` below for the
        cases where it genuinely does defer."""

        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-followup-due", as_of=NOW, retained=_state()
        )
        task = _task(due, OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH)
        assert task.disposition is DueDisposition.DUE
        assert task.dependency_types == ()
        assert task.source_evidence_ids == ()

    def test_ordinal_after_research_operations_refresh_before_brief_build(self) -> None:
        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-followup-order", as_of=NOW, retained=_state()
        )
        assert tuple(item.task_type for item in due.tasks) == due.dependency_order
        research = _task(due, OperatingTaskType.RESEARCH_OPERATIONS_REFRESH)
        followup = _task(due, OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH)
        brief = _task(due, OperatingTaskType.OPERATING_BRIEF_BUILD)
        assert research.ordinal < followup.ordinal < brief.ordinal

    def test_deterministic_and_replayable(self) -> None:
        first = plan_due_work(
            plan=_plan(), cycle_id="cycle-followup-replay", as_of=NOW, retained=_state()
        )
        second = plan_due_work(
            plan=_plan(), cycle_id="cycle-followup-replay", as_of=NOW, retained=_state()
        )
        assert _task(first, OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH) == _task(
            second, OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH
        )

    def test_operating_brief_build_lists_it_as_a_dependency_but_is_never_blocked_by_it(
        self,
    ) -> None:
        """``OPERATING_BRIEF_BUILD``'s ``dependency_types`` are derived from
        ``TASK_ORDER[:-1]`` so it always lists every prior task, including
        this new one -- but ``_dispatch_task`` hard-codes ``OPERATING_BRIEF_
        BUILD`` as immune to ``dependency_failed`` for every task type, so
        this is purely informational, never an actual same-cycle gate."""

        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-followup-brief-dep", as_of=NOW, retained=_state()
        )
        brief = _task(due, OperatingTaskType.OPERATING_BRIEF_BUILD)
        assert OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH in brief.dependency_types


class TestCounterfactualOutcomeRefreshDueMapping:
    """Mirrors ``TestCounterfactualFollowUpRefreshDueMapping`` above exactly
    for Counterfactual Outcome Feedback v1's own recurring task, inserted
    immediately after it in ``TASK_ORDER``."""

    def test_due_every_cycle_by_default(self) -> None:
        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-outcome-due", as_of=NOW, retained=_state()
        )
        task = _task(due, OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH)
        assert task.disposition is DueDisposition.DUE
        assert task.dependency_types == (OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH,)
        assert task.source_evidence_ids == ()
        assert task.network_behavior == "LOCAL_RETAINED_READS_ONLY_NO_PROVIDER_REQUEST"

    def test_ordinal_after_counterfactual_followup_refresh_before_brief_build(self) -> None:
        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-outcome-order", as_of=NOW, retained=_state()
        )
        assert tuple(item.task_type for item in due.tasks) == due.dependency_order
        followup = _task(due, OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH)
        outcome = _task(due, OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH)
        brief = _task(due, OperatingTaskType.OPERATING_BRIEF_BUILD)
        assert followup.ordinal < outcome.ordinal < brief.ordinal

    def test_deterministic_and_replayable(self) -> None:
        first = plan_due_work(
            plan=_plan(), cycle_id="cycle-outcome-replay", as_of=NOW, retained=_state()
        )
        second = plan_due_work(
            plan=_plan(), cycle_id="cycle-outcome-replay", as_of=NOW, retained=_state()
        )
        assert _task(first, OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH) == _task(
            second, OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH
        )

    def test_operating_brief_build_lists_it_as_a_dependency_but_is_never_blocked_by_it(
        self,
    ) -> None:
        due = plan_due_work(
            plan=_plan(), cycle_id="cycle-outcome-brief-dep", as_of=NOW, retained=_state()
        )
        brief = _task(due, OperatingTaskType.OPERATING_BRIEF_BUILD)
        assert OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH in brief.dependency_types


class TestCounterfactualFollowUpDeferredForOptionsPriority:
    """Scheduling/Operational Priority for Recurring Follow-Up vs.
    Time-Critical Options Capture: pins the exact predicate
    (``counterfactual_followup_deferred_for_options_priority``) and its one
    ``plan_due_work`` call site -- independent of the full multi-session
    cycle-level tests above, mirroring
    ``TestOptionsCaptureHorizonReasonAndReevaluationPredicate``'s own idiom
    for the sibling options-horizon predicate."""

    # -- pure predicate, no plan_due_work involved --

    def test_defers_when_options_task_is_itself_due(self) -> None:
        options_task = DueTask.create(
            task_type=OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE,
            disposition=DueDisposition.DUE,
            reasons=("WITHIN_POST_CLOSE_CAPTURE_WINDOW",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id="cycle-predicate-options-due",
            network_behavior="BOUNDED_ALPACA_OPTIONS_GET_ONLY_WHEN_DUE_AND_SESSION_WINDOW_OPEN",
        )
        # Even with no known next window at all -- the window open *right
        # now* is reason enough on its own.
        assert (
            counterfactual_followup_deferred_for_options_priority(
                options_task=options_task, next_window_open_at=None, as_of=NOW
            )
            is True
        )

    def test_does_not_defer_when_no_next_window_is_known(self) -> None:
        options_task = DueTask.create(
            task_type=OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE,
            disposition=DueDisposition.NOT_DUE,
            reasons=("BEFORE_CAPTURE_WINDOW",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id="cycle-predicate-no-window",
            network_behavior="BOUNDED_ALPACA_OPTIONS_GET_ONLY_WHEN_DUE_AND_SESSION_WINDOW_OPEN",
        )
        assert (
            counterfactual_followup_deferred_for_options_priority(
                options_task=options_task, next_window_open_at=None, as_of=NOW
            )
            is False
        )

    def test_does_not_defer_when_next_window_is_comfortably_far_away(self) -> None:
        options_task = DueTask.create(
            task_type=OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE,
            disposition=DueDisposition.NOT_DUE,
            reasons=("BEFORE_CAPTURE_WINDOW",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id="cycle-predicate-far-window",
            network_behavior="BOUNDED_ALPACA_OPTIONS_GET_ONLY_WHEN_DUE_AND_SESSION_WINDOW_OPEN",
        )
        far = NOW + COUNTERFACTUAL_FOLLOWUP_OPTIONS_GUARD_INTERVAL + timedelta(microseconds=1)
        assert (
            counterfactual_followup_deferred_for_options_priority(
                options_task=options_task, next_window_open_at=far, as_of=NOW
            )
            is False
        )

    def test_defers_when_next_window_is_imminent(self) -> None:
        """The 19:55-ish scenario: options is not itself due yet, but its
        next window opens soon enough that a slow follow-up cycle could
        otherwise cost the timer's genuine re-evaluation of it (Part:
        systemd does not queue a second concurrent oneshot firing)."""

        options_task = DueTask.create(
            task_type=OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE,
            disposition=DueDisposition.NOT_DUE,
            reasons=("BEFORE_CAPTURE_WINDOW",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id="cycle-predicate-imminent-window",
            network_behavior="BOUNDED_ALPACA_OPTIONS_GET_ONLY_WHEN_DUE_AND_SESSION_WINDOW_OPEN",
        )
        soon = NOW + timedelta(minutes=5)
        assert (
            counterfactual_followup_deferred_for_options_priority(
                options_task=options_task, next_window_open_at=soon, as_of=NOW
            )
            is True
        )

    def test_defers_at_exact_guard_interval_boundary_never_just_outside_it(self) -> None:
        options_task = DueTask.create(
            task_type=OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE,
            disposition=DueDisposition.NOT_DUE,
            reasons=("BEFORE_CAPTURE_WINDOW",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id="cycle-predicate-boundary",
            network_behavior="BOUNDED_ALPACA_OPTIONS_GET_ONLY_WHEN_DUE_AND_SESSION_WINDOW_OPEN",
        )
        exactly_at_guard = NOW + COUNTERFACTUAL_FOLLOWUP_OPTIONS_GUARD_INTERVAL
        assert (
            counterfactual_followup_deferred_for_options_priority(
                options_task=options_task, next_window_open_at=exactly_at_guard, as_of=NOW
            )
            is True
        )
        just_outside = exactly_at_guard + timedelta(microseconds=1)
        assert (
            counterfactual_followup_deferred_for_options_priority(
                options_task=options_task, next_window_open_at=just_outside, as_of=NOW
            )
            is False
        )

    def test_does_not_defer_when_options_capability_is_unavailable(self) -> None:
        """A missing options capability is not a time-critical-window
        concern -- it must never make follow-up wait for a window that will
        never open."""

        options_task = DueTask.create(
            task_type=OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE,
            disposition=DueDisposition.UNAVAILABLE,
            reasons=("OPTIONS_CAPTURE_CAPABILITY_NOT_RETAINED",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id="cycle-predicate-unavailable",
            network_behavior="BOUNDED_ALPACA_OPTIONS_GET_ONLY_WHEN_DUE_AND_SESSION_WINDOW_OPEN",
        )
        assert (
            counterfactual_followup_deferred_for_options_priority(
                options_task=options_task, next_window_open_at=None, as_of=NOW
            )
            is False
        )

    # -- wired through plan_due_work end to end --

    def test_plan_due_work_defers_followup_when_options_window_is_imminent(self) -> None:
        """Scenario B (19:55-ish): options genuinely not due yet, but its
        next window opens inside the guard interval -- follow-up correctly
        yields (``NOT_DUE``, never ``BLOCKED``: nothing is wrong) instead of
        risking a long cycle that could cost the *next* timer tick's genuine
        re-evaluation of the about-to-open window."""

        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-followup-defer-imminent",
            as_of=NOW,
            retained=_state(
                options_capture_due_status="BEFORE_CAPTURE_WINDOW",
                options_capture_next_window_open_at=NOW + timedelta(minutes=5),
            ),
        )
        followup = _task(due, OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH)
        assert followup.disposition is DueDisposition.NOT_DUE
        assert followup.reasons == (COUNTERFACTUAL_FOLLOWUP_DEFERRED_FOR_OPTIONS_PRIORITY,)
        assert followup.dependency_types == ()
        # Same-cycle ordering is untouched: options is still evaluated (and
        # would still dispatch, were it DUE) strictly before follow-up.
        options = _task(due, OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE)
        assert options.ordinal < followup.ordinal

    def test_plan_due_work_leaves_followup_due_when_next_window_is_far_away(self) -> None:
        """Scenario A (ordinary time): follow-up executes normally when no
        options window is imminent -- proves the guard does not overreach."""

        due = plan_due_work(
            plan=_plan(),
            cycle_id="cycle-followup-no-defer",
            as_of=NOW,
            retained=_state(
                options_capture_due_status="BEFORE_CAPTURE_WINDOW",
                options_capture_next_window_open_at=(
                    NOW + COUNTERFACTUAL_FOLLOWUP_OPTIONS_GUARD_INTERVAL + timedelta(hours=1)
                ),
            ),
        )
        followup = _task(due, OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH)
        assert followup.disposition is DueDisposition.DUE
        assert followup.reasons == ("COUNTERFACTUAL_FOLLOWUP_MANAGED_EVERY_CYCLE",)


class _FollowUpOnlyFailingExecutor:
    """Fails exactly ``COUNTERFACTUAL_FOLLOWUP_REFRESH``; completes every
    other due task -- proves it neither blocks nor is blocked by crypto,
    Kalshi, options, portfolio, or mandate (``dependency_types=()``), and
    that ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE``'s own scheduling is completely
    unaffected by this new task's presence or failure."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, *, task: object, **_: object) -> TaskExecution:
        self.calls.append(task.task_type.value)  # type: ignore[attr-defined]
        if task.task_type is OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH:  # type: ignore[attr-defined]
            raise RuntimeError("fixture: genuine counterfactual follow-up failure")
        return TaskExecution(OperatingTaskStatus.COMPLETED, ("fixture-output",), (), True, 0)


class TestCounterfactualFollowUpFailureIsolation:
    def _retained(self) -> RetainedOperatingState:
        return _state(
            crypto_universe_created_at=NOW - timedelta(days=7),
            crypto_universe_refresh_supported=True,
            crypto_pulse_as_of=NOW - timedelta(days=1),
            total_market_source_batch_id="batch-old",
            kalshi_latest_at=NOW - timedelta(minutes=10),
            kalshi_refresh_supported=True,
            options_capture_due_status="NOT_DUE",
            mandate_lineage_has_any_version=True,
            active_mandate_id="mandate-1",
            active_mandate_lineage_id="default",
            active_mandate_version=1,
            # Force OPERATING_BRIEF_BUILD genuinely DUE (the base ``_state()``
            # fixture defaults ``latest_brief_as_of=NOW``, i.e. "already built
            # today") so this test actually exercises its documented
            # immunity to ``dependency_failed`` rather than short-circuiting
            # to SKIPPED_NOT_DUE before ever reaching that rule.
            latest_brief_as_of=None,
        )

    def test_followup_failure_does_not_prevent_any_unrelated_recurring_task(self) -> None:
        plan = _plan()
        repository = _MemoryRepository(plan)
        source = _StateSource(self._retained())
        executor = _FollowUpOnlyFailingExecutor()
        service = ExecuteOperatingCycle(repository, source, executor)  # type: ignore[arg-type]
        result = service.execute(
            cycle_id="cycle-followup-isolated",
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key="cycle-followup-isolated-key",
        )
        by_type = {item.task_type.value: item for item in result.tasks}
        assert by_type["COUNTERFACTUAL_FOLLOWUP_REFRESH"].status is OperatingTaskStatus.FAILED
        # Every other task -- crypto, Kalshi, options, portfolio, and
        # mandate -- reaches its ordinary outcome under this fixture,
        # confirming dependency_types=() holds in both directions.
        assert by_type["CRYPTO_UNIVERSE_REFRESH"].status is OperatingTaskStatus.COMPLETED
        assert by_type["CRYPTO_MARKET_PULSE"].status is OperatingTaskStatus.COMPLETED
        assert by_type["KALSHI_MARKET_REFRESH"].status is OperatingTaskStatus.COMPLETED
        assert by_type["SHARED_OPPORTUNITY_REBUILD"].status is OperatingTaskStatus.COMPLETED
        assert (
            by_type["OPTIONS_RESEARCH_EVIDENCE_CAPTURE"].status
            is OperatingTaskStatus.SKIPPED_NOT_DUE
        )
        assert by_type["PORTFOLIO_SNAPSHOT_REFRESH"].status is OperatingTaskStatus.COMPLETED
        assert by_type["MANDATE_EVALUATION_REBUILD"].status is OperatingTaskStatus.COMPLETED
        # OPERATING_BRIEF_BUILD is hard-coded immune to dependency_failed.
        assert by_type["OPERATING_BRIEF_BUILD"].status is OperatingTaskStatus.COMPLETED
        # The cycle as a whole is honestly PARTIAL -- a genuine FAILED task
        # exists alongside genuinely COMPLETED ones -- never silently
        # downgraded and never escalated to a total FAILED cycle.
        assert result.state.value == "PARTIAL"
        # Options never even reached the executor (NOT_DUE short-circuits
        # before dispatch) -- its own call/no-call behavior is unchanged.
        assert "OPTIONS_RESEARCH_EVIDENCE_CAPTURE" not in executor.calls
        assert "COUNTERFACTUAL_FOLLOWUP_REFRESH" in executor.calls
        # Counterfactual Outcome Feedback v1: unlike every task above, this
        # one *does* declare a genuine same-cycle dependency on
        # COUNTERFACTUAL_FOLLOWUP_REFRESH -- proven blocked, not merely
        # unaffected, by the dedicated test class immediately below.
        assert by_type["COUNTERFACTUAL_OUTCOME_REFRESH"].status is OperatingTaskStatus.BLOCKED
        assert (
            by_type["COUNTERFACTUAL_OUTCOME_REFRESH"].error_code
            == "OPERATING_TASK_DEPENDENCY_BLOCKED"
        )
        # Blocked by a declared dependency failure means the executor was
        # never even reached for this task.
        assert "COUNTERFACTUAL_OUTCOME_REFRESH" not in executor.calls


class TestCounterfactualOutcomeRefreshDependencyOnFollowUp:
    """The one genuine same-cycle blocking edge this milestone adds: unlike
    ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` itself (``dependency_types=()``),
    ``COUNTERFACTUAL_OUTCOME_REFRESH`` declares a real dependency on it, so
    a genuine follow-up-refresh failure blocks aggregation from running
    over what would otherwise be stale/incomplete evidence this same
    cycle -- reusing ``_FollowUpOnlyFailingExecutor`` exactly as
    ``TestCounterfactualFollowUpFailureIsolation`` does above."""

    def _retained(self) -> RetainedOperatingState:
        return _state(
            crypto_universe_created_at=NOW - timedelta(days=7),
            crypto_universe_refresh_supported=True,
            crypto_pulse_as_of=NOW - timedelta(days=1),
            total_market_source_batch_id="batch-old",
            kalshi_latest_at=NOW - timedelta(minutes=10),
            kalshi_refresh_supported=True,
            options_capture_due_status="NOT_DUE",
            mandate_lineage_has_any_version=True,
            active_mandate_id="mandate-1",
            active_mandate_lineage_id="default",
            active_mandate_version=1,
            latest_brief_as_of=None,
        )

    def test_followup_failure_blocks_outcome_refresh_the_same_cycle(self) -> None:
        plan = _plan()
        repository = _MemoryRepository(plan)
        source = _StateSource(self._retained())
        executor = _FollowUpOnlyFailingExecutor()
        service = ExecuteOperatingCycle(repository, source, executor)  # type: ignore[arg-type]
        result = service.execute(
            cycle_id="cycle-outcome-blocked-by-followup",
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key="cycle-outcome-blocked-by-followup-key",
        )
        by_type = {item.task_type.value: item for item in result.tasks}
        assert by_type["COUNTERFACTUAL_FOLLOWUP_REFRESH"].status is OperatingTaskStatus.FAILED
        assert by_type["COUNTERFACTUAL_OUTCOME_REFRESH"].status is OperatingTaskStatus.BLOCKED
        assert (
            by_type["COUNTERFACTUAL_OUTCOME_REFRESH"].error_code
            == "OPERATING_TASK_DEPENDENCY_BLOCKED"
        )
        assert "DEPENDENCY_FAILED" in by_type["COUNTERFACTUAL_OUTCOME_REFRESH"].warning_codes
        assert "COUNTERFACTUAL_OUTCOME_REFRESH" not in executor.calls


class TestEvaluatedAtThreading:
    """The concrete Part A fix: the options capture due-check must see true
    wall-clock time even when the caller pins ``as_of`` to a coarser,
    day-scoped boundary (exactly what the supervised worker does) -- proven
    here at the wiring level via the fake state source's own call log; the
    genuine end-to-end proof against real XNYS calendar evidence lives in
    ``tests/persistence/test_postgres_operating_cycle.py``."""

    def test_execute_threads_executed_at_into_snapshot_as_evaluated_at(self) -> None:
        plan = _plan()
        repository = _MemoryRepository(plan)
        midnight = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        true_now = NOW.replace(hour=20, minute=5)
        retained = _state(captured_at=midnight)
        source = _StateSource(retained)
        executor = _FailingExecutor()
        service = ExecuteOperatingCycle(repository, source, executor)  # type: ignore[arg-type]
        service.execute(
            cycle_id="cycle-evaluated-at",
            plan_id=plan.plan_id,
            as_of=midnight,
            executed_at=true_now,
            idempotency_key="cycle-evaluated-at-key",
        )
        # Called twice: once to plan due-work, once to build the final
        # brief/health -- both must receive the true wall-clock instant, not
        # the day-scoped ``as_of``.
        assert source.evaluated_at_calls == [true_now, true_now]


class TestOperatingCycleAlertCandidates:
    """Part E: the existing, previously-unwired alert outbox must fire for
    genuine FAILED tasks, a missed options capture window, and repeated
    cross-cycle failures -- and must stay silent for ordinary NOT_DUE/
    healthy cycles."""

    def _healthy_result(self) -> object:
        plan = _plan()
        repository = _MemoryRepository(plan)
        source = _StateSource(_state())
        executor = _AllCompleteExecutor()
        service = ExecuteOperatingCycle(repository, source, executor)  # type: ignore[arg-type]
        return service.execute(
            cycle_id="cycle-alert-healthy",
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key="cycle-alert-healthy-key",
        )

    def test_ordinary_not_due_cycle_produces_zero_alert_candidates(self) -> None:
        candidates = derive_operating_cycle_alert_candidates(self._healthy_result())
        assert candidates == ()

    def test_failed_task_produces_a_high_severity_candidate(self) -> None:
        plan = _plan()
        repository = _MemoryRepository(plan)
        retained = _state(
            crypto_universe_created_at=NOW - timedelta(days=7),
            crypto_universe_refresh_supported=True,
            crypto_pulse_as_of=NOW - timedelta(days=1),
        )
        source = _StateSource(retained)
        executor = _FailingExecutor()
        service = ExecuteOperatingCycle(repository, source, executor)  # type: ignore[arg-type]
        result = service.execute(
            cycle_id="cycle-alert-failed",
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key="cycle-alert-failed-key",
        )
        candidates = derive_operating_cycle_alert_candidates(result)
        assert any(item.category == "OPERATING_TASK_FAILED" for item in candidates)
        failed_candidate = next(
            item for item in candidates if item.category == "OPERATING_TASK_FAILED"
        )
        assert failed_candidate.severity is AlertSeverity.HIGH
        assert "CRYPTO_MARKET_PULSE" in failed_candidate.message

    def test_missed_capture_window_produces_a_candidate_but_before_window_does_not(self) -> None:
        plan = _plan()
        missed = ExecuteOperatingCycle(
            _MemoryRepository(plan),
            _StateSource(_state(options_capture_due_status="MISSED_CAPTURE_WINDOW")),
            _AllCompleteExecutor(),
        ).execute(  # type: ignore[arg-type]
            cycle_id="cycle-alert-missed",
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key="cycle-alert-missed-key",
        )
        missed_candidates = derive_operating_cycle_alert_candidates(missed)
        assert any(item.category == "OPTIONS_CAPTURE_WINDOW_MISSED" for item in missed_candidates)

        before_window = ExecuteOperatingCycle(
            _MemoryRepository(plan),
            _StateSource(_state(options_capture_due_status="NOT_DUE")),
            _AllCompleteExecutor(),
        ).execute(  # type: ignore[arg-type]
            cycle_id="cycle-alert-before-window",
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key="cycle-alert-before-window-key",
        )
        assert derive_operating_cycle_alert_candidates(before_window) == ()

    def test_repeated_failures_produce_a_critical_candidate(self) -> None:
        plan = _plan()
        result = ExecuteOperatingCycle(
            _MemoryRepository(plan),
            _StateSource(_state(repeated_failure_count=3)),
            _AllCompleteExecutor(),
        ).execute(  # type: ignore[arg-type]
            cycle_id="cycle-alert-repeated",
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key="cycle-alert-repeated-key",
        )
        candidates = derive_operating_cycle_alert_candidates(result)
        repeated_candidate = next(
            item for item in candidates if item.category == "REPEATED_CYCLE_FAILURE"
        )
        assert repeated_candidate.severity is AlertSeverity.CRITICAL

    def test_every_candidate_category_is_accepted_by_the_real_alert_domain_vocabulary(
        self,
    ) -> None:
        """Genuine gap this regression closes: ``derive_operating_cycle_
        alert_candidates`` never itself calls ``AlertDelivery.create``, so a
        category string invalid against that domain's own closed
        vocabulary could pass every candidate-derivation test above while
        genuinely failing delivery every single time -- exactly what
        happened during this milestone's own real acceptance run before
        this test was added (``AlertDelivery.create`` rejected
        ``category="OPERATING_CYCLE_TASK_FAILED"`` with
        ``ALERT_DELIVERY_BOUND_INVALID``, silently swallowed by an
        over-broad ``except (OSError, ValueError)`` in the CLI worker).
        Every category this module can ever produce must round-trip
        through the real domain constructor."""

        from apex.domain.operational_reliability import AlertDelivery, AlertDeliveryStatus

        for category in (
            "OPERATING_TASK_FAILED",
            "OPTIONS_CAPTURE_WINDOW_MISSED",
            "REPEATED_CYCLE_FAILURE",
        ):
            delivery = AlertDelivery.create(
                alert_id=f"fixture-{category.lower()}-alert",
                category=category,
                source_evidence_ids=("fixture-evidence",),
                severity=AlertSeverity.HIGH,
                sink="LOCAL_FILE_OUTBOX",
                delivery_attempt=1,
                status=AlertDeliveryStatus.DELIVERED,
                message="fixture message",
                attempted_at=NOW,
                delivered_at=NOW,
            )
            assert delivery.category == category


class _AllCompleteExecutor:
    def execute(self, *, task: object, **_: object) -> TaskExecution:
        return TaskExecution(OperatingTaskStatus.COMPLETED, ("fixture-output",), (), True, 0)


class _FailingExecutorWithRequestCount:
    """Identical in shape to ``_FailingExecutor``, except its raised
    exception carries ``network_requests_before_failure`` -- mirroring
    exactly what ``adapters.operating_cycle_tasks``'s options-capture
    branch attaches when a genuine provider read happened before a later
    step failed (Options Automatic Capture Parent Persistence &
    Partial-Evidence Recovery v1, Part 12)."""

    def execute(self, *, task: object, **_: object) -> TaskExecution:
        if task.task_type.value == "CRYPTO_MARKET_PULSE":  # type: ignore[attr-defined]
            error = RuntimeError("simulated downstream persistence failure")
            error.network_requests_before_failure = 5  # type: ignore[attr-defined]
            raise error
        return TaskExecution(OperatingTaskStatus.COMPLETED, ("fixture-output",), (), True, 0)


def test_dispatch_reports_genuine_request_count_on_failure_and_zero_when_absent() -> None:
    """Precise proof of ``ExecuteOperatingCycle._dispatch_task``'s Part 12
    fix: an executor exception carrying ``network_requests_before_failure``
    is reflected on the retained ``TaskExecution``; an executor exception
    *without* that attribute (every task type other than options capture,
    today -- proved here via the pre-existing ``_FailingExecutor``) still
    reports exactly zero, unchanged."""

    plan = _plan()
    retained = _state(
        crypto_universe_created_at=NOW - timedelta(days=7),
        crypto_universe_refresh_supported=True,
        crypto_pulse_as_of=NOW - timedelta(days=1),
        total_market_source_batch_id="batch-old",
        kalshi_latest_at=NOW - timedelta(minutes=10),
        kalshi_refresh_supported=True,
        shared_crypto_pulse_id="pulse-old",
        latest_brief_as_of=NOW - timedelta(days=1),
    )

    baseline_result = ExecuteOperatingCycle(
        _MemoryRepository(plan),
        _StateSource(retained),
        _FailingExecutor(),  # type: ignore[arg-type]
    ).execute(
        cycle_id="cycle-request-count-baseline",
        plan_id=plan.plan_id,
        as_of=NOW,
        executed_at=NOW,
        idempotency_key="cycle-request-count-baseline-key",
    )
    baseline_failed = next(
        item for item in baseline_result.tasks if item.status is OperatingTaskStatus.FAILED
    )
    assert baseline_failed.network_requests == 0

    annotated_result = ExecuteOperatingCycle(
        _MemoryRepository(plan),
        _StateSource(retained),
        _FailingExecutorWithRequestCount(),  # type: ignore[arg-type]
    ).execute(
        cycle_id="cycle-request-count-annotated",
        plan_id=plan.plan_id,
        as_of=NOW,
        executed_at=NOW,
        idempotency_key="cycle-request-count-annotated-key",
    )
    annotated_failed = next(
        item for item in annotated_result.tasks if item.status is OperatingTaskStatus.FAILED
    )
    assert annotated_failed.network_requests == 5
    assert annotated_failed.error_code == "RuntimeError"


# --------------------------------------------------------------------------
# PAPER Continuation Health Signal v1: compute_paper_continuation_health is
# the one authoritative "is it healthy right now" classification for a task
# type whose healthy dispatches never persist a terminal OperatingTaskResult
# (awaits_intraday_continuation=True, unconditionally, on
# STRATEGY_PAPER_TRIAL_EXECUTION/CRYPTO_PAPER_TRIAL_EXECUTION/
# KALSHI_PAPER_TRIAL_EXECUTION -- see that field's own docstring). Pure,
# total, deterministic: every case below is a plain input/output assertion,
# no I/O, no clock read. See tests/persistence/
# test_postgres_strategy_paper_trial_recurring.py::TestContinuationHealthSignal
# for the real-Postgres integration proof (a genuine historical failure
# followed by real continuation, and that the original row is never
# mutated).
# --------------------------------------------------------------------------

_HEALTH_NOW = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)
_HEALTH_WINDOW = timedelta(minutes=10)


def test_continuation_health_old_failure_with_fresh_continuation_is_recovered() -> None:
    # Milestone case 1 / 3: the exact real production shape this milestone
    # exists to fix -- a terminal FAILED result days old, while genuinely
    # fresh continuation evidence (e.g. account_last_reconciled_at) exists.
    health = compute_paper_continuation_health(
        latest_terminal_status=OperatingTaskStatus.FAILED,
        latest_terminal_at=_HEALTH_NOW - timedelta(days=3),
        latest_continuation_at=_HEALTH_NOW - timedelta(minutes=2),
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
    )
    assert health.state is PaperContinuationHealthState.RECOVERED
    assert health.recovered_since_failure is True
    assert health.is_stale is False
    # The historical failure is preserved in the result, never hidden --
    # "recovered" is meaningless without what it recovered from.
    assert health.latest_terminal_status is OperatingTaskStatus.FAILED
    assert health.latest_terminal_at == _HEALTH_NOW - timedelta(days=3)
    assert health.latest_continuation_at == _HEALTH_NOW - timedelta(minutes=2)


def test_continuation_health_fresh_failure_is_failed() -> None:
    # Milestone case 2: a genuinely NEW failure must immediately supersede
    # any earlier healthy signal, regardless of freshness-window math.
    health = compute_paper_continuation_health(
        latest_terminal_status=OperatingTaskStatus.FAILED,
        latest_terminal_at=_HEALTH_NOW - timedelta(minutes=1),
        latest_continuation_at=_HEALTH_NOW - timedelta(hours=6),
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
    )
    assert health.state is PaperContinuationHealthState.FAILED
    assert health.recovered_since_failure is False
    assert health.is_stale is False


def test_continuation_health_failure_after_continuation_wins_even_if_continuation_looks_fresh() -> (
    None
):
    # A failure that happened AFTER the last known continuation evidence is
    # the most recent real signal even if that continuation evidence would
    # itself still fall inside the freshness window by elapsed time alone --
    # a genuine FAILED row always wins over a merely-not-yet-stale earlier
    # success. This also mirrors reality: once a task type is persisted
    # FAILED for a cycle_id, the scheduler stops redispatching it that UTC
    # day, so no continuation evidence newer than the failure can exist.
    health = compute_paper_continuation_health(
        latest_terminal_status=OperatingTaskStatus.FAILED,
        latest_terminal_at=_HEALTH_NOW - timedelta(minutes=5),
        latest_continuation_at=_HEALTH_NOW - timedelta(minutes=8),
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
    )
    assert health.state is PaperContinuationHealthState.FAILED


def test_continuation_health_no_failure_with_fresh_continuation_is_healthy() -> None:
    # Milestone case 4.
    health = compute_paper_continuation_health(
        latest_terminal_status=None,
        latest_terminal_at=None,
        latest_continuation_at=_HEALTH_NOW - timedelta(minutes=1),
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
    )
    assert health.state is PaperContinuationHealthState.HEALTHY
    assert health.recovered_since_failure is False
    assert health.latest_terminal_status is None
    assert health.latest_terminal_at is None


def test_continuation_health_a_completed_terminal_status_is_not_treated_as_a_failure() -> None:
    # A terminal COMPLETED/REUSED result (e.g. the one persisted UTC-day
    # rollover row) must never be mistaken for a failure just because it is
    # the "latest terminal result" on record.
    health = compute_paper_continuation_health(
        latest_terminal_status=OperatingTaskStatus.COMPLETED,
        latest_terminal_at=_HEALTH_NOW - timedelta(hours=2),
        latest_continuation_at=_HEALTH_NOW - timedelta(minutes=1),
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
    )
    assert health.state is PaperContinuationHealthState.HEALTHY
    assert health.recovered_since_failure is False


def test_continuation_health_fresh_continuation_with_no_active_work_is_idle_not_failed() -> None:
    # Milestone case 5: has_active_work is the caller's own task-specific
    # signal (e.g. "does any strategy currently hold an open trial") --
    # explicit False reads as IDLE, never FAILED.
    health = compute_paper_continuation_health(
        latest_terminal_status=None,
        latest_terminal_at=None,
        latest_continuation_at=_HEALTH_NOW - timedelta(minutes=1),
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
        has_active_work=False,
    )
    assert health.state is PaperContinuationHealthState.IDLE
    assert health.state is not PaperContinuationHealthState.FAILED


@pytest.mark.parametrize("has_active_work", [None, True])
def test_continuation_health_active_work_not_explicitly_false_is_plain_healthy(
    has_active_work: bool | None,
) -> None:
    health = compute_paper_continuation_health(
        latest_terminal_status=None,
        latest_terminal_at=None,
        latest_continuation_at=_HEALTH_NOW - timedelta(minutes=1),
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
        has_active_work=has_active_work,
    )
    assert health.state is PaperContinuationHealthState.HEALTHY


def test_continuation_health_stale_continuation_is_stale_regardless_of_terminal_status() -> None:
    # Milestone case 6: expected continuation has not been observed
    # recently enough to call current -- represented truthfully as STALE,
    # not silently upgraded to HEALTHY just because nothing ever failed.
    health = compute_paper_continuation_health(
        latest_terminal_status=None,
        latest_terminal_at=None,
        latest_continuation_at=_HEALTH_NOW - timedelta(hours=3),
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
    )
    assert health.state is PaperContinuationHealthState.STALE
    assert health.is_stale is True


def test_continuation_health_stale_after_a_recovery_still_reports_recovered_since_failure() -> None:
    # A recovery genuinely happened (continuation was newer than the
    # failure) but has since itself gone stale -- current status is
    # honestly STALE (no fresh proof of health right now), while
    # recovered_since_failure remains True as real historical context: the
    # failure was not the last thing that happened, going stale was.
    health = compute_paper_continuation_health(
        latest_terminal_status=OperatingTaskStatus.FAILED,
        latest_terminal_at=_HEALTH_NOW - timedelta(days=5),
        latest_continuation_at=_HEALTH_NOW - timedelta(hours=3),
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
    )
    assert health.state is PaperContinuationHealthState.STALE
    assert health.recovered_since_failure is True


def test_continuation_health_freshness_boundary_is_exclusive() -> None:
    # Exactly at the freshness window is NOT stale; a hair past it is.
    at_boundary = compute_paper_continuation_health(
        latest_terminal_status=None,
        latest_terminal_at=None,
        latest_continuation_at=_HEALTH_NOW - _HEALTH_WINDOW,
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
    )
    assert at_boundary.state is PaperContinuationHealthState.HEALTHY
    past_boundary = compute_paper_continuation_health(
        latest_terminal_status=None,
        latest_terminal_at=None,
        latest_continuation_at=_HEALTH_NOW - _HEALTH_WINDOW - timedelta(seconds=1),
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
    )
    assert past_boundary.state is PaperContinuationHealthState.STALE


def test_continuation_health_nothing_ever_retained_is_unknown_not_stale() -> None:
    # Neither a terminal result nor continuation evidence exists at all --
    # distinct from STALE (which means "expected but missing"): nothing was
    # ever due to have happened yet.
    health = compute_paper_continuation_health(
        latest_terminal_status=None,
        latest_terminal_at=None,
        latest_continuation_at=None,
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
    )
    assert health.state is PaperContinuationHealthState.UNKNOWN
    assert health.is_stale is False
    assert health.recovered_since_failure is False


def test_continuation_health_old_failure_with_no_continuation_evidence_at_all_is_failed() -> None:
    # A failure with nothing ever superseding it -- still genuinely FAILED,
    # not softened to UNKNOWN or STALE just because it happened long ago.
    health = compute_paper_continuation_health(
        latest_terminal_status=OperatingTaskStatus.FAILED,
        latest_terminal_at=_HEALTH_NOW - timedelta(days=30),
        latest_continuation_at=None,
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
    )
    assert health.state is PaperContinuationHealthState.FAILED


def test_continuation_health_reports_the_freshness_window_used_in_seconds() -> None:
    health = compute_paper_continuation_health(
        latest_terminal_status=None,
        latest_terminal_at=None,
        latest_continuation_at=_HEALTH_NOW,
        now=_HEALTH_NOW,
        freshness_window=timedelta(seconds=600),
    )
    assert health.freshness_window_seconds == 600


def test_continuation_health_is_pure_and_deterministic_across_repeated_calls() -> None:
    # Requirement 7 (deterministic replay creates no misleading duplicate
    # activity), at the pure-function level: identical inputs, called
    # repeatedly, always produce an identical result -- no hidden state, no
    # I/O, nothing to accidentally duplicate.
    kwargs = dict(
        latest_terminal_status=OperatingTaskStatus.FAILED,
        latest_terminal_at=_HEALTH_NOW - timedelta(days=3),
        latest_continuation_at=_HEALTH_NOW - timedelta(minutes=2),
        now=_HEALTH_NOW,
        freshness_window=_HEALTH_WINDOW,
    )
    results = [compute_paper_continuation_health(**kwargs) for _ in range(5)]  # type: ignore[arg-type]
    assert len(set(results)) == 1
