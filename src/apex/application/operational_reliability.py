"""Application services for bounded local operational reliability."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from apex.application.live_readiness import (
    CredentialsAbsentCapabilityInspector,
    InspectProviderCapability,
    LiveReadinessRepository,
)
from apex.domain.live_readiness import (
    AccountEnvironment,
    ExecutionControlState,
    Provider,
    ProviderCapabilitySnapshot,
)
from apex.domain.operational_reliability import (
    AlertDelivery,
    AlertDeliveryStatus,
    AlertSeverity,
    BackupManifest,
    DeploymentProfile,
    KillSwitchDrill,
    OperationalHealth,
    OperationalHealthState,
    OperationalReliabilityError,
    RecoveryAttempt,
    RecoveryStatus,
    RestoreVerification,
    RunnerHeartbeat,
    RunnerStatus,
)


class OperationalReliabilityRepository(Protocol):
    def save_deployment_profile(self, value: DeploymentProfile) -> DeploymentProfile: ...
    def deployment_profile(self, profile_id: str) -> DeploymentProfile | None: ...
    def deployment_profiles(self) -> tuple[DeploymentProfile, ...]: ...
    def claim_runner_heartbeat(self, value: RunnerHeartbeat) -> RunnerHeartbeat: ...
    def runner_heartbeats(self, runner_id: str) -> tuple[RunnerHeartbeat, ...]: ...
    def latest_runner_heartbeat(self, runner_id: str) -> RunnerHeartbeat | None: ...
    def save_backup_manifest(self, value: BackupManifest) -> BackupManifest: ...
    def backup_manifest(self, manifest_id: str) -> BackupManifest | None: ...
    def backup_manifests(self) -> tuple[BackupManifest, ...]: ...
    def save_restore_verification(self, value: RestoreVerification) -> RestoreVerification: ...
    def restore_verification(self, verification_id: str) -> RestoreVerification | None: ...
    def restore_verifications(self) -> tuple[RestoreVerification, ...]: ...
    def save_recovery_attempt(self, value: RecoveryAttempt) -> RecoveryAttempt: ...
    def recovery_attempts(self) -> tuple[RecoveryAttempt, ...]: ...
    def save_alert_delivery(self, value: AlertDelivery) -> AlertDelivery: ...
    def alert_deliveries(self) -> tuple[AlertDelivery, ...]: ...
    def save_operational_health(self, value: OperationalHealth) -> OperationalHealth: ...
    def operational_health(self, health_id: str) -> OperationalHealth | None: ...
    def health_projections(self) -> tuple[OperationalHealth, ...]: ...
    def save_kill_switch_drill(self, value: KillSwitchDrill) -> KillSwitchDrill: ...
    def kill_switch_drills(self) -> tuple[KillSwitchDrill, ...]: ...


class RegisterDeploymentProfile:
    def __init__(self, repository: OperationalReliabilityRepository) -> None:
        self._repository = repository

    def execute(self, *, created_at: datetime, actor: str) -> DeploymentProfile:
        return self._repository.save_deployment_profile(
            DeploymentProfile.local_v1(created_at=created_at, actor=actor)
        )


class SupervisedOperatingRunner:
    """One retained lease and heartbeat stream around the existing exact planner."""

    LEASE_SECONDS = 180
    EVALUATION_CADENCE_SECONDS = 300

    def __init__(self, repository: OperationalReliabilityRepository) -> None:
        self._repository = repository

    def _event(
        self,
        *,
        runner_id: str,
        run_instance_id: str,
        plan_id: str,
        status: RunnerStatus,
        at: datetime,
        last_cycle_id: str | None,
        due_evaluation_count: int,
    ) -> RunnerHeartbeat:
        retained = self._repository.runner_heartbeats(runner_id)
        return RunnerHeartbeat.create(
            heartbeat_id=f"{run_instance_id}-heartbeat-{len(retained) + 1}",
            runner_id=runner_id,
            run_instance_id=run_instance_id,
            plan_id=plan_id,
            ordinal=len(retained) + 1,
            status=status,
            heartbeat_at=at,
            lease_expires_at=at + timedelta(seconds=self.LEASE_SECONDS),
            last_cycle_id=last_cycle_id,
            next_due_at=at + timedelta(seconds=self.EVALUATION_CADENCE_SECONDS),
            due_evaluation_count=due_evaluation_count,
        )

    def start(
        self, *, runner_id: str, run_instance_id: str, plan_id: str, at: datetime
    ) -> RunnerHeartbeat:
        return self._repository.claim_runner_heartbeat(
            self._event(
                runner_id=runner_id,
                run_instance_id=run_instance_id,
                plan_id=plan_id,
                status=RunnerStatus.RUNNING,
                at=at,
                last_cycle_id=None,
                due_evaluation_count=0,
            )
        )

    def heartbeat(
        self,
        *,
        runner_id: str,
        run_instance_id: str,
        plan_id: str,
        at: datetime,
        last_cycle_id: str | None,
        due_evaluation_count: int,
        paused: bool = False,
    ) -> RunnerHeartbeat:
        latest = self._repository.latest_runner_heartbeat(runner_id)
        if latest is None or latest.run_instance_id != run_instance_id:
            raise OperationalReliabilityError("RUNNER_INSTANCE_NOT_ACTIVE")
        return self._repository.claim_runner_heartbeat(
            self._event(
                runner_id=runner_id,
                run_instance_id=run_instance_id,
                plan_id=plan_id,
                status=RunnerStatus.PAUSED if paused else RunnerStatus.RUNNING,
                at=at,
                last_cycle_id=last_cycle_id,
                due_evaluation_count=due_evaluation_count,
            )
        )

    def stop(
        self,
        *,
        runner_id: str,
        run_instance_id: str,
        plan_id: str,
        at: datetime,
    ) -> RunnerHeartbeat:
        latest = self._repository.latest_runner_heartbeat(runner_id)
        if latest is None or latest.run_instance_id != run_instance_id:
            raise OperationalReliabilityError("RUNNER_INSTANCE_NOT_ACTIVE")
        return self._repository.claim_runner_heartbeat(
            self._event(
                runner_id=runner_id,
                run_instance_id=run_instance_id,
                plan_id=plan_id,
                status=RunnerStatus.STOPPED,
                at=at,
                last_cycle_id=latest.last_cycle_id,
                due_evaluation_count=latest.due_evaluation_count,
            )
        )


class RecordRecoveryAttempt:
    def __init__(self, repository: OperationalReliabilityRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        attempt_id: str,
        scenario: str,
        actions: tuple[str, ...],
        outcomes: tuple[str, ...],
        status: RecoveryStatus,
        actor: str,
        attempted_at: datetime,
    ) -> RecoveryAttempt:
        return self._repository.save_recovery_attempt(
            RecoveryAttempt.create(
                attempt_id=attempt_id,
                scenario=scenario,
                actions=actions,
                outcomes=outcomes,
                status=status,
                actor=actor,
                attempted_at=attempted_at,
            )
        )


class LocalAlertOutbox:
    """A bounded ignored-file sink; retained PostgreSQL evidence stays authoritative."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def deliver(self, alert: AlertDelivery) -> bool:
        self._root.mkdir(parents=True, exist_ok=True)
        target = (self._root / f"{alert.alert_id}.json").resolve()
        if target.parent != self._root:
            raise OperationalReliabilityError("ALERT_OUTBOX_PATH_INVALID")
        target.write_text(json.dumps(asdict(alert), default=str, sort_keys=True) + "\n")
        return True


