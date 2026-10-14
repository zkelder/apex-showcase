from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apex.adapters.operational_reliability_acceptance import (
    run_fixture_operational_failure_acceptance,
)
from apex.application.operational_reliability import (
    DeliverOperationalAlert,
    LocalAlertOutbox,
    MaterializeOperationalHealth,
    RegisterDeploymentProfile,
    SupervisedOperatingRunner,
    validate_private_inspection_approval,
)
from apex.domain.live_readiness import Provider
from apex.domain.operational_reliability import (
    AlertDelivery,
    AlertSeverity,
    BackupManifest,
    DeploymentProfile,
    KillSwitchDrill,
    OperationalHealth,
    OperationalHealthState,
    OperationalReliabilityError,
    RecoveryAttempt,
    RestoreVerification,
    RunnerHeartbeat,
    RunnerStatus,
    build_operational_reliability_tasks,
    submission_allowed,
)

NOW = datetime(2026, 8, 4, 14, tzinfo=UTC)


class MemoryOperationalRepository:
    def __init__(self) -> None:
        self.profiles: dict[str, DeploymentProfile] = {}
        self.heartbeats: list[RunnerHeartbeat] = []
        self.manifests: dict[str, BackupManifest] = {}
        self.verifications: dict[str, RestoreVerification] = {}
        self.recoveries: list[RecoveryAttempt] = []
        self.alerts: list[AlertDelivery] = []
        self.health: dict[str, OperationalHealth] = {}
        self.drills: list[KillSwitchDrill] = []

    def save_deployment_profile(self, value: DeploymentProfile) -> DeploymentProfile:
        self.profiles.setdefault(value.profile_id, value)
        return self.profiles[value.profile_id]

    def deployment_profile(self, profile_id: str) -> DeploymentProfile | None:
        return self.profiles.get(profile_id)

    def deployment_profiles(self) -> tuple[DeploymentProfile, ...]:
        return tuple(self.profiles.values())

    def claim_runner_heartbeat(self, value: RunnerHeartbeat) -> RunnerHeartbeat:
        latest = self.latest_runner_heartbeat(value.runner_id)
        if latest is not None:
            if value.ordinal != latest.ordinal + 1:
                raise OperationalReliabilityError("RUNNER_HEARTBEAT_ORDINAL_CONFLICT")
            if (
                value.run_instance_id != latest.run_instance_id
                and latest.status in {RunnerStatus.RUNNING, RunnerStatus.PAUSED}
                and latest.lease_expires_at > value.heartbeat_at
            ):
                raise OperationalReliabilityError("RUNNER_DUPLICATE_ACTIVE_LEASE")
        self.heartbeats.append(value)
        return value

    def runner_heartbeats(self, runner_id: str) -> tuple[RunnerHeartbeat, ...]:
        return tuple(item for item in self.heartbeats if item.runner_id == runner_id)

    def latest_runner_heartbeat(self, runner_id: str) -> RunnerHeartbeat | None:
        values = self.runner_heartbeats(runner_id)
        return None if not values else values[-1]

    def save_backup_manifest(self, value: BackupManifest) -> BackupManifest:
        self.manifests.setdefault(value.manifest_id, value)
        return self.manifests[value.manifest_id]

    def backup_manifest(self, manifest_id: str) -> BackupManifest | None:
        return self.manifests.get(manifest_id)

    def backup_manifests(self) -> tuple[BackupManifest, ...]:
        return tuple(self.manifests.values())

    def save_restore_verification(self, value: RestoreVerification) -> RestoreVerification:
        self.verifications.setdefault(value.verification_id, value)
        return self.verifications[value.verification_id]

    def restore_verification(self, verification_id: str) -> RestoreVerification | None:
        return self.verifications.get(verification_id)

    def restore_verifications(self) -> tuple[RestoreVerification, ...]:
        return tuple(self.verifications.values())

    def save_recovery_attempt(self, value: RecoveryAttempt) -> RecoveryAttempt:
        self.recoveries.append(value)
        return value

    def recovery_attempts(self) -> tuple[RecoveryAttempt, ...]:
        return tuple(self.recoveries)

    def save_alert_delivery(self, value: AlertDelivery) -> AlertDelivery:
        self.alerts.append(value)
        return value

    def alert_deliveries(self) -> tuple[AlertDelivery, ...]:
        return tuple(self.alerts)

    def save_operational_health(self, value: OperationalHealth) -> OperationalHealth:
        self.health.setdefault(value.health_id, value)
        return self.health[value.health_id]

    def operational_health(self, health_id: str) -> OperationalHealth | None:
        return self.health.get(health_id)

    def health_projections(self) -> tuple[OperationalHealth, ...]:
        return tuple(self.health.values())

    def save_kill_switch_drill(self, value: KillSwitchDrill) -> KillSwitchDrill:
        self.drills.append(value)
        return value

    def kill_switch_drills(self) -> tuple[KillSwitchDrill, ...]:
        return tuple(self.drills)


