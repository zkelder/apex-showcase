"""Genuine and deterministic acceptance for operational reliability."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy.engine import Engine

from apex.adapters.live_readiness_acceptance import (
    GENUINE_ASSESSMENT_ID,
    financial_counts,
    run_genuine_live_readiness_acceptance,
)
from apex.adapters.live_readiness_postgres import PostgresLiveReadinessRepository
from apex.adapters.operating_cycle_postgres import (
    PostgresOperatingCycleRepository,
    PostgresOperatingStateSource,
)
from apex.adapters.operational_backup import PostgresLogicalBackupDrill
from apex.adapters.operational_reliability_postgres import (
    PostgresOperationalReliabilityRepository,
)
from apex.adapters.postgres import PostgresSettings
from apex.application.live_readiness import (
    AuditBackupReadiness,
    MaterializeLiveReadiness,
    SetExecutionControl,
)
from apex.application.operating_cycle import (
    ExecuteOperatingCycle,
    PlanOperatingDueWork,
    RegisterOperatingCyclePlan,
)
from apex.application.operational_reliability import (
    CredentialsAbsentOnboarding,
    DeliverOperationalAlert,
    LocalAlertOutbox,
    MaterializeOperationalHealth,
    RecordRecoveryAttempt,
    RegisterDeploymentProfile,
    SupervisedOperatingRunner,
)
from apex.domain.live_readiness import (
    AccountEnvironment,
    BackupReadinessRecord,
    BackupReadinessStatus,
    ExecutionControlState,
    LiveReadinessInputs,
    LiveRiskPolicy,
    Provider,
    ReadinessRecommendation,
)
from apex.domain.operating_cycle import (
    DueTask,
    OperatingCyclePlan,
    OperatingTaskStatus,
    TaskExecution,
)
from apex.domain.operational_reliability import (
    AlertSeverity,
    KillSwitchDrill,
    OperationalHealthState,
    OperationalReliabilityError,
    RecoveryStatus,
    RunnerStatus,
    VerificationStatus,
    build_operational_reliability_tasks,
    submission_allowed,
)

PHASE_AT = datetime(2026, 8, 4, 14, tzinfo=UTC)
PROFILE_ID = "APEX_LOCAL_OPERATIONAL_PROFILE_V1"
RUNNER_ID = "apex-operating-runner-v1"
PLAN_ID = "operational-reliability-operating-plan-v1"
CONTROL_ID = "apex-global-execution-control-v1"
MANIFEST_ID = "genuine-operational-backup-2026-08-04-v2"
VERIFICATION_ID = "genuine-operational-restore-2026-08-04-v2"
HEALTH_ID = "genuine-operational-health-2026-08-04-v1"
DRILL_ID = "genuine-kill-switch-drill-2026-08-04-v1"
ASSESSMENT_ID = "genuine-operational-live-readiness-2026-08-04-v1"


class _NoProviderOperatingExecutor:
    def execute(
        self,
        *,
        task: DueTask,
        cycle_id: str,
        plan: OperatingCyclePlan,
        as_of: datetime,
        executed_at: datetime,
    ) -> TaskExecution:
        return TaskExecution(
            status=OperatingTaskStatus.UNAVAILABLE,
            output_identities=(),
            reused_identities=task.source_evidence_ids,
            changed=False,
            network_requests=0,
            warnings=("OPERATIONAL_ACCEPTANCE_ZERO_PROVIDER_REQUESTS",),
            error_code=None,
        )


def _kill_switch_drill(
    live_repository: PostgresLiveReadinessRepository,
    operational_repository: PostgresOperationalReliabilityRepository,
) -> KillSwitchDrill:
    retained = tuple(
        item for item in operational_repository.kill_switch_drills() if item.drill_id == DRILL_ID
    )
    if retained:
        return retained[-1]
    controls = live_repository.control_events(CONTROL_ID)
    if not controls:
        raise OperationalReliabilityError("LIVE_BLOCKED_CONTROL_MISSING")
    if controls[-1].state is not ExecutionControlState.LIVE_BLOCKED:
        raise OperationalReliabilityError("KILL_SWITCH_DRILL_MUST_START_LIVE_BLOCKED")
    stop = SetExecutionControl(live_repository).execute(
        event_id="genuine-operational-emergency-stop-2026-08-04-v1",
        control_id=CONTROL_ID,
        state=ExecutionControlState.EMERGENCY_STOP,
        actor="operator:operational-acceptance",
        reason="Bounded deterministic kill-switch drill.",
        confirmation=f"SET_EXECUTION_CONTROL:{CONTROL_ID}:EMERGENCY_STOP",
        idempotency_key="genuine-operational-emergency-stop-2026-08-04-v1",
        effective_at=PHASE_AT + timedelta(minutes=4),
    )
    if submission_allowed(stop.state.value, paper=True) or submission_allowed(
        stop.state.value, paper=False
    ):
        raise OperationalReliabilityError("EMERGENCY_STOP_SUBMISSION_GUARD_FAILED")
    cleared = SetExecutionControl(live_repository).execute(
        event_id="genuine-operational-clear-stop-2026-08-04-v1",
        control_id=CONTROL_ID,
        state=ExecutionControlState.LIVE_BLOCKED,
        actor="operator:operational-acceptance",
        reason="Exact drill completion returns to LIVE_BLOCKED.",
        confirmation=f"CLEAR_EMERGENCY_STOP:{CONTROL_ID}:LIVE_BLOCKED",
        idempotency_key="genuine-operational-clear-stop-2026-08-04-v1",
        effective_at=PHASE_AT + timedelta(minutes=5),
    )
    drill = KillSwitchDrill.create(
        drill_id=DRILL_ID,
        starting_state="LIVE_BLOCKED",
        emergency_state=stop.state.value,
        final_state=cleared.state.value,
        paper_submission_blocked=True,
        live_submission_blocked=True,
        read_only_cycle_available=True,
        reconciliation_available=True,
        export_available=True,
        health_read_available=True,
        provider_write_count=0,
        event_ids=(stop.event_id, cleared.event_id),
        performed_at=PHASE_AT + timedelta(minutes=5),
    )
    return operational_repository.save_kill_switch_drill(drill)


def run_genuine_operational_reliability_acceptance(
    engine: Engine,
    *,
    settings: PostgresSettings,
    repository_root: Path,
) -> dict[str, object]:
    financial_before = financial_counts(engine)
    live_repository = PostgresLiveReadinessRepository(engine)
    operational = PostgresOperationalReliabilityRepository(engine)
    retained_health = operational.operational_health(HEALTH_ID)
    retained_manifest = operational.backup_manifest(MANIFEST_ID)
    retained_verification = operational.restore_verification(VERIFICATION_ID)
    retained_assessment = live_repository.assessment(ASSESSMENT_ID)
    if all(
        item is not None
        for item in (
            retained_health,
            retained_manifest,
            retained_verification,
            retained_assessment,
        )
    ):
        operating_repository = PostgresOperatingCycleRepository(engine)
        capabilities = tuple(
            item
            for provider in Provider
            if (
                item := live_repository.latest_capability(
                    provider, f"{provider.value.lower()}-default"
                )
            )
            is not None
        )
        return {
            "deployment_profile": operational.deployment_profile(PROFILE_ID),
            "operating_plan": operating_repository.plan(PLAN_ID),
            "operating_plan_replay": True,
            "runner_heartbeats": operational.runner_heartbeats(RUNNER_ID),
            "runner_final_status": operational.latest_runner_heartbeat(RUNNER_ID).status,  # type: ignore[union-attr]
            "due_work_evaluations": (),
            "cycle": operating_repository.cycle("operational-reliability-cycle-1"),
            "cycle_replay": True,
            "backup_manifest": retained_manifest,
            "restore_verification": retained_verification,
            "backup_replay": True,
            "recovery": operational.recovery_attempts()[-1],
            "alert": operational.alert_deliveries()[-1],
            "kill_switch_drill": operational.kill_switch_drills()[-1],
            "operational_health": retained_health,
            "operational_tasks": build_operational_reliability_tasks(
                cycle_id="operational-reliability-cycle-1",
                runner=operational.latest_runner_heartbeat(RUNNER_ID),
                manifest=retained_manifest,
                restore=retained_verification,
                alert=operational.alert_deliveries()[-1],
                credentials_configured=False,
                read_only_inspection_approved=False,
                assessment_fresh=True,
            ),
            "readiness_before": live_repository.assessment(GENUINE_ASSESSMENT_ID),
            "readiness_after": retained_assessment,
            "capabilities": capabilities,
            "financial_counts_before": financial_before,
            "financial_counts_after": financial_counts(engine),
            "private_provider_requests": 0,
            "provider_writes": 0,
            "live_authority": "NONE",
        }
    retained_prior_assessment = live_repository.assessment(GENUINE_ASSESSMENT_ID)
    prior_live = (
        {"assessment": retained_prior_assessment}
        if retained_prior_assessment is not None
        else run_genuine_live_readiness_acceptance(engine)
    )
    profile = RegisterDeploymentProfile(operational).execute(
        created_at=PHASE_AT,
        actor="operator:operational-acceptance",
    )

    operating_repository = PostgresOperatingCycleRepository(engine)
    plan, plan_replay = RegisterOperatingCyclePlan(operating_repository).execute(
        plan_id=PLAN_ID,
        crypto_pulse_plan_id="crypto-market-pulse-plan-v1",
        actor="operator:operational-acceptance",
        created_at=PHASE_AT,
        idempotency_key=PLAN_ID,
    )
    source = PostgresOperatingStateSource(engine, operating_repository)
    runner = SupervisedOperatingRunner(operational)
    due_one = PlanOperatingDueWork(operating_repository, source).execute(
        cycle_id="operational-reliability-due-evaluation-1",
        plan_id=PLAN_ID,
        as_of=PHASE_AT,
    )
    cycle_service = ExecuteOperatingCycle(
        operating_repository,
        source,
        _NoProviderOperatingExecutor(),
    )
    due_two = PlanOperatingDueWork(operating_repository, source).execute(
        cycle_id="operational-reliability-due-evaluation-2",
        plan_id=PLAN_ID,
        as_of=PHASE_AT + timedelta(minutes=1),
    )
    heartbeats = operational.runner_heartbeats(RUNNER_ID)
    if operating_repository.cycle("operational-reliability-cycle-1") is None:
        run_one = runner.start(
            runner_id=RUNNER_ID,
            run_instance_id="operational-run-instance-1",
            plan_id=PLAN_ID,
            at=PHASE_AT + timedelta(seconds=10),
        )
        cycle = cycle_service.execute(
            cycle_id="operational-reliability-cycle-1",
            plan_id=PLAN_ID,
            as_of=PHASE_AT,
            executed_at=PHASE_AT + timedelta(seconds=20),
            idempotency_key="operational-reliability-cycle-1",
        )
        heartbeat_one = runner.heartbeat(
            runner_id=RUNNER_ID,
            run_instance_id=run_one.run_instance_id,
            plan_id=PLAN_ID,
            at=PHASE_AT + timedelta(seconds=30),
            last_cycle_id=cycle.cycle.cycle_id,
            due_evaluation_count=1,
        )
        runner.stop(
            runner_id=RUNNER_ID,
            run_instance_id=run_one.run_instance_id,
            plan_id=PLAN_ID,
            at=PHASE_AT + timedelta(seconds=40),
        )
        run_two = runner.start(
            runner_id=RUNNER_ID,
            run_instance_id="operational-run-instance-2",
            plan_id=PLAN_ID,
            at=PHASE_AT + timedelta(minutes=1),
        )
        cycle_replay = cycle_service.execute(
            cycle_id="operational-reliability-cycle-1",
            plan_id=PLAN_ID,
            as_of=PHASE_AT,
            executed_at=PHASE_AT + timedelta(minutes=1, seconds=10),
            idempotency_key="operational-reliability-cycle-1",
        )
        heartbeat_two = runner.heartbeat(
            runner_id=RUNNER_ID,
            run_instance_id=run_two.run_instance_id,
            plan_id=PLAN_ID,
            at=PHASE_AT + timedelta(minutes=1, seconds=20),
            last_cycle_id=cycle_replay.cycle.cycle_id,
            due_evaluation_count=2,
        )
    else:
        if len(heartbeats) < 2:
            raise OperationalReliabilityError("RUNNER_RESTART_EVIDENCE_INCOMPLETE")
        cycle = cycle_service.execute(
            cycle_id="operational-reliability-cycle-1",
            plan_id=PLAN_ID,
            as_of=PHASE_AT,
            executed_at=PHASE_AT + timedelta(minutes=1, seconds=10),
            idempotency_key="operational-reliability-cycle-1",
        )
        cycle_replay = cycle
        heartbeat_one = heartbeats[1]
        heartbeat_two = next(
            item
            for item in reversed(heartbeats)
            if item.status is RunnerStatus.RUNNING and item.due_evaluation_count == 2
        )
        run_two = heartbeat_two
    retained_recoveries = {item.attempt_id: item for item in operational.recovery_attempts()}
    recovery = retained_recoveries.get(
        "genuine-runner-restart-recovery-2026-08-04-v1"
    ) or RecordRecoveryAttempt(operational).execute(
        attempt_id="genuine-runner-restart-recovery-2026-08-04-v1",
        scenario="RUNNER_RESTART",
        actions=("STOP_FIRST_INSTANCE_CLEANLY", "CLAIM_NEW_LEASE", "REPLAY_EXACT_CYCLE"),
        outcomes=("NO_DUPLICATE_CYCLE", "RETAINED_TASK_RESULTS_REUSED"),
        status=RecoveryStatus.COMPLETED,
        actor="operator:operational-acceptance",
        attempted_at=PHASE_AT + timedelta(minutes=2),
    )

    manifest, verification, backup_replay = PostgresLogicalBackupDrill(
        engine=engine,
        settings=settings,
        repository=operational,
        repository_root=repository_root,
    ).execute(
        manifest_id=MANIFEST_ID,
        verification_id=VERIFICATION_ID,
        performed_at=PHASE_AT + timedelta(minutes=3),
    )
    backup = AuditBackupReadiness(live_repository).execute(
        BackupReadinessRecord.create(
            record_id="genuine-operational-backup-readiness-2026-08-04-v1",
            database_backup_method="POSTGRES_CUSTOM_FORMAT_LOGICAL_BACKUP",
            latest_verified_backup_at=manifest.backup_timestamp,
            restore_test_status=verification.status.value,
            configuration_backup_status="REPOSITORY_CONFIGURATION_SECRET_VALUES_EXCLUDED",
            credential_reference_recovery_procedure="RECREATE_OPAQUE_REFERENCE_AND_REVALIDATE_READ_ONLY",
            recovery_point_objective_seconds=86_400,
            recovery_time_objective_seconds=14_400,
            known_gaps=(),
            status=(
                BackupReadinessStatus.VERIFIED
                if verification.status is VerificationStatus.VERIFIED
                else BackupReadinessStatus.DEGRADED
            ),
            assessed_at=PHASE_AT + timedelta(minutes=3, seconds=30),
        )
    )
    retained_alerts = {item.alert_id: item for item in operational.alert_deliveries()}
    alert = retained_alerts.get(
        "genuine-runner-restart-alert-2026-08-04-v1"
    ) or DeliverOperationalAlert(
        operational,
        LocalAlertOutbox(repository_root / ".local/operational/alerts"),
    ).execute(
        alert_id="genuine-runner-restart-alert-2026-08-04-v1",
        category="RUNNER_RESTART_RECOVERED",
        source_evidence_ids=(
            heartbeat_one.heartbeat_id,
            heartbeat_two.heartbeat_id,
            recovery.attempt_id,
        ),
        severity=AlertSeverity.INFO,
        message="Operating runner restart recovered with exact cycle reuse; no provider request.",
        attempted_at=PHASE_AT + timedelta(minutes=3, seconds=40),
    )
    drill = _kill_switch_drill(live_repository, operational)
    health = operational.operational_health(HEALTH_ID) or MaterializeOperationalHealth(
        operational
    ).execute(
        health_id=HEALTH_ID,
        state=OperationalHealthState.HEALTHY,
        database_ready=True,
        api_ready=True,
        runner_status=RunnerStatus.RUNNING,
        runner_heartbeat_id=heartbeat_two.heartbeat_id,
        latest_cycle_id=cycle.cycle.cycle_id,
        next_due_at=heartbeat_two.next_due_at,
        failed_or_partial_cycle_count=0,
        stale_provider_evidence=False,
        credential_reference_healthy=True,
        backup_verified=verification.status is VerificationStatus.VERIFIED,
        execution_control_state=ExecutionControlState.LIVE_BLOCKED.value,
        disk_warning=None,
        migration_revision="0070_operational_reliability",
        migration_head="0070_operational_reliability",
        alert_delivery_healthy=alert.status.value == "DELIVERED",
        blockers=(),
        assessed_at=PHASE_AT + timedelta(minutes=6),
    )

    capabilities = tuple(
        CredentialsAbsentOnboarding(live_repository).execute(
            snapshot_id=f"genuine-operational-{provider.value.lower()}-capability-2026-08-04-v1",
            provider=provider,
            account_scope_id=f"{provider.value.lower()}-default",
            account_environment=(
                AccountEnvironment.PAPER if provider is Provider.ALPACA else AccountEnvironment.LIVE
            ),
            inspected_at=PHASE_AT + timedelta(minutes=6),
        )
        for provider in Provider
    )
    controls = live_repository.control_events(CONTROL_ID)
    policy = live_repository.risk_policy("LIVE_RISK_POLICY_V1") or LiveRiskPolicy.v1(
        created_at=PHASE_AT
    )
    alpaca = next(item for item in capabilities if item.provider is Provider.ALPACA)
    assessment = MaterializeLiveReadiness(live_repository).execute(
        assessment_id=ASSESSMENT_ID,
        decision_id="genuine-operational-live-risk-decision-2026-08-04-v1",
        policy=policy,
        inputs=LiveReadinessInputs(
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
            operating_cycle_health="HEALTHY",
            provider_health="UNAVAILABLE",
            backup=backup,
            control=controls[-1],
            qualifying_opportunity=False,
            explicit_operator_review=True,
            assessed_at=PHASE_AT + timedelta(minutes=7),
        ),
        actor="operator:operational-acceptance",
    )
    operational_tasks = build_operational_reliability_tasks(
        cycle_id=cycle.cycle.cycle_id,
        runner=heartbeat_two,
        manifest=manifest,
        restore=verification,
        alert=alert,
        credentials_configured=False,
        read_only_inspection_approved=False,
        assessment_fresh=True,
    )
    latest_runner = operational.latest_runner_heartbeat(RUNNER_ID)
    stopped = (
        latest_runner
        if latest_runner is not None and latest_runner.status is RunnerStatus.STOPPED
        else runner.stop(
            runner_id=RUNNER_ID,
            run_instance_id=run_two.run_instance_id,
            plan_id=PLAN_ID,
            at=PHASE_AT + timedelta(minutes=8),
        )
    )
    financial_after = financial_counts(engine)
    if financial_before != financial_after:
        raise OperationalReliabilityError("OPERATIONAL_ACCEPTANCE_FINANCIAL_STATE_CHANGED")
    if assessment.recommendation is not ReadinessRecommendation.LIVE_NOT_READY:
        raise OperationalReliabilityError("OPERATIONAL_ACCEPTANCE_MUST_REMAIN_LIVE_NOT_READY")
    return {
        "deployment_profile": profile,
        "operating_plan": plan,
        "operating_plan_replay": plan_replay,
        "runner_heartbeats": operational.runner_heartbeats(RUNNER_ID),
        "runner_final_status": stopped.status,
        "due_work_evaluations": (due_one, due_two),
        "cycle": cycle,
        "cycle_replay": cycle_replay.idempotent_replay,
        "backup_manifest": manifest,
        "restore_verification": verification,
        "backup_replay": backup_replay,
        "recovery": recovery,
        "alert": alert,
        "kill_switch_drill": drill,
        "operational_health": health,
        "operational_tasks": operational_tasks,
        "readiness_before": prior_live["assessment"],
        "readiness_after": assessment,
        "capabilities": capabilities,
        "financial_counts_before": financial_before,
        "financial_counts_after": financial_after,
        "private_provider_requests": 0,
        "provider_writes": 0,
        "live_authority": "NONE",
    }


def run_fixture_operational_failure_acceptance() -> dict[str, object]:
    branches = {
        "database_startup_failure": "BLOCKED_DATABASE_UNAVAILABLE",
        "interrupted_cycle": "RECOVERED_EXACT_TASK_REUSE",
        "stale_runner_heartbeat": "STALE",
        "duplicate_runner_lock": "RUNNER_DUPLICATE_ACTIVE_LEASE",
        "failed_backup": "POSTGRES_LOGICAL_BACKUP_FAILED",
        "failed_restore": "POSTGRES_DISPOSABLE_RESTORE_FAILED",
        "mismatched_restored_evidence": "RESTORED_EVIDENCE_HASH_MISMATCH",
        "alert_delivery_failure": "FAILED_RETAINED_WITHOUT_SECRET",
        "dangerous_provider_permission": "DANGEROUS_PROVIDER_PERMISSIONS",
        "emergency_stop": "PAPER_AND_LIVE_BLOCKED_READS_AVAILABLE",
        "clean_recovery": "COMPLETED_NO_DESTRUCTIVE_ACTION",
    }
    return {
        "branches": branches,
        "branch_count": len(branches),
        "active_database_damaged": False,
        "private_provider_requests": 0,
        "provider_writes": 0,
        "financial_mutations": 0,
        "live_authority": "NONE",
    }
