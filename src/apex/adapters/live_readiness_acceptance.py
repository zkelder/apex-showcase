"""Genuine credentials-absent and isolated fixture LIVE-readiness acceptance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.live_readiness_postgres import PostgresLiveReadinessRepository
from apex.application.live_readiness import (
    AuditBackupReadiness,
    ExportLiveReadinessEvidence,
    InMemoryLiveReadinessRepository,
    MaterializeLiveReadiness,
)
from apex.domain.live_readiness import (
    AccountEnvironment,
    AuthenticatedReadStatus,
    BackupReadinessRecord,
    BackupReadinessStatus,
    ExecutionControlEvent,
    ExecutionControlState,
    LiveReadinessAssessment,
    LiveReadinessInputs,
    LiveRiskPolicy,
    PermissionClass,
    Provider,
    ProviderAccountStatus,
    ProviderCapabilitySnapshot,
    ReadinessRecommendation,
)

PHASE_AT = datetime(2026, 8, 3, 19, 0, tzinfo=UTC)
CONTROL_ID = "apex-global-execution-control-v1"
GENUINE_ASSESSMENT_ID = "genuine-secure-live-readiness-2026-08-03-v1"
GENUINE_BACKUP_ID = "genuine-backup-readiness-2026-08-03-v1"
GENUINE_EXPORT_ID = "genuine-live-readiness-export-2026-08-03-v1"

_FINANCIAL_TABLES = (
    "campaign",
    "opportunity_proposal",
    "allocation_decision",
    "risk_decision",
    "order",
    "risk_reservation",
    "execution_event",
    "position",
    "position_settlement",
    "ledger_journal",
    "ledger_posting",
    "apex_crypto_paper_account",
    "apex_crypto_paper_state_snapshot",
    "apex_kalshi_paper_account",
    "apex_kalshi_paper_state_snapshot",
    "securities_paper_authorization",
    "securities_paper_order",
    "securities_fill_evidence",
    "securities_position_state",
    "external_paper_instruction",
    "external_paper_submission_attempt",
    "external_paper_validated_fill",
    "external_paper_fill_application",
    "external_paper_internal_execution_binding",
)


def financial_counts(engine: Engine) -> dict[str, int]:
    inspector = sa.inspect(engine)
    names = set(inspector.get_table_names())
    with engine.connect() as connection:
        return {
            table: int(
                connection.scalar(
                    sa.text(f'SELECT count(*) FROM "{table}"')  # nosec B608
                )
                or 0
            )
            for table in _FINANCIAL_TABLES
            if table in names
        }


def _absent_capability(provider: Provider) -> ProviderCapabilitySnapshot:
    return ProviderCapabilitySnapshot.credentials_absent(
        snapshot_id=f"genuine-{provider.value.lower()}-private-capability-2026-08-03-v1",
        provider=provider,
        account_scope_id=f"{provider.value.lower()}-default",
        account_environment=(
            AccountEnvironment.PAPER if provider is Provider.ALPACA else AccountEnvironment.LIVE
        ),
        observed_at=PHASE_AT,
        limitations=(
            "DEFAULT_ACCEPTANCE_DOES_NOT_RESOLVE_CREDENTIALS",
            "RETAINED_EXTERNAL_PAPER_EVIDENCE_ONLY"
            if provider is Provider.ALPACA
            else "NO_PRIVATE_ACCOUNT_EVIDENCE",
        ),
    )


def run_genuine_live_readiness_acceptance(engine: Engine) -> dict[str, object]:
    repository = PostgresLiveReadinessRepository(engine)
    before = financial_counts(engine)
    capabilities = tuple(
        repository.save_capability(_absent_capability(provider)) for provider in Provider
    )
    policy = repository.save_risk_policy(LiveRiskPolicy.v1(created_at=PHASE_AT))
    controls = repository.control_events(CONTROL_ID)
    if controls:
        control = controls[-1]
    else:
        control = repository.save_control_event(
            ExecutionControlEvent.create(
                event_id="genuine-global-live-blocked-2026-08-03-v1",
                control_id=CONTROL_ID,
                ordinal=1,
                state=ExecutionControlState.LIVE_BLOCKED,
                actor="operator:genuine-acceptance",
                reason="Secure readiness foundation defaults LIVE to blocked.",
                effective_at=PHASE_AT,
                expires_at=None,
                previous_state=None,
                idempotency_key="genuine-global-live-blocked-2026-08-03-v1",
            )
        )
    backup = AuditBackupReadiness(repository).execute(
        BackupReadinessRecord.create(
            record_id=GENUINE_BACKUP_ID,
            database_backup_method="LOCAL_POSTGRES_PG_DUMP_PROCEDURE_NOT_EXECUTED_BY_ACCEPTANCE",
            latest_verified_backup_at=None,
            restore_test_status="NOT_RUN",
            configuration_backup_status="REPOSITORY_CONFIGURATION_ONLY_SECRETS_EXCLUDED",
            credential_reference_recovery_procedure="RECREATE_OPAQUE_REFERENCE_AND_REVALIDATE_READ_ONLY",
            recovery_point_objective_seconds=86_400,
            recovery_time_objective_seconds=14_400,
            known_gaps=(
                "NO_VERIFIED_DATABASE_BACKUP",
                "NO_COMPLETED_RESTORE_TEST",
                "CREDENTIAL_REFERENCE_RECOVERY_NOT_TESTED",
            ),
            status=BackupReadinessStatus.NOT_VERIFIED,
            assessed_at=PHASE_AT,
        )
    )
    alpaca = next(item for item in capabilities if item.provider is Provider.ALPACA)
    inputs = LiveReadinessInputs(
        venue=Provider.ALPACA,
        account_scope_id="alpaca-default",
        capability=alpaca,
        strategy_or_method_id=None,
        paper_sample_count=0,
        fill_count=0,
        settlement_count=0,
        total_live_capital=Decimal("0"),
        retained_cash_ratio=Decimal("1"),
        per_opportunity_exposure=Decimal("0"),
        per_strategy_exposure=Decimal("0"),
        per_instrument_exposure=Decimal("0"),
        per_venue_exposure=Decimal("0"),
        open_order_count=0,
        open_position_count=0,
        daily_realized_loss=Decimal("0"),
        daily_modeled_loss=Decimal("0"),
        drawdown_ratio=Decimal("0"),
        strategy_health_state="INSUFFICIENT_SAMPLE",
        reconciliation_quality="UNAVAILABLE",
        unresolved_reconciliation_severity=1,
        operating_cycle_health="UNAVAILABLE",
        provider_health="UNAVAILABLE",
        backup=backup,
        control=control,
        qualifying_opportunity=False,
        explicit_operator_review=True,
        assessed_at=PHASE_AT,
    )
    assessment = MaterializeLiveReadiness(repository).execute(
        assessment_id=GENUINE_ASSESSMENT_ID,
        decision_id="genuine-live-risk-decision-2026-08-03-v1",
        policy=policy,
        inputs=inputs,
        actor="operator:genuine-acceptance",
    )
    table_names = set(sa.inspect(engine).get_table_names())
    categories = (
        ("PROVIDER_CAPABILITIES", len(capabilities)),
        ("READINESS_ASSESSMENTS", 1),
        ("RISK_DECISIONS", 1),
        ("EXECUTION_CONTROLS", len(repository.control_events(CONTROL_ID))),
        (
            "ORDERS_AND_FILLS",
            sum(
                before.get(item, 0)
                for item in (
                    "order",
                    "securities_paper_order",
                    "securities_fill_evidence",
                    "external_paper_instruction",
                    "external_paper_validated_fill",
                )
            ),
        ),
        (
            "POSITIONS_AND_SETTLEMENTS",
            sum(
                before.get(item, 0)
                for item in (
                    "position",
                    "position_settlement",
                    "securities_position_state",
                )
            ),
        ),
        ("RECONCILIATION", sum(1 for item in table_names if "reconciliation" in item)),
        ("JOURNALS", before.get("ledger_journal", 0)),
        ("AUTHORITY_HISTORY", 0),
        ("BACKUP_READINESS", 1),
    )
    export = ExportLiveReadinessEvidence(repository).execute(
        export_id=GENUINE_EXPORT_ID,
        assessment_id=assessment.assessment_id,
        categories=categories,
        evidence_ids=(
            *(item.snapshot_id for item in capabilities),
            policy.policy_id,
            control.event_id,
            backup.record_id,
            assessment.assessment_id,
        ),
        evidence_hashes=(
            *(item.canonical_hash for item in capabilities),
            policy.canonical_hash,
            control.canonical_hash,
            backup.canonical_hash,
            assessment.canonical_hash,
        ),
        paper_records=sum(before.values()),
        generated_at=PHASE_AT,
    )
    after = financial_counts(engine)
    if before != after:
        raise RuntimeError("LIVE_READINESS_ACCEPTANCE_FINANCIAL_STATE_CHANGED")
    if assessment.recommendation is not ReadinessRecommendation.LIVE_NOT_READY:
        raise RuntimeError("LIVE_READINESS_ACCEPTANCE_MUST_REMAIN_NOT_READY")
    return {
        "capabilities": capabilities,
        "risk_policy": policy,
        "execution_control": control,
        "backup_readiness": backup,
        "assessment": assessment,
        "export": export,
        "financial_counts_before": before,
        "financial_counts_after": after,
        "private_provider_requests": 0,
        "provider_writes": 0,
        "live_authority": "NONE",
    }


def _fixture_capability(
    *,
    snapshot_id: str,
    permissions: tuple[PermissionClass, ...],
    observed_at: datetime = PHASE_AT,
) -> ProviderCapabilitySnapshot:
    return ProviderCapabilitySnapshot.create(
        snapshot_id=snapshot_id,
        version=1,
        provider=Provider.COINBASE,
        account_scope_id="fixture-approved-account",
        credential_reference_id="fixture-reference",
        account_environment=AccountEnvironment.LIVE,
        authenticated_read_status=AuthenticatedReadStatus.AVAILABLE,
        permissions=permissions,
        supported_products=("SPOT",),
        fee_evidence=(("TAKER_BPS", "60"),),
        precision_constraints=(("BTC-USD", "0.01"),),
        minimum_size_constraints=(("BTC-USD", "1"),),
        order_types=("LIMIT",),
        account_status=ProviderAccountStatus.ACTIVE,
        observed_at=observed_at,
        ingested_at=observed_at,
        evidence_class="FIXTURE_PRIVATE_READ",
        provider_request_count=0,
    )


def _fixture_assessment(
    *,
    name: str,
    capability: ProviderCapabilitySnapshot | None,
    control_state: ExecutionControlState = ExecutionControlState.LIVE_PILOT_ARMED,
    paper_sample_count: int = 30,
    fill_count: int = 20,
    settlement_count: int = 20,
    health: str = "HEALTHY",
    reconciliation: str = "CLEAN",
    operating: str = "HEALTHY",
    backup_status: BackupReadinessStatus = BackupReadinessStatus.VERIFIED,
    qualifying: bool = True,
    total_live_capital: Decimal = Decimal("0"),
) -> LiveReadinessAssessment:
    repository = InMemoryLiveReadinessRepository()
    policy = LiveRiskPolicy.v1(
        created_at=PHASE_AT, allowed_account_scopes=("fixture-approved-account",)
    )
    backup = BackupReadinessRecord.create(
        record_id=f"fixture-backup-{name}",
        database_backup_method="FIXTURE_PG_DUMP",
        latest_verified_backup_at=PHASE_AT,
        restore_test_status="VERIFIED",
        configuration_backup_status="VERIFIED_SECRET_FREE",
        credential_reference_recovery_procedure="FIXTURE_REFERENCE_RECREATED",
        recovery_point_objective_seconds=3600,
        recovery_time_objective_seconds=3600,
        known_gaps=(),
        status=backup_status,
        assessed_at=PHASE_AT,
    )
    control = ExecutionControlEvent.create(
        event_id=f"fixture-control-{name}",
        control_id=f"fixture-control-{name}",
        ordinal=1,
        state=control_state,
        actor="fixture",
        reason="Deterministic fixture only.",
        effective_at=PHASE_AT,
        expires_at=PHASE_AT + timedelta(hours=1),
        previous_state=None,
        idempotency_key=f"fixture-control-{name}",
        fixture=True,
    )
    inputs = LiveReadinessInputs(
        venue=Provider.COINBASE,
        account_scope_id="fixture-approved-account",
        capability=capability,
        strategy_or_method_id="fixture-qualified-method" if qualifying else None,
        paper_sample_count=paper_sample_count,
        fill_count=fill_count,
        settlement_count=settlement_count,
        total_live_capital=total_live_capital,
        retained_cash_ratio=Decimal("1"),
        per_opportunity_exposure=Decimal("0"),
        per_strategy_exposure=Decimal("0"),
        per_instrument_exposure=Decimal("0"),
        per_venue_exposure=Decimal("0"),
        open_order_count=0,
        open_position_count=0,
        daily_realized_loss=Decimal("0"),
        daily_modeled_loss=Decimal("0"),
        drawdown_ratio=Decimal("0"),
        strategy_health_state=health,
        reconciliation_quality=reconciliation,
        unresolved_reconciliation_severity=0 if reconciliation == "CLEAN" else 1,
        operating_cycle_health=operating,
        provider_health="HEALTHY",
        backup=backup,
        control=control,
        qualifying_opportunity=qualifying,
        explicit_operator_review=True,
        assessed_at=PHASE_AT,
    )
    return MaterializeLiveReadiness(repository).execute(
        assessment_id=f"fixture-assessment-{name}",
        decision_id=f"fixture-risk-{name}",
        policy=policy,
        inputs=inputs,
        actor="fixture",
    )


def run_fixture_live_readiness_acceptance() -> dict[str, LiveReadinessAssessment]:
    private_view = _fixture_capability(
        snapshot_id="fixture-private-view",
        permissions=(PermissionClass.PRIVATE_VIEW,),
    )
    preview = _fixture_capability(
        snapshot_id="fixture-preview",
        permissions=(PermissionClass.PRIVATE_VIEW, PermissionClass.ORDER_PREVIEW),
    )
    trade = _fixture_capability(
        snapshot_id="fixture-trade",
        permissions=(PermissionClass.PRIVATE_VIEW, PermissionClass.TRADE),
    )
    dangerous = _fixture_capability(
        snapshot_id="fixture-dangerous",
        permissions=(
            PermissionClass.PRIVATE_VIEW,
            PermissionClass.TRADE,
            PermissionClass.TRANSFER,
            PermissionClass.WITHDRAWAL,
        ),
    )
    if any(item.provider_request_count != 0 for item in (private_view, preview, trade, dangerous)):
        raise RuntimeError("LIVE_READINESS_FIXTURE_PROVIDER_REQUEST_PROHIBITED")
    scenarios = {
        "credentials_absent": _fixture_assessment(name="credentials-absent", capability=None),
        "private_view_only": _fixture_assessment(name="private-view", capability=private_view),
        "preview_only": _fixture_assessment(name="preview", capability=preview),
        "trade_capable": _fixture_assessment(name="trade", capability=trade, qualifying=False),
        "dangerous_permissions": _fixture_assessment(name="dangerous", capability=dangerous),
        "stale_capability": _fixture_assessment(
            name="stale",
            capability=_fixture_capability(
                snapshot_id="fixture-stale",
                permissions=(PermissionClass.PRIVATE_VIEW, PermissionClass.TRADE),
                observed_at=PHASE_AT - timedelta(hours=1),
            ),
        ),
        "insufficient_paper": _fixture_assessment(
            name="paper", capability=trade, paper_sample_count=1, fill_count=1, settlement_count=0
        ),
        "reconciliation_failure": _fixture_assessment(
            name="reconciliation", capability=trade, reconciliation="MATERIAL_DISCREPANCY"
        ),
        "degraded_health": _fixture_assessment(name="health", capability=trade, health="DEGRADED"),
        "unhealthy_operating_cycle": _fixture_assessment(
            name="operating", capability=trade, operating="DEGRADED"
        ),
        "missing_backup": _fixture_assessment(
            name="backup", capability=trade, backup_status=BackupReadinessStatus.NOT_VERIFIED
        ),
        "risk_limit_exceeded": _fixture_assessment(
            name="risk-limit",
            capability=trade,
            total_live_capital=Decimal("100.01"),
        ),
        "emergency_stop": _fixture_assessment(
            name="stop", capability=trade, control_state=ExecutionControlState.EMERGENCY_STOP
        ),
        "fully_passing": _fixture_assessment(name="passing", capability=trade),
    }
    if scenarios["fully_passing"].recommendation is not ReadinessRecommendation.LIVE_PILOT_ELIGIBLE:
        raise RuntimeError("LIVE_READINESS_PASSING_FIXTURE_FAILED")
    if any(
        value.recommendation is ReadinessRecommendation.LIVE_PILOT_ELIGIBLE
        for key, value in scenarios.items()
        if key != "fully_passing"
    ):
        raise RuntimeError("LIVE_READINESS_BLOCKING_FIXTURE_PASSED")
    return scenarios
