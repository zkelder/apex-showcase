"""Immutable PostgreSQL evidence for bounded operational reliability."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.application.operational_reliability import OperationalReliabilityRepository
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
    ServiceDefinition,
    VerificationStatus,
)

operational_reliability_metadata = sa.MetaData()


def _table(name: str, identity: str, *columns: Any) -> sa.Table:
    return sa.Table(
        name,
        operational_reliability_metadata,
        sa.Column(identity, sa.String(300), primary_key=True),
        *columns,
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(20), nullable=False, server_default="NONE"),
        sa.CheckConstraint("length(canonical_hash) = 64", name=f"ck_{name}_hash"),
        sa.CheckConstraint("authority = 'NONE'", name=f"ck_{name}_authority"),
    )


deployment_profile_table = _table("operational_deployment_profile", "profile_id")
runner_heartbeat_table = _table(
    "operational_runner_heartbeat",
    "heartbeat_id",
    sa.Column("runner_id", sa.String(300), nullable=False),
    sa.Column("run_instance_id", sa.String(300), nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("runner_id", "ordinal", name="uq_operational_runner_ordinal"),
    sa.CheckConstraint("ordinal > 0", name="ck_operational_runner_ordinal"),
)
backup_manifest_table = _table("operational_backup_manifest", "manifest_id")
restore_verification_table = _table("operational_restore_verification", "verification_id")
recovery_attempt_table = _table("operational_recovery_attempt", "attempt_id")
alert_delivery_table = _table("operational_alert_delivery", "alert_id")
operational_health_table = _table("operational_health_projection", "health_id")
kill_switch_drill_table = _table("operational_kill_switch_drill", "drill_id")

OPERATIONAL_RELIABILITY_TABLES = tuple(operational_reliability_metadata.tables)
T = TypeVar("T")


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _payload(value: object) -> str:
    return json.dumps(_json_value(asdict(cast(Any, value))), sort_keys=True, separators=(",", ":"))


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise OperationalReliabilityError("OPERATIONAL_RETAINED_PAYLOAD_INVALID")
    return cast(dict[str, Any], value)


def _dt(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def _pairs(value: object) -> tuple[tuple[str, int], ...]:
    return tuple((str(item[0]), int(str(item[1]))) for item in cast(list[list[object]], value))


def _strings(value: object) -> tuple[str, ...]:
    return tuple(str(item) for item in cast(list[object], value))


def _profile(row: RowMapping) -> DeploymentProfile:
    value = _data(row)
    services = tuple(ServiceDefinition(**item) for item in value["services"])
    return DeploymentProfile(
        profile_id=str(value["profile_id"]),
        version=int(value["version"]),
        policy_id=str(value["policy_id"]),
        services=services,
        created_at=_dt(value["created_at"]),
        actor=str(value["actor"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        provider_write_capability="NONE",
        live_authority="NONE",
    )


def _heartbeat(row: RowMapping) -> RunnerHeartbeat:
    value = _data(row)
    return RunnerHeartbeat(
        heartbeat_id=str(value["heartbeat_id"]),
        runner_id=str(value["runner_id"]),
        run_instance_id=str(value["run_instance_id"]),
        plan_id=str(value["plan_id"]),
        ordinal=int(value["ordinal"]),
        status=RunnerStatus(value["status"]),
        heartbeat_at=_dt(value["heartbeat_at"]),
        lease_expires_at=_dt(value["lease_expires_at"]),
        last_cycle_id=None if value["last_cycle_id"] is None else str(value["last_cycle_id"]),
        next_due_at=_dt(value["next_due_at"]),
        due_evaluation_count=int(value["due_evaluation_count"]),
        duplicate_prevention_key=str(value["duplicate_prevention_key"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        provider_request_count=0,
        provider_write_count=0,
        live_authority="NONE",
    )


def _manifest(row: RowMapping) -> BackupManifest:
    value = _data(row)
    return BackupManifest(
        manifest_id=str(value["manifest_id"]),
        policy_id=str(value["policy_id"]),
        schema_revision=str(value["schema_revision"]),
        source_database_identity=str(value["source_database_identity"]),
        backup_timestamp=_dt(value["backup_timestamp"]),
        storage_reference=str(value["storage_reference"]),
        file_size_bytes=int(value["file_size_bytes"]),
        sha256=str(value["sha256"]),
        table_counts=_pairs(value["table_counts"]),
        evidence_exclusions=_strings(value["evidence_exclusions"]),
        secret_handling=str(value["secret_handling"]),
        selected_evidence_hash=str(value["selected_evidence_hash"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        includes_decrypted_secrets=False,
    )


def _verification(row: RowMapping) -> RestoreVerification:
    value = _data(row)
    return RestoreVerification(
        verification_id=str(value["verification_id"]),
        policy_id=str(value["policy_id"]),
        manifest_id=str(value["manifest_id"]),
        disposable_database_identity=str(value["disposable_database_identity"]),
        expected_schema_revision=str(value["expected_schema_revision"]),
        restored_schema_revision=(
            None
            if value["restored_schema_revision"] is None
            else str(value["restored_schema_revision"])
        ),
        protected_count_comparisons=_pairs(value["protected_count_comparisons"]),
        restored_evidence_hash=(
            None
            if value["restored_evidence_hash"] is None
            else str(value["restored_evidence_hash"])
        ),
        status=VerificationStatus(value["status"]),
        failures=_strings(value["failures"]),
        started_at=_dt(value["started_at"]),
        completed_at=_dt(value["completed_at"]),
        disposable_database_destroyed=bool(value["disposable_database_destroyed"]),
        active_database_unchanged=bool(value["active_database_unchanged"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        active_database_replacement_authority="NONE",
    )


def _recovery(row: RowMapping) -> RecoveryAttempt:
    value = _data(row)
    return RecoveryAttempt(
        attempt_id=str(value["attempt_id"]),
        policy_id=str(value["policy_id"]),
        scenario=str(value["scenario"]),
        actions=_strings(value["actions"]),
        outcomes=_strings(value["outcomes"]),
        status=RecoveryStatus(value["status"]),
        actor=str(value["actor"]),
        attempted_at=_dt(value["attempted_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        destructive_action_performed=False,
    )


def _alert(row: RowMapping) -> AlertDelivery:
    value = _data(row)
    return AlertDelivery(
        alert_id=str(value["alert_id"]),
        policy_id=str(value["policy_id"]),
        category=str(value["category"]),
        source_evidence_ids=_strings(value["source_evidence_ids"]),
        severity=AlertSeverity(value["severity"]),
        sink=str(value["sink"]),
        delivery_attempt=int(value["delivery_attempt"]),
        status=AlertDeliveryStatus(value["status"]),
        message=str(value["message"]),
        attempted_at=_dt(value["attempted_at"]),
        delivered_at=None if value["delivered_at"] is None else _dt(value["delivered_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        contains_secret_values=False,
    )


def _health(row: RowMapping) -> OperationalHealth:
    value = _data(row)
    return OperationalHealth(
        health_id=str(value["health_id"]),
        policy_id=str(value["policy_id"]),
        state=OperationalHealthState(value["state"]),
        database_ready=bool(value["database_ready"]),
        api_ready=bool(value["api_ready"]),
        runner_status=RunnerStatus(value["runner_status"]),
        runner_heartbeat_id=(
            None if value["runner_heartbeat_id"] is None else str(value["runner_heartbeat_id"])
        ),
        latest_cycle_id=None if value["latest_cycle_id"] is None else str(value["latest_cycle_id"]),
        next_due_at=None if value["next_due_at"] is None else _dt(value["next_due_at"]),
        failed_or_partial_cycle_count=int(value["failed_or_partial_cycle_count"]),
        stale_provider_evidence=bool(value["stale_provider_evidence"]),
        credential_reference_healthy=bool(value["credential_reference_healthy"]),
        backup_verified=bool(value["backup_verified"]),
        execution_control_state=str(value["execution_control_state"]),
        disk_warning=None if value["disk_warning"] is None else str(value["disk_warning"]),
        migration_revision=str(value["migration_revision"]),
        migration_head=str(value["migration_head"]),
        alert_delivery_healthy=bool(value["alert_delivery_healthy"]),
        blockers=_strings(value["blockers"]),
        assessed_at=_dt(value["assessed_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        live_authority="NONE",
    )


def _drill(row: RowMapping) -> KillSwitchDrill:
    value = _data(row)
    return KillSwitchDrill(
        drill_id=str(value["drill_id"]),
        policy_id=str(value["policy_id"]),
        starting_state=str(value["starting_state"]),
        emergency_state=str(value["emergency_state"]),
        final_state=str(value["final_state"]),
        paper_submission_blocked=bool(value["paper_submission_blocked"]),
        live_submission_blocked=bool(value["live_submission_blocked"]),
        read_only_cycle_available=bool(value["read_only_cycle_available"]),
        reconciliation_available=bool(value["reconciliation_available"]),
        export_available=bool(value["export_available"]),
        health_read_available=bool(value["health_read_available"]),
        provider_write_count=int(value["provider_write_count"]),
        event_ids=_strings(value["event_ids"]),
        performed_at=_dt(value["performed_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        live_authority="NONE",
    )


class PostgresOperationalReliabilityRepository(OperationalReliabilityRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _save(self, table: sa.Table, identity: str, value: T) -> T:
        key = str(getattr(value, identity))
        payload = _payload(value)
        canonical_hash = str(cast(Any, value).canonical_hash)
        with self._engine.begin() as connection:
            existing = (
                connection.execute(sa.select(table).where(table.c[identity] == key))
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if str(existing["canonical_hash"]) != canonical_hash:
                    raise OperationalReliabilityError("OPERATIONAL_EVIDENCE_ID_CONFLICT")
                return value
            columns: dict[str, object] = {
                identity: key,
                "payload": payload,
                "canonical_hash": canonical_hash,
                "authority": "NONE",
            }
            if table is runner_heartbeat_table:
                heartbeat = cast(RunnerHeartbeat, value)
                columns.update(
                    runner_id=heartbeat.runner_id,
                    run_instance_id=heartbeat.run_instance_id,
                    ordinal=heartbeat.ordinal,
                    status=heartbeat.status.value,
                    heartbeat_at=heartbeat.heartbeat_at,
                    lease_expires_at=heartbeat.lease_expires_at,
                )
            try:
                connection.execute(table.insert().values(**columns))
            except IntegrityError as error:
                raise OperationalReliabilityError("OPERATIONAL_EVIDENCE_CONFLICT") from error
        return value

    def _one(
        self,
        table: sa.Table,
        identity: str,
        key: str,
        loader: Callable[[RowMapping], T],
    ) -> T | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(sa.select(table).where(table.c[identity] == key))
                .mappings()
                .one_or_none()
            )
        return None if row is None else loader(row)

    def _all(self, table: sa.Table, loader: Callable[[RowMapping], T]) -> tuple[T, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(sa.select(table)).mappings().all()
        return tuple(loader(row) for row in rows)

    def save_deployment_profile(self, value: DeploymentProfile) -> DeploymentProfile:
        return self._save(deployment_profile_table, "profile_id", value)

    def deployment_profile(self, profile_id: str) -> DeploymentProfile | None:
        return self._one(deployment_profile_table, "profile_id", profile_id, _profile)

    def deployment_profiles(self) -> tuple[DeploymentProfile, ...]:
        return self._all(deployment_profile_table, _profile)

    def claim_runner_heartbeat(self, value: RunnerHeartbeat) -> RunnerHeartbeat:
        with self._engine.begin() as connection:
            connection.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtext(:runner_id))"),
                {"runner_id": value.runner_id},
            )
            latest = (
                connection.execute(
                    sa.select(runner_heartbeat_table)
                    .where(runner_heartbeat_table.c.runner_id == value.runner_id)
                    .order_by(runner_heartbeat_table.c.ordinal.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if latest is not None:
                retained = _heartbeat(latest)
                if value.ordinal != retained.ordinal + 1:
                    raise OperationalReliabilityError("RUNNER_HEARTBEAT_ORDINAL_CONFLICT")
                if (
                    retained.run_instance_id != value.run_instance_id
                    and retained.status in {RunnerStatus.RUNNING, RunnerStatus.PAUSED}
                    and retained.lease_expires_at > value.heartbeat_at
                ):
                    raise OperationalReliabilityError("RUNNER_DUPLICATE_ACTIVE_LEASE")
            elif value.ordinal != 1:
                raise OperationalReliabilityError("RUNNER_FIRST_HEARTBEAT_ORDINAL_INVALID")
            connection.execute(
                runner_heartbeat_table.insert().values(
                    heartbeat_id=value.heartbeat_id,
                    runner_id=value.runner_id,
                    run_instance_id=value.run_instance_id,
                    ordinal=value.ordinal,
                    status=value.status.value,
                    heartbeat_at=value.heartbeat_at,
                    lease_expires_at=value.lease_expires_at,
                    payload=_payload(value),
                    canonical_hash=value.canonical_hash,
                    authority="NONE",
                )
            )
        return value

    def runner_heartbeats(self, runner_id: str) -> tuple[RunnerHeartbeat, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(runner_heartbeat_table)
                    .where(runner_heartbeat_table.c.runner_id == runner_id)
                    .order_by(runner_heartbeat_table.c.ordinal)
                )
                .mappings()
                .all()
            )
        return tuple(_heartbeat(row) for row in rows)

    def latest_runner_heartbeat(self, runner_id: str) -> RunnerHeartbeat | None:
        values = self.runner_heartbeats(runner_id)
        return None if not values else values[-1]

    def save_backup_manifest(self, value: BackupManifest) -> BackupManifest:
        return self._save(backup_manifest_table, "manifest_id", value)

    def backup_manifest(self, manifest_id: str) -> BackupManifest | None:
        return self._one(backup_manifest_table, "manifest_id", manifest_id, _manifest)

    def backup_manifests(self) -> tuple[BackupManifest, ...]:
        return self._all(backup_manifest_table, _manifest)

    def save_restore_verification(self, value: RestoreVerification) -> RestoreVerification:
        return self._save(restore_verification_table, "verification_id", value)

    def restore_verification(self, verification_id: str) -> RestoreVerification | None:
        return self._one(
            restore_verification_table, "verification_id", verification_id, _verification
        )

    def restore_verifications(self) -> tuple[RestoreVerification, ...]:
        return self._all(restore_verification_table, _verification)

    def save_recovery_attempt(self, value: RecoveryAttempt) -> RecoveryAttempt:
        return self._save(recovery_attempt_table, "attempt_id", value)

    def recovery_attempts(self) -> tuple[RecoveryAttempt, ...]:
        return self._all(recovery_attempt_table, _recovery)

    def save_alert_delivery(self, value: AlertDelivery) -> AlertDelivery:
        return self._save(alert_delivery_table, "alert_id", value)

    def alert_deliveries(self) -> tuple[AlertDelivery, ...]:
        return self._all(alert_delivery_table, _alert)

    def save_operational_health(self, value: OperationalHealth) -> OperationalHealth:
        return self._save(operational_health_table, "health_id", value)

    def operational_health(self, health_id: str) -> OperationalHealth | None:
        return self._one(operational_health_table, "health_id", health_id, _health)

    def health_projections(self) -> tuple[OperationalHealth, ...]:
        return self._all(operational_health_table, _health)

    def save_kill_switch_drill(self, value: KillSwitchDrill) -> KillSwitchDrill:
        return self._save(kill_switch_drill_table, "drill_id", value)

    def kill_switch_drills(self) -> tuple[KillSwitchDrill, ...]:
        return self._all(kill_switch_drill_table, _drill)
