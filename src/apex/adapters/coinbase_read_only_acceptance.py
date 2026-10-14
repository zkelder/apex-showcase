"""Credentials-absent genuine acceptance for Coinbase private read-only bring-up."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.engine import Engine

from apex.adapters.coinbase_read_only import (
    NetworkDisabledCoinbasePrivateTransport,
    UnavailableCoinbaseCredentialResolver,
)
from apex.adapters.coinbase_read_only_postgres import PostgresCoinbaseReadOnlyRepository
from apex.adapters.live_readiness_acceptance import financial_counts
from apex.adapters.live_readiness_postgres import PostgresLiveReadinessRepository
from apex.adapters.operational_reliability_postgres import (
    PostgresOperationalReliabilityRepository,
)
from apex.application.coinbase_read_only import (
    InspectCoinbaseReadOnlyCapability,
    RefreshCoinbaseReadOnlyCapability,
    coinbase_read_only_workspace,
)
from apex.domain.live_readiness import ExecutionControlState, ReadinessRecommendation

PHASE_AT = datetime(2026, 8, 5, 14, tzinfo=UTC)
ATTEMPT_ID = "genuine-coinbase-private-read-credentials-absent-2026-08-05-v1"
REFRESH_ID = "genuine-coinbase-private-read-refresh-2026-08-05-v1"


def run_genuine_coinbase_credentials_absent_acceptance(engine: Engine) -> dict[str, object]:
    """Retain the exact block with zero provider requests and no fabricated account data."""

    repository = PostgresCoinbaseReadOnlyRepository(engine)
    live = PostgresLiveReadinessRepository(engine)
    before = financial_counts(engine)
    captures_before = repository.captures()
    inspector = InspectCoinbaseReadOnlyCapability(
        repository,
        live,
        UnavailableCoinbaseCredentialResolver(),
        NetworkDisabledCoinbasePrivateTransport(),
    )
    result = inspector.execute(
        attempt_id=ATTEMPT_ID,
        capture_id="must-not-create-genuine-coinbase-capture",
        readiness_snapshot_id="must-not-create-genuine-coinbase-readiness-snapshot",
        approval_id=None,
        credential_reference_id=None,
        account_purpose="READ_ONLY_LIVE_CAPABILITY_INSPECTION",
        product_ids=(),
        inspected_at=PHASE_AT,
        execution_control=ExecutionControlState.LIVE_BLOCKED,
    )
    refresh = RefreshCoinbaseReadOnlyCapability(repository, inspector).execute(
        refresh_id=REFRESH_ID,
        attempt_id=ATTEMPT_ID,
        capture_id="must-not-create-genuine-coinbase-capture",
        readiness_snapshot_id="must-not-create-genuine-coinbase-readiness-snapshot",
        approval_id=None,
        credential_reference_id=None,
        account_purpose="READ_ONLY_LIVE_CAPABILITY_INSPECTION",
        product_ids=(),
        evaluated_at=PHASE_AT,
        due=True,
        execution_control=ExecutionControlState.LIVE_BLOCKED,
    )
    assessments = live.assessments()
    assessment = None if not assessments else assessments[-1]
    operational = PostgresOperationalReliabilityRepository(engine)
    health = operational.health_projections()
    after = financial_counts(engine)
    if (
        result.capture is not None
        or repository.captures() != captures_before
        or result.attempt.request_count != 0
        or result.attempt.provider_write_count != 0
        or before != after
        or (
            assessment is not None
            and assessment.recommendation is not ReadinessRecommendation.LIVE_NOT_READY
        )
    ):
        raise RuntimeError("COINBASE_CREDENTIALS_ABSENT_ACCEPTANCE_FAILED")
    return {
        "attempt": result.attempt,
        "refresh": refresh,
        "capture": None,
        "readiness": assessment,
        "operational_health": None if not health else health[-1],
        "private_provider_requests": 0,
        "provider_writes": 0,
        "financial_counts_before": before,
        "financial_counts_after": after,
        "workspace": coinbase_read_only_workspace(repository),
        "live_authority": "NONE",
    }
