from datetime import UTC, datetime, timedelta

from apex.application.sustained_shadow import ShadowWorkerResult
from apex.application.sustained_shadow_operations import (
    InMemorySustainedShadowOperationsRepository,
    RunSecuritiesShadowDailyOperation,
    latest_evidence_eligible_session,
)
from apex.domain.securities import SecuritiesCalendarSession
from apex.domain.sustained_shadow import ShadowCaptureState
from apex.domain.sustained_shadow_operations import (
    SecuritiesShadowSchedulePolicy,
    ShadowOperationState,
    forward_evidence_recommendation,
)

NOW = datetime(2026, 7, 28, 21, 0, tzinfo=UTC)


def test_daily_operation_is_idempotent_and_no_new_session_is_success() -> None:
    repository = InMemorySustainedShadowOperationsRepository()
    policy = SecuritiesShadowSchedulePolicy.create(plan_id="plan", calendar_identity="XNYS_V1")
    calls = 0

    def worker() -> ShadowWorkerResult:
        nonlocal calls
        calls += 1
        return ShadowWorkerResult(
            "plan", ShadowCaptureState.NO_NEW_SESSION, "session", 0, "session", "NO_NEW_SESSION"
        )

    service = RunSecuritiesShadowDailyOperation(
        repository=repository, worker_once=worker, resolve_once=lambda: 0
    )
    first = service.execute(
        policy=policy,
        session_id="session",
        due_at=NOW - timedelta(seconds=1),
        owner="owner",
        token="token",
        now=NOW,
    )
    second = service.execute(
        policy=policy,
        session_id="session",
        due_at=NOW - timedelta(seconds=1),
        owner="owner",
        token="token-two",
        now=NOW,
    )

    assert first.job.state is ShadowOperationState.COMPLETED_NO_NEW_SESSION
    assert second.recommendation == "IDEMPOTENT_REPLAY"
    assert calls == 1


def test_forward_gate_is_conservative() -> None:
    assert (
        forward_evidence_recommendation(
            resolved_predictions=0,
            independent_sessions=0,
            severe_drift=False,
            integrity_blocked=False,
        )
        == "REQUIRE_MORE_EVIDENCE"
    )


def test_retry_budget_is_bounded_and_terminal_failure_is_replay_stable() -> None:
    repository = InMemorySustainedShadowOperationsRepository()
    policy = SecuritiesShadowSchedulePolicy.create(plan_id="plan", calendar_identity="XNYS_V1")
    calls = 0

    def unavailable() -> ShadowWorkerResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("PROVIDER_UNAVAILABLE")

    service = RunSecuritiesShadowDailyOperation(
        repository=repository, worker_once=unavailable, resolve_once=lambda: 0
    )
    for attempt in range(policy.maximum_attempts):
        result = service.execute(
            policy=policy,
            session_id="session",
            due_at=NOW - timedelta(seconds=1),
            owner="owner",
            token=f"token-{attempt}",
            now=NOW,
        )
        assert result.job.state is ShadowOperationState.RETRYABLE_FAILURE
    exhausted = service.execute(
        policy=policy,
        session_id="session",
        due_at=NOW - timedelta(seconds=1),
        owner="owner",
        token="token-exhausted",
        now=NOW,
    )
    replay = service.execute(
        policy=policy,
        session_id="session",
        due_at=NOW - timedelta(seconds=1),
        owner="owner",
        token="token-replay",
        now=NOW,
    )

    assert calls == policy.maximum_attempts
    assert exhausted.job.state is ShadowOperationState.PERMANENT_FAILURE
    assert exhausted.recommendation == "ATTEMPT_BUDGET_EXHAUSTED"
    assert replay.recommendation == "IDEMPOTENT_REPLAY"
    assert (
        forward_evidence_recommendation(
            resolved_predictions=100,
            independent_sessions=30,
            severe_drift=True,
            integrity_blocked=False,
        )
        == "PAUSE"
    )


def test_calendar_eligibility_uses_retained_early_close_not_fixed_market_clock() -> None:
    early = SecuritiesCalendarSession.create(
        calendar_identity="XNYS",
        calendar_version="v1",
        timezone="America/New_York",
        session_date="2026-11-27",
        opens_at=datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
        closes_at=datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
        is_early_close=True,
        source_identity="retained-calendar",
    )
    policy = SecuritiesShadowSchedulePolicy.create(plan_id="plan", calendar_identity="XNYS_V1")

    assert (
        latest_evidence_eligible_session(
            sessions=(early,), policy=policy, now=early.closes_at + timedelta(seconds=900)
        )
        == early
    )
