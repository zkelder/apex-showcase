"""Pure evidence rules for bounded local operational reliability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from apex.domain.live_readiness import redact_payload
from apex.domain.values import content_digest

OPERATIONAL_DEPLOYMENT_PROFILE_V1 = "APEX_LOCAL_OPERATIONAL_PROFILE_V1"
OPERATING_RUNNER_POLICY_V1 = "APEX_SUPERVISED_OPERATING_RUNNER_V1"
BACKUP_MANIFEST_POLICY_V1 = "APEX_POSTGRES_LOGICAL_BACKUP_MANIFEST_V1"
RESTORE_VERIFICATION_POLICY_V1 = "APEX_DISPOSABLE_RESTORE_VERIFICATION_V1"
RECOVERY_POLICY_V1 = "APEX_BOUNDED_RECOVERY_V1"
ALERT_DELIVERY_POLICY_V1 = "APEX_LOCAL_ALERT_DELIVERY_V1"
OPERATIONAL_HEALTH_POLICY_V1 = "APEX_OPERATIONAL_HEALTH_V1"
KILL_SWITCH_DRILL_POLICY_V1 = "APEX_KILL_SWITCH_DRILL_V1"
OPERATIONAL_WARNING = "APEX OPERATIONS — NO PROVIDER WRITE — LIVE AUTHORITY NONE"


class OperationalReliabilityError(ValueError):
    """Operational evidence is invalid or would cross an authority boundary."""


class RunnerStatus(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class OperationalHealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    NOT_RUN = "NOT_RUN"


class RecoveryStatus(StrEnum):
    COMPLETED = "COMPLETED"
    MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
    FAILED = "FAILED"


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertDeliveryStatus(StrEnum):
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    RETAINED_ONLY = "RETAINED_ONLY"


class OperationalTaskDisposition(StrEnum):
    DUE = "DUE"
    NOT_DUE = "NOT_DUE"
    BLOCKED = "BLOCKED"


def _id(value: str, reason: str, maximum: int = 300) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise OperationalReliabilityError(reason)
    return normalized


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OperationalReliabilityError(reason)
    return value.astimezone(UTC)


def _hash(value: str, reason: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(item not in "0123456789abcdef" for item in normalized):
        raise OperationalReliabilityError(reason)
    return normalized


def _pairs(values: tuple[tuple[str, int], ...], reason: str) -> tuple[tuple[str, int], ...]:
    normalized = tuple(sorted((_id(key, reason), int(value)) for key, value in values))
    if any(value < 0 for _, value in normalized) or len({key for key, _ in normalized}) != len(
        normalized
    ):
        raise OperationalReliabilityError(reason)
    return normalized


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    service_id: str
    startup_ordinal: int
    shutdown_ordinal: int
    required: bool
    launch_mode: str
    health_check: str
    restart_policy: str
    cpu_limit: str
    memory_limit_mb: int
    storage_reference: str
    configuration_references: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        service_id: str,
        startup_ordinal: int,
        shutdown_ordinal: int,
        required: bool,
        launch_mode: str,
        health_check: str,
        restart_policy: str,
        cpu_limit: str,
        memory_limit_mb: int,
        storage_reference: str,
        configuration_references: tuple[str, ...],
    ) -> ServiceDefinition:
        if startup_ordinal <= 0 or shutdown_ordinal <= 0 or memory_limit_mb <= 0:
            raise OperationalReliabilityError("DEPLOYMENT_SERVICE_BOUND_INVALID")
        return cls(
            _id(service_id, "DEPLOYMENT_SERVICE_ID_INVALID"),
            startup_ordinal,
            shutdown_ordinal,
            required,
            _id(launch_mode, "DEPLOYMENT_SERVICE_LAUNCH_MODE_INVALID"),
            _id(health_check, "DEPLOYMENT_SERVICE_HEALTH_CHECK_INVALID"),
            _id(restart_policy, "DEPLOYMENT_SERVICE_RESTART_POLICY_INVALID"),
            _id(cpu_limit, "DEPLOYMENT_SERVICE_CPU_LIMIT_INVALID"),
            memory_limit_mb,
            _id(storage_reference, "DEPLOYMENT_SERVICE_STORAGE_INVALID", 500),
            tuple(
                sorted(
                    _id(item, "DEPLOYMENT_SERVICE_CONFIGURATION_INVALID", 500)
                    for item in configuration_references
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class DeploymentProfile:
    profile_id: str
    version: int
    policy_id: str
    services: tuple[ServiceDefinition, ...]
    created_at: datetime
    actor: str
    canonical_hash: str
    authority: str = "NONE"
    provider_write_capability: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def local_v1(cls, *, created_at: datetime, actor: str) -> DeploymentProfile:
        services = (
            ServiceDefinition.create(
                service_id="POSTGRESQL",
                startup_ordinal=1,
                shutdown_ordinal=4,
                required=True,
                launch_mode="DOCKER_COMPOSE_LOCAL",
                health_check="PG_ISREADY",
                restart_policy="UNLESS_STOPPED_MAX_5",
                cpu_limit="1.00",
                memory_limit_mb=1024,
                storage_reference="NAMED_VOLUME:apex-postgres-data",
                configuration_references=("APEX_POSTGRES_*",),
            ),
            ServiceDefinition.create(
                service_id="APEX_API",
                startup_ordinal=2,
                shutdown_ordinal=3,
                required=True,
                launch_mode="SUPERVISED_LOCAL_PROCESS",
                health_check="GET:/health/ready",
                restart_policy="ON_FAILURE_MAX_3",
                cpu_limit="1.00",
                memory_limit_mb=768,
                storage_reference="READ_ONLY_REPOSITORY_PLUS_POSTGRES",
                configuration_references=("APEX_POSTGRES_*", "APEX_OPERATOR_ALLOWED_ORIGINS"),
            ),
            ServiceDefinition.create(
                service_id="OPERATING_CYCLE_RUNNER",
                startup_ordinal=3,
                shutdown_ordinal=2,
                required=True,
                launch_mode="SUPERVISED_LOCAL_PROCESS",
                health_check="RETAINED_HEARTBEAT_MAX_180_SECONDS",
                restart_policy="ON_FAILURE_MAX_3",
                cpu_limit="0.50",
                memory_limit_mb=512,
                storage_reference="POSTGRES_APPEND_ONLY_EVIDENCE",
                configuration_references=("APEX_POSTGRES_*", "APEX_OPERATING_PLAN_ID"),
            ),
            ServiceDefinition.create(
                service_id="BOUNDED_RESEARCH_WORKER",
                startup_ordinal=4,
                shutdown_ordinal=1,
                required=False,
                launch_mode="EXACT_ONE_PASS_ONLY",
                health_check="NOT_REQUIRED_WHEN_IDLE",
                restart_policy="NEVER_AUTOMATICALLY_RETRY_REAL_FAILURE",
                cpu_limit="1.00",
                memory_limit_mb=1024,
                storage_reference="LOCAL_IGNORED_ARTIFACTS",
                configuration_references=(".local/optuna", ".local/mlflow"),
            ),
        )
        values = (
            OPERATIONAL_DEPLOYMENT_PROFILE_V1,
            1,
            OPERATIONAL_DEPLOYMENT_PROFILE_V1,
            services,
            _time(created_at, "DEPLOYMENT_PROFILE_TIME_INVALID"),
            _id(actor, "DEPLOYMENT_PROFILE_ACTOR_INVALID"),
            "NONE",
            "NONE",
            "NONE",
        )
        return cls(*values[:-3], content_digest(values), *values[-3:])


@dataclass(frozen=True, slots=True)
class RunnerHeartbeat:
    heartbeat_id: str
    runner_id: str
    run_instance_id: str
    plan_id: str
    ordinal: int
    status: RunnerStatus
    heartbeat_at: datetime
    lease_expires_at: datetime
    last_cycle_id: str | None
    next_due_at: datetime
    due_evaluation_count: int
    duplicate_prevention_key: str
    canonical_hash: str
    authority: str = "NONE"
    provider_request_count: int = 0
    provider_write_count: int = 0
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        heartbeat_id: str,
        runner_id: str,
        run_instance_id: str,
        plan_id: str,
        ordinal: int,
        status: RunnerStatus,
        heartbeat_at: datetime,
        lease_expires_at: datetime,
        last_cycle_id: str | None,
        next_due_at: datetime,
        due_evaluation_count: int,
    ) -> RunnerHeartbeat:
        heartbeat = _time(heartbeat_at, "RUNNER_HEARTBEAT_TIME_INVALID")
        lease = _time(lease_expires_at, "RUNNER_LEASE_TIME_INVALID")
        due = _time(next_due_at, "RUNNER_NEXT_DUE_TIME_INVALID")
        if ordinal <= 0 or due_evaluation_count < 0 or lease <= heartbeat:
            raise OperationalReliabilityError("RUNNER_HEARTBEAT_BOUND_INVALID")
        runner = _id(runner_id, "RUNNER_ID_INVALID")
        values = (
            _id(heartbeat_id, "RUNNER_HEARTBEAT_ID_INVALID"),
            runner,
            _id(run_instance_id, "RUNNER_INSTANCE_ID_INVALID"),
            _id(plan_id, "RUNNER_PLAN_ID_INVALID"),
            ordinal,
            status,
            heartbeat,
            lease,
            None if last_cycle_id is None else _id(last_cycle_id, "RUNNER_CYCLE_ID_INVALID"),
            due,
            due_evaluation_count,
            f"{OPERATING_RUNNER_POLICY_V1}:{runner}",
            "NONE",
            0,
            0,
            "NONE",
        )
        return cls(*values[:-4], content_digest(values), *values[-4:])


@dataclass(frozen=True, slots=True)
class BackupManifest:
    manifest_id: str
    policy_id: str
    schema_revision: str
    source_database_identity: str
    backup_timestamp: datetime
    storage_reference: str
    file_size_bytes: int
    sha256: str
    table_counts: tuple[tuple[str, int], ...]
    evidence_exclusions: tuple[str, ...]
    secret_handling: str
    selected_evidence_hash: str
    canonical_hash: str
    authority: str = "NONE"
    includes_decrypted_secrets: bool = False

    @classmethod
    def create(
        cls,
        *,
        manifest_id: str,
        schema_revision: str,
        source_database_identity: str,
        backup_timestamp: datetime,
        storage_reference: str,
        file_size_bytes: int,
        sha256: str,
        table_counts: tuple[tuple[str, int], ...],
        evidence_exclusions: tuple[str, ...],
        secret_handling: str,
        selected_evidence_hash: str,
    ) -> BackupManifest:
        if file_size_bytes <= 0:
            raise OperationalReliabilityError("BACKUP_FILE_SIZE_INVALID")
        values = (
            _id(manifest_id, "BACKUP_MANIFEST_ID_INVALID"),
            BACKUP_MANIFEST_POLICY_V1,
            _id(schema_revision, "BACKUP_SCHEMA_REVISION_INVALID"),
            _id(source_database_identity, "BACKUP_DATABASE_IDENTITY_INVALID"),
            _time(backup_timestamp, "BACKUP_TIMESTAMP_INVALID"),
            _id(storage_reference, "BACKUP_STORAGE_REFERENCE_INVALID", 500),
            file_size_bytes,
            _hash(sha256, "BACKUP_SHA256_INVALID"),
            _pairs(table_counts, "BACKUP_TABLE_COUNTS_INVALID"),
            tuple(
                sorted(_id(item, "BACKUP_EXCLUSION_INVALID", 500) for item in evidence_exclusions)
            ),
            _id(secret_handling, "BACKUP_SECRET_HANDLING_INVALID", 500),
            _hash(selected_evidence_hash, "BACKUP_EVIDENCE_HASH_INVALID"),
            "NONE",
            False,
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class RestoreVerification:
    verification_id: str
    policy_id: str
    manifest_id: str
    disposable_database_identity: str
    expected_schema_revision: str
    restored_schema_revision: str | None
    protected_count_comparisons: tuple[tuple[str, int], ...]
    restored_evidence_hash: str | None
    status: VerificationStatus
    failures: tuple[str, ...]
    started_at: datetime
    completed_at: datetime
    disposable_database_destroyed: bool
    active_database_unchanged: bool
    canonical_hash: str
    authority: str = "NONE"
    active_database_replacement_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        verification_id: str,
        manifest_id: str,
        disposable_database_identity: str,
        expected_schema_revision: str,
        restored_schema_revision: str | None,
        protected_count_comparisons: tuple[tuple[str, int], ...],
        restored_evidence_hash: str | None,
        status: VerificationStatus,
        failures: tuple[str, ...],
        started_at: datetime,
        completed_at: datetime,
        disposable_database_destroyed: bool,
        active_database_unchanged: bool,
    ) -> RestoreVerification:
        started = _time(started_at, "RESTORE_STARTED_TIME_INVALID")
        completed = _time(completed_at, "RESTORE_COMPLETED_TIME_INVALID")
        if completed < started or (status is VerificationStatus.VERIFIED and failures):
            raise OperationalReliabilityError("RESTORE_VERIFICATION_STATE_INVALID")
        values = (
            _id(verification_id, "RESTORE_VERIFICATION_ID_INVALID"),
            RESTORE_VERIFICATION_POLICY_V1,
            _id(manifest_id, "RESTORE_MANIFEST_ID_INVALID"),
            _id(disposable_database_identity, "RESTORE_DATABASE_IDENTITY_INVALID"),
            _id(expected_schema_revision, "RESTORE_EXPECTED_SCHEMA_INVALID"),
            None
            if restored_schema_revision is None
            else _id(restored_schema_revision, "RESTORE_SCHEMA_INVALID"),
            _pairs(protected_count_comparisons, "RESTORE_COUNT_COMPARISON_INVALID"),
            None
            if restored_evidence_hash is None
            else _hash(restored_evidence_hash, "RESTORE_EVIDENCE_HASH_INVALID"),
            status,
            tuple(sorted(_id(item, "RESTORE_FAILURE_INVALID", 500) for item in failures)),
            started,
            completed,
            disposable_database_destroyed,
            active_database_unchanged,
            "NONE",
            "NONE",
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class RecoveryAttempt:
    attempt_id: str
    policy_id: str
    scenario: str
    actions: tuple[str, ...]
    outcomes: tuple[str, ...]
    status: RecoveryStatus
    actor: str
    attempted_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    destructive_action_performed: bool = False

    @classmethod
    def create(
        cls,
        *,
        attempt_id: str,
        scenario: str,
        actions: tuple[str, ...],
        outcomes: tuple[str, ...],
        status: RecoveryStatus,
        actor: str,
        attempted_at: datetime,
    ) -> RecoveryAttempt:
        allowed = {
            "DATABASE_UNAVAILABLE",
            "INTERRUPTED_CYCLE",
            "RUNNER_RESTART",
            "STALE_LOCK",
            "INCOMPLETE_TASK",
            "DISPOSABLE_RESTORE",
        }
        normalized = _id(scenario, "RECOVERY_SCENARIO_INVALID")
        if normalized not in allowed:
            raise OperationalReliabilityError("RECOVERY_SCENARIO_UNSUPPORTED")
        values = (
            _id(attempt_id, "RECOVERY_ATTEMPT_ID_INVALID"),
            RECOVERY_POLICY_V1,
            normalized,
            tuple(_id(item, "RECOVERY_ACTION_INVALID", 500) for item in actions),
            tuple(_id(item, "RECOVERY_OUTCOME_INVALID", 500) for item in outcomes),
            status,
            _id(actor, "RECOVERY_ACTOR_INVALID"),
            _time(attempted_at, "RECOVERY_TIME_INVALID"),
            "NONE",
            False,
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class AlertDelivery:
    alert_id: str
    policy_id: str
    category: str
    source_evidence_ids: tuple[str, ...]
    severity: AlertSeverity
    sink: str
    delivery_attempt: int
    status: AlertDeliveryStatus
    message: str
    attempted_at: datetime
    delivered_at: datetime | None
    canonical_hash: str
    authority: str = "NONE"
    contains_secret_values: bool = False

    @classmethod
    def create(
        cls,
        *,
        alert_id: str,
        category: str,
        source_evidence_ids: tuple[str, ...],
        severity: AlertSeverity,
        sink: str,
        delivery_attempt: int,
        status: AlertDeliveryStatus,
        message: str,
        attempted_at: datetime,
        delivered_at: datetime | None,
    ) -> AlertDelivery:
        allowed = {
            "OPERATING_RUNNER_STOPPED",
            "REPEATED_CYCLE_FAILURE",
            "STALE_CRITICAL_MARKET_EVIDENCE",
            "DATABASE_UNAVAILABLE",
            "BACKUP_VERIFICATION_FAILED",
            "RECONCILIATION_BLOCKED",
            "EMERGENCY_STOP_ACTIVE",
            "DANGEROUS_PROVIDER_PERMISSIONS",
            "READINESS_REGRESSION",
            "RUNNER_RESTART_RECOVERED",
            # Recurring Operating Cycle Integration v2: a genuine per-task
            # FAILED outcome, or a required options-research capture window
            # that closed without a genuine capture, this cycle.
            "OPERATING_TASK_FAILED",
            "OPTIONS_CAPTURE_WINDOW_MISSED",
        }
        normalized = _id(category, "ALERT_CATEGORY_INVALID")
        if normalized not in allowed or delivery_attempt <= 0:
            raise OperationalReliabilityError("ALERT_DELIVERY_BOUND_INVALID")
        attempted = _time(attempted_at, "ALERT_ATTEMPT_TIME_INVALID")
        delivered = None if delivered_at is None else _time(delivered_at, "ALERT_TIME_INVALID")
        safe_message = str(redact_payload(message))
        if status is AlertDeliveryStatus.DELIVERED and delivered is None:
            raise OperationalReliabilityError("ALERT_DELIVERY_TIME_REQUIRED")
        values = (
            _id(alert_id, "ALERT_ID_INVALID"),
            ALERT_DELIVERY_POLICY_V1,
            normalized,
            tuple(sorted(_id(item, "ALERT_SOURCE_ID_INVALID") for item in source_evidence_ids)),
            severity,
            _id(sink, "ALERT_SINK_INVALID"),
            delivery_attempt,
            status,
            _id(safe_message, "ALERT_MESSAGE_INVALID", 1000),
            attempted,
            delivered,
            "NONE",
            False,
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class OperationalHealth:
    health_id: str
    policy_id: str
    state: OperationalHealthState
    database_ready: bool
    api_ready: bool
    runner_status: RunnerStatus
    runner_heartbeat_id: str | None
    latest_cycle_id: str | None
    next_due_at: datetime | None
    failed_or_partial_cycle_count: int
    stale_provider_evidence: bool
    credential_reference_healthy: bool
    backup_verified: bool
    execution_control_state: str
    disk_warning: str | None
    migration_revision: str
    migration_head: str
    alert_delivery_healthy: bool
    blockers: tuple[str, ...]
    assessed_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(cls, **values: Any) -> OperationalHealth:
        required = {
            "health_id",
            "state",
            "database_ready",
            "api_ready",
            "runner_status",
            "runner_heartbeat_id",
            "latest_cycle_id",
            "next_due_at",
            "failed_or_partial_cycle_count",
            "stale_provider_evidence",
            "credential_reference_healthy",
            "backup_verified",
            "execution_control_state",
            "disk_warning",
            "migration_revision",
            "migration_head",
            "alert_delivery_healthy",
            "blockers",
            "assessed_at",
        }
        if set(values) != required or int(values["failed_or_partial_cycle_count"]) < 0:
            raise OperationalReliabilityError("OPERATIONAL_HEALTH_INPUT_INVALID")
        normalized = {
            **values,
            "health_id": _id(values["health_id"], "OPERATIONAL_HEALTH_ID_INVALID"),
            "policy_id": OPERATIONAL_HEALTH_POLICY_V1,
            "assessed_at": _time(values["assessed_at"], "OPERATIONAL_HEALTH_TIME_INVALID"),
            "blockers": tuple(
                sorted(
                    _id(item, "OPERATIONAL_HEALTH_BLOCKER_INVALID") for item in values["blockers"]
                )
            ),
            "authority": "NONE",
            "live_authority": "NONE",
        }
        normalized["canonical_hash"] = content_digest(normalized)
        return cls(**normalized)


@dataclass(frozen=True, slots=True)
class KillSwitchDrill:
    drill_id: str
    policy_id: str
    starting_state: str
    emergency_state: str
    final_state: str
    paper_submission_blocked: bool
    live_submission_blocked: bool
    read_only_cycle_available: bool
    reconciliation_available: bool
    export_available: bool
    health_read_available: bool
    provider_write_count: int
    event_ids: tuple[str, ...]
    performed_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(cls, **values: Any) -> KillSwitchDrill:
        if (
            values.get("starting_state") != "LIVE_BLOCKED"
            or values.get("emergency_state") != "EMERGENCY_STOP"
            or values.get("final_state") != "LIVE_BLOCKED"
            or values.get("provider_write_count") != 0
            or not all(
                values.get(item) is True
                for item in (
                    "paper_submission_blocked",
                    "live_submission_blocked",
                    "read_only_cycle_available",
                    "reconciliation_available",
                    "export_available",
                    "health_read_available",
                )
            )
        ):
            raise OperationalReliabilityError("KILL_SWITCH_DRILL_FAILED")
        normalized = {
            **values,
            "drill_id": _id(values["drill_id"], "KILL_SWITCH_DRILL_ID_INVALID"),
            "policy_id": KILL_SWITCH_DRILL_POLICY_V1,
            "performed_at": _time(values["performed_at"], "KILL_SWITCH_DRILL_TIME_INVALID"),
            "event_ids": tuple(
                _id(item, "KILL_SWITCH_EVENT_ID_INVALID") for item in values["event_ids"]
            ),
            "authority": "NONE",
            "live_authority": "NONE",
        }
        normalized["canonical_hash"] = content_digest(normalized)
        return cls(**normalized)


def submission_allowed(control_state: str, *, paper: bool) -> bool:
    """Operational guard only; it never grants submission authority."""

    if control_state in {"EMERGENCY_STOP", "DISABLED"}:
        return False
    if not paper and control_state != "LIVE_PILOT_ARMED":
        return False
    return paper and control_state == "PAPER_ONLY"


@dataclass(frozen=True, slots=True)
class OperationalReliabilityTask:
    task_id: str
    task_type: str
    disposition: OperationalTaskDisposition
    reasons: tuple[str, ...]
    source_evidence_ids: tuple[str, ...]
    provider_request_limit: int
    provider_write_capability: str
    canonical_hash: str
    authority: str = "NONE"


def build_operational_reliability_tasks(
    *,
    cycle_id: str,
    runner: RunnerHeartbeat | None,
    manifest: BackupManifest | None,
    restore: RestoreVerification | None,
    alert: AlertDelivery | None,
    credentials_configured: bool,
    read_only_inspection_approved: bool,
    assessment_fresh: bool,
) -> tuple[OperationalReliabilityTask, ...]:
    rows = (
        (
            "RUNNER_HEALTH",
            OperationalTaskDisposition.DUE,
            () if runner is not None else ("RUNNER_HEARTBEAT_MISSING",),
            () if runner is None else (runner.heartbeat_id,),
            0,
        ),
        (
            "BACKUP_DUE_STATUS",
            OperationalTaskDisposition.DUE,
            () if manifest is not None else ("VERIFIED_BACKUP_MISSING",),
            () if manifest is None else (manifest.manifest_id,),
            0,
        ),
        (
            "LATEST_RESTORE_VERIFICATION",
            OperationalTaskDisposition.DUE,
            ()
            if restore is not None and restore.status is VerificationStatus.VERIFIED
            else ("VERIFIED_RESTORE_MISSING",),
            () if restore is None else (restore.verification_id,),
            0,
        ),
        (
            "ALERT_DELIVERY_HEALTH",
            OperationalTaskDisposition.DUE,
            ()
            if alert is not None and alert.status is AlertDeliveryStatus.DELIVERED
            else ("ALERT_DELIVERY_NOT_HEALTHY",),
            () if alert is None else (alert.alert_id,),
            0,
        ),
        (
            "CAPABILITY_INSPECTION_DUE_STATUS",
            (
                OperationalTaskDisposition.DUE
                if credentials_configured and read_only_inspection_approved
                else OperationalTaskDisposition.NOT_DUE
            ),
            (
                ()
                if credentials_configured and read_only_inspection_approved
                else ("CREDENTIALS_ABSENT_OR_READ_ONLY_APPROVAL_MISSING",)
            ),
            (),
            10 if credentials_configured and read_only_inspection_approved else 0,
        ),
        (
            "READINESS_REASSESSMENT",
            (
                OperationalTaskDisposition.NOT_DUE
                if assessment_fresh
                else OperationalTaskDisposition.DUE
            ),
            () if not assessment_fresh else ("READINESS_ASSESSMENT_FRESH",),
            (),
            0,
        ),
    )
    tasks: list[OperationalReliabilityTask] = []
    for ordinal, (task_type, disposition, reasons, sources, request_limit) in enumerate(rows, 1):
        task_id = f"{cycle_id}-operational-reliability-{ordinal}"
        canonical_hash = content_digest(
            (
                task_id,
                task_type,
                disposition,
                tuple(reasons),
                tuple(sources),
                request_limit,
                "NONE",
                "NONE",
            )
        )
        tasks.append(
            OperationalReliabilityTask(
                task_id=task_id,
                task_type=task_type,
                disposition=disposition,
                reasons=tuple(reasons),
                source_evidence_ids=tuple(sources),
                provider_request_limit=request_limit,
                provider_write_capability="NONE",
                canonical_hash=canonical_hash,
            )
        )
    return tuple(tasks)