def test_deployment_profile_is_bounded_and_authority_free() -> None:
    repository = MemoryOperationalRepository()
    profile = RegisterDeploymentProfile(repository).execute(created_at=NOW, actor="operator:test")
    assert profile.profile_id == "APEX_LOCAL_OPERATIONAL_PROFILE_V1"
    assert tuple(item.service_id for item in profile.services) == (
        "POSTGRESQL",
        "APEX_API",
        "OPERATING_CYCLE_RUNNER",
        "BOUNDED_RESEARCH_WORKER",
    )
    assert tuple(item.startup_ordinal for item in profile.services) == (1, 2, 3, 4)
    assert profile.authority == profile.live_authority == "NONE"
    assert profile.provider_write_capability == "NONE"


def test_runner_prevents_duplicates_and_recovers_after_stop_or_stale_lease() -> None:
    repository = MemoryOperationalRepository()
    runner = SupervisedOperatingRunner(repository)
    first = runner.start(runner_id="runner", run_instance_id="instance-1", plan_id="plan", at=NOW)
    with pytest.raises(OperationalReliabilityError, match="DUPLICATE_ACTIVE_LEASE"):
        runner.start(
            runner_id="runner",
            run_instance_id="instance-2",
            plan_id="plan",
            at=NOW + timedelta(seconds=1),
        )
    runner.stop(
        runner_id="runner",
        run_instance_id=first.run_instance_id,
        plan_id="plan",
        at=NOW + timedelta(seconds=2),
    )
    restarted = runner.start(
        runner_id="runner",
        run_instance_id="instance-2",
        plan_id="plan",
        at=NOW + timedelta(seconds=3),
    )
    assert restarted.status is RunnerStatus.RUNNING
    assert restarted.ordinal == 3
    assert restarted.provider_request_count == restarted.provider_write_count == 0


def test_alert_outbox_redacts_secrets_and_retains_delivery(tmp_path: Path) -> None:
    repository = MemoryOperationalRepository()
    alert = DeliverOperationalAlert(repository, LocalAlertOutbox(tmp_path)).execute(
        alert_id="redacted-alert",
        category="DATABASE_UNAVAILABLE",
        source_evidence_ids=("health-1",),
        severity=AlertSeverity.CRITICAL,
        message="Authorization: Bearer fixture-sensitive-value",  # pragma: allowlist secret
        attempted_at=NOW,
    )
    rendered = (tmp_path / "redacted-alert.json").read_text()
    assert alert.status.value == "DELIVERED"
    assert "fixture-sensitive-value" not in alert.message
    assert "fixture-sensitive-value" not in rendered
    assert "[REDACTED]" in rendered
    assert not alert.contains_secret_values


