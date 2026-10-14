"""Credentials-absent genuine acceptance for Kalshi private read-only bring-up."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy.engine import Engine

from apex.adapters.kalshi_read_only import (
    NetworkDisabledKalshiPrivateTransport,
    UnavailableKalshiCredentialResolver,
)
from apex.adapters.kalshi_read_only_postgres import PostgresKalshiReadOnlyRepository
from apex.adapters.live_readiness_acceptance import financial_counts
from apex.adapters.live_readiness_postgres import PostgresLiveReadinessRepository
from apex.adapters.operational_reliability_postgres import (
    PostgresOperationalReliabilityRepository,
)
from apex.application.kalshi_read_only import (
    InspectKalshiReadOnlyCapability,
    RefreshKalshiReadOnlyCapability,
    kalshi_read_only_workspace,
)
from apex.application.live_readiness import live_readiness_workspace
from apex.domain.live_readiness import (
    ExecutionControlState,
    LiveReadinessOperatingTask,
    LiveReadinessTaskType,
    ReadinessRecommendation,
)

PHASE_AT = datetime(2026, 8, 3, 14, tzinfo=UTC)
ATTEMPT_ID = "genuine-kalshi-private-read-credentials-absent-2026-08-03-v1"
REFRESH_ID = "genuine-kalshi-private-read-refresh-2026-08-03-v1"


def run_genuine_kalshi_credentials_absent_acceptance(engine: Engine) -> dict[str, object]:
    """Retain the exact block with zero provider requests and no fabricated account data."""

    repository = PostgresKalshiReadOnlyRepository(engine)
    live = PostgresLiveReadinessRepository(engine)
    before = financial_counts(engine)
    captures_before = repository.captures()
    inspector = InspectKalshiReadOnlyCapability(
        repository,
        live,
        UnavailableKalshiCredentialResolver(),
        NetworkDisabledKalshiPrivateTransport(),
    )
    result = inspector.execute(
        attempt_id=ATTEMPT_ID,
        capture_id="must-not-create-genuine-kalshi-capture",
        readiness_snapshot_id="must-not-create-genuine-kalshi-readiness-snapshot",
        approval_id=None,
        credential_reference_id=None,
        account_purpose="READ_ONLY_LIVE_CAPABILITY_INSPECTION",
        inspected_at=PHASE_AT,
        execution_control=ExecutionControlState.LIVE_BLOCKED,
    )
    refresh = RefreshKalshiReadOnlyCapability(repository, inspector).execute(
        refresh_id=REFRESH_ID,
        attempt_id=ATTEMPT_ID,
        capture_id="must-not-create-genuine-kalshi-capture",
        readiness_snapshot_id="must-not-create-genuine-kalshi-readiness-snapshot",
        approval_id=None,
        credential_reference_id=None,
        account_purpose="READ_ONLY_LIVE_CAPABILITY_INSPECTION",
        evaluated_at=PHASE_AT,
        due=True,
        execution_control=ExecutionControlState.LIVE_BLOCKED,
    )
    assessments = live.assessments()
    assessment = None if not assessments else assessments[-1]
    operational = PostgresOperationalReliabilityRepository(engine)
    health = operational.health_projections()
    after = financial_counts(engine)
    live_workspace = live_readiness_workspace(live)
    tasks = cast(
        tuple[LiveReadinessOperatingTask, ...],
        live_workspace["operating_cycle_tasks"],
    )
    kalshi_task = next(
        item
        for item in tasks
        if item.task_type is LiveReadinessTaskType.KALSHI_READ_ONLY_CAPABILITY_REFRESH
    )
    if (
        result.capture is not None
        or repository.captures() != captures_before
        or result.attempt.request_count != 0
        or result.attempt.provider_write_count != 0
        or before != after
        or kalshi_task.disposition.value != "CREDENTIALS_ABSENT"
        or kalshi_task.provider_request_count != 0
        or kalshi_task.provider_write_count != 0
        or (
            assessment is not None
            and assessment.recommendation is not ReadinessRecommendation.LIVE_NOT_READY
        )
    ):
        raise RuntimeError("KALSHI_CREDENTIALS_ABSENT_ACCEPTANCE_FAILED")
    return {
        "attempt": result.attempt,
        "refresh": refresh,
        "capture": None,
        "readiness": assessment,
        "operational_health": None if not health else health[-1],
        "operating_cycle_task": kalshi_task,
        "private_provider_requests": 0,
        "provider_writes": 0,
        "financial_counts_before": before,
        "financial_counts_after": after,
        "workspace": kalshi_read_only_workspace(repository),
        "live_authority": "NONE",
    }