class DeliverOperationalAlert:
    def __init__(
        self,
        repository: OperationalReliabilityRepository,
        outbox: LocalAlertOutbox | None,
    ) -> None:
        self._repository = repository
        self._outbox = outbox

    def execute(
        self,
        *,
        alert_id: str,
        category: str,
        source_evidence_ids: tuple[str, ...],
        severity: AlertSeverity,
        message: str,
        attempted_at: datetime,
    ) -> AlertDelivery:
        retained = AlertDelivery.create(
            alert_id=alert_id,
            category=category,
            source_evidence_ids=source_evidence_ids,
            severity=severity,
            sink="LOCAL_FILE_OUTBOX" if self._outbox is not None else "RETAINED_ONLY",
            delivery_attempt=1,
            status=AlertDeliveryStatus.RETAINED_ONLY,
            message=message,
            attempted_at=attempted_at,
            delivered_at=None,
        )
        if self._outbox is None:
            return self._repository.save_alert_delivery(retained)
        try:
            delivered = AlertDelivery.create(
                alert_id=alert_id,
                category=category,
                source_evidence_ids=source_evidence_ids,
                severity=severity,
                sink="LOCAL_FILE_OUTBOX",
                delivery_attempt=1,
                status=AlertDeliveryStatus.DELIVERED,
                message=message,
                attempted_at=attempted_at,
                delivered_at=attempted_at,
            )
            self._outbox.deliver(delivered)
            return self._repository.save_alert_delivery(delivered)
        except OSError:
            failed = AlertDelivery.create(
                alert_id=alert_id,
                category=category,
                source_evidence_ids=source_evidence_ids,
                severity=severity,
                sink="LOCAL_FILE_OUTBOX",
                delivery_attempt=1,
                status=AlertDeliveryStatus.FAILED,
                message=message,
                attempted_at=attempted_at,
                delivered_at=None,
            )
            return self._repository.save_alert_delivery(failed)