def test_operational_health_fails_closed_and_preserves_live_boundary() -> None:
    repository = MemoryOperationalRepository()
    health = MaterializeOperationalHealth(repository).execute(
        health_id="health-failure",
        database_ready=False,
        api_ready=True,
        runner_status=RunnerStatus.STOPPED,
        runner_heartbeat_id=None,
        latest_cycle_id=None,
        next_due_at=None,
        failed_or_partial_cycle_count=1,
        stale_provider_evidence=True,
        credential_reference_healthy=True,
        backup_verified=False,
        execution_control_state="LIVE_BLOCKED",
        disk_warning="LOW_FREE_SPACE",
        migration_revision="0069_secure_live_readiness",
        migration_head="0077_historical_market_data",
        alert_delivery_healthy=False,
        blockers=("STALE_CRITICAL_MARKET_EVIDENCE",),
        assessed_at=NOW,
    )
    assert health.state is OperationalHealthState.DEGRADED
    assert {
        "DATABASE_UNAVAILABLE",
        "OPERATING_RUNNER_STOPPED",
        "BACKUP_NOT_VERIFIED",
        "MIGRATION_HEAD_MISMATCH",
        "ALERT_DELIVERY_UNHEALTHY",
    }.issubset(health.blockers)
    assert health.live_authority == "NONE"


def test_read_only_approval_is_exact_and_rejects_write_classes() -> None:
    validate_private_inspection_approval(
        provider=Provider.COINBASE,
        credential_reference_id="coinbase-ref",
        endpoint_classes=("ACCOUNT_STATUS_READ", "FEES_READ"),
        maximum_requests=2,
        timeout_seconds=10,
        confirmation="APPROVE_READ_ONLY_INSPECTION:COINBASE:coinbase-ref",
    )
    with pytest.raises(OperationalReliabilityError, match="APPROVAL_INVALID"):
        validate_private_inspection_approval(
            provider=Provider.COINBASE,
            credential_reference_id="coinbase-ref",
            endpoint_classes=("ORDER_CREATE",),
            maximum_requests=1,
            timeout_seconds=10,
            confirmation="APPROVE_READ_ONLY_INSPECTION:COINBASE:coinbase-ref",
        )


def test_emergency_stop_blocks_submissions_without_granting_live() -> None:
    assert not submission_allowed("EMERGENCY_STOP", paper=True)
    assert not submission_allowed("EMERGENCY_STOP", paper=False)
    assert not submission_allowed("LIVE_BLOCKED", paper=False)
    assert submission_allowed("PAPER_ONLY", paper=True)


def test_failure_acceptance_covers_all_required_branches() -> None:
    acceptance = run_fixture_operational_failure_acceptance()
    assert acceptance["branch_count"] == 11
    assert set(acceptance["branches"]) == {
        "database_startup_failure",
        "interrupted_cycle",
        "stale_runner_heartbeat",
        "duplicate_runner_lock",
        "failed_backup",
        "failed_restore",
        "mismatched_restored_evidence",
        "alert_delivery_failure",
        "dangerous_provider_permission",
        "emergency_stop",
        "clean_recovery",
    }
    assert acceptance["private_provider_requests"] == 0
    assert acceptance["provider_writes"] == 0
    assert acceptance["financial_mutations"] == 0
    assert acceptance["live_authority"] == "NONE"


def test_operating_cycle_tasks_are_bounded_and_credentials_absent_is_not_due() -> None:
    tasks = build_operational_reliability_tasks(
        cycle_id="cycle-1",
        runner=None,
        manifest=None,
        restore=None,
        alert=None,
        credentials_configured=False,
        read_only_inspection_approved=False,
        assessment_fresh=False,
    )
    assert tuple(item.task_type for item in tasks) == (
        "RUNNER_HEALTH",
        "BACKUP_DUE_STATUS",
        "LATEST_RESTORE_VERIFICATION",
        "ALERT_DELIVERY_HEALTH",
        "CAPABILITY_INSPECTION_DUE_STATUS",
        "READINESS_REASSESSMENT",
    )
    capability = tasks[4]
    assert capability.disposition.value == "NOT_DUE"
    assert capability.provider_request_limit == 0
    assert all(item.provider_write_capability == "NONE" for item in tasks)