class MaterializeOperationalHealth:
    def __init__(self, repository: OperationalReliabilityRepository) -> None:
        self._repository = repository

    def execute(self, **values: Any) -> OperationalHealth:
        blockers = set(values.pop("blockers", ()))
        if not values["database_ready"]:
            blockers.add("DATABASE_UNAVAILABLE")
        if not values["api_ready"]:
            blockers.add("API_UNAVAILABLE")
        if values["runner_status"] is RunnerStatus.STOPPED:
            blockers.add("OPERATING_RUNNER_STOPPED")
        if values["migration_revision"] != values["migration_head"]:
            blockers.add("MIGRATION_HEAD_MISMATCH")
        if not values["backup_verified"]:
            blockers.add("BACKUP_NOT_VERIFIED")
        if values["execution_control_state"] == ExecutionControlState.EMERGENCY_STOP.value:
            blockers.add("EMERGENCY_STOP_ACTIVE")
        if not values["alert_delivery_healthy"]:
            blockers.add("ALERT_DELIVERY_UNHEALTHY")
        state = values.pop("state", None)
        if state is None:
            state = (
                OperationalHealthState.HEALTHY if not blockers else OperationalHealthState.DEGRADED
            )
        value = OperationalHealth.create(state=state, blockers=tuple(blockers), **values)
        return self._repository.save_operational_health(value)


class CredentialsAbsentOnboarding:
    """Default onboarding path: inspect references and make no private request."""

    def __init__(self, repository: LiveReadinessRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        snapshot_id: str,
        provider: Provider,
        account_scope_id: str,
        account_environment: AccountEnvironment,
        inspected_at: datetime,
    ) -> ProviderCapabilitySnapshot:
        return InspectProviderCapability(
            self._repository,
            CredentialsAbsentCapabilityInspector(provider),
        ).execute(
            snapshot_id=snapshot_id,
            account_scope_id=account_scope_id,
            account_environment=account_environment,
            credential_reference_id=None,
            inspected_at=inspected_at,
            request_limit=0,
            actor="operator:credentials-absent-onboarding",
            reason="Default operational acceptance retains absence without provider access.",
            confirmation=f"INSPECT_PROVIDER_CAPABILITY_READ_ONLY:{snapshot_id}",
            idempotency_key=snapshot_id,
        )


def validate_private_inspection_approval(
    *,
    provider: Provider,
    credential_reference_id: str,
    endpoint_classes: tuple[str, ...],
    maximum_requests: int,
    timeout_seconds: int,
    confirmation: str,
) -> None:
    allowed = {
        "ACCOUNT_STATUS_READ",
        "ACCOUNT_TYPES_READ",
        "PERMISSIONS_READ",
        "BALANCES_READ",
        "FEES_READ",
        "PRODUCT_RESTRICTIONS_READ",
        "MINIMUM_SIZES_READ",
        "PREVIEW_AVAILABILITY_METADATA_READ",
    }
    expected = f"APPROVE_READ_ONLY_INSPECTION:{provider.value}:{credential_reference_id}"
    if (
        confirmation != expected
        or not endpoint_classes
        or any(item not in allowed for item in endpoint_classes)
        or not 1 <= maximum_requests <= 10
        or not 1 <= timeout_seconds <= 30
    ):
        raise OperationalReliabilityError("PRIVATE_INSPECTION_APPROVAL_INVALID")


def operational_workspace(repository: OperationalReliabilityRepository) -> dict[str, object]:
    profiles = repository.deployment_profiles()
    manifests = repository.backup_manifests()
    verifications = repository.restore_verifications()
    recoveries = repository.recovery_attempts()
    alerts = repository.alert_deliveries()
    health = repository.health_projections()
    drills = repository.kill_switch_drills()
    runner = repository.latest_runner_heartbeat("apex-operating-runner-v1")
    return {
        "deployment_profile": None if not profiles else profiles[-1],
        "runner": runner,
        "backup_manifest": None if not manifests else manifests[-1],
        "restore_verification": None if not verifications else verifications[-1],
        "recovery_attempts": recoveries,
        "alert_deliveries": alerts,
        "operational_health": None if not health else health[-1],
        "kill_switch_drill": None if not drills else drills[-1],
        "safe_controls": (
            "START_STOP_LOCAL_OPERATING_RUNNER",
            "PAUSE_RESUME_OPERATING_CYCLES",
            "TRIGGER_EXACT_BACKUP_VERIFICATION",
            "INSPECT_RECOVERY_STATUS",
            "RUN_CREDENTIALS_ABSENT_INSPECTION",
            "EMERGENCY_STOP",
            "CLEAR_EMERGENCY_STOP_TO_LIVE_BLOCKED",
            "MATERIALIZE_READINESS_ASSESSMENT",
        ),
        "provider_request_count": 0,
        "provider_write_count": 0,
        "authority": "NONE",
        "live_authority": "NONE",
    }
