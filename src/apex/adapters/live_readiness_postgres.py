"""Immutable PostgreSQL repository for secure provider and readiness evidence."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypeVar, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.domain.live_readiness import (
    AccountEnvironment,
    AuthenticatedReadStatus,
    BackupReadinessRecord,
    BackupReadinessStatus,
    CredentialReference,
    CredentialValidationStatus,
    ExecutionControlEvent,
    ExecutionControlState,
    LiveReadinessAssessment,
    LiveReadinessError,
    LiveRiskDecision,
    LiveRiskPolicy,
    PermissionClass,
    Provider,
    ProviderAccountStatus,
    ProviderCapabilitySnapshot,
    ReadinessExport,
    ReadinessRecommendation,
    RiskDecisionState,
    SecretBackend,
)

live_readiness_metadata = sa.MetaData()


def _evidence_table(name: str, identity: str) -> sa.Table:
    return sa.Table(
        name,
        live_readiness_metadata,
        sa.Column(identity, sa.String(300), primary_key=True),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(20), nullable=False, server_default="NONE"),
        sa.CheckConstraint("length(canonical_hash) = 64", name=f"ck_{name}_hash"),
        sa.CheckConstraint("authority = 'NONE'", name=f"ck_{name}_authority"),
    )


credential_reference_table = _evidence_table("provider_credential_reference", "reference_id")
credential_reference_table.append_constraint(
    sa.CheckConstraint(
        "lower(payload) NOT LIKE '%\"api_key\":%' "
        "AND lower(payload) NOT LIKE '%\"private_key\":%' "
        "AND lower(payload) NOT LIKE '%\"passphrase\":%' "
        "AND lower(payload) NOT LIKE '%\"password\":%' "
        "AND lower(payload) NOT LIKE '%\"bearer_token\":%' "
        "AND lower(payload) NOT LIKE '%-----begin %' "
        "AND lower(payload) NOT LIKE '%bearer %'",
        name="ck_provider_credential_no_secret_material",
    )
)
capability_snapshot_table = _evidence_table("provider_account_capability_snapshot", "snapshot_id")
live_risk_policy_table = _evidence_table("live_risk_policy", "policy_id")
live_risk_decision_table = _evidence_table("live_risk_decision", "decision_id")
execution_control_table = _evidence_table("execution_control_event", "event_id")
execution_control_table.append_column(sa.Column("control_id", sa.String(300), nullable=False))
execution_control_table.append_column(sa.Column("ordinal", sa.Integer, nullable=False))
execution_control_table.append_constraint(
    sa.UniqueConstraint("control_id", "ordinal", name="uq_execution_control_ordinal")
)
execution_control_table.append_constraint(
    sa.CheckConstraint("ordinal > 0", name="ck_execution_control_positive_ordinal")
)
backup_readiness_table = _evidence_table("backup_readiness_record", "record_id")
live_readiness_assessment_table = _evidence_table("live_readiness_assessment", "assessment_id")
readiness_export_table = _evidence_table("live_readiness_export", "export_id")

LIVE_READINESS_EVIDENCE_TABLES = tuple(live_readiness_metadata.tables)


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _payload(value: object) -> str:
    return json.dumps(_json_value(asdict(cast(Any, value))), sort_keys=True, separators=(",", ":"))


def _load(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise LiveReadinessError("LIVE_READINESS_RETAINED_PAYLOAD_INVALID")
    return cast(dict[str, Any], value)


def _dt(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _credential(row: RowMapping) -> CredentialReference:
    value = _load(row)
    return CredentialReference(
        reference_id=str(value["reference_id"]),
        version=int(value["version"]),
        provider=Provider(str(value["provider"])),
        account_environment=AccountEnvironment(str(value["account_environment"])),
        account_purpose=str(value["account_purpose"]),
        required_permissions=tuple(PermissionClass(item) for item in value["required_permissions"]),
        secret_backend=SecretBackend(str(value["secret_backend"])),
        backend_reference_id=str(value["backend_reference_id"]),
        created_at=cast(datetime, _dt(value["created_at"])),
        rotated_at=_dt(value["rotated_at"]),
        validation_status=CredentialValidationStatus(str(value["validation_status"])),
        last_successful_read_at=_dt(value["last_successful_read_at"]),
        warnings=tuple(str(item) for item in value["warnings"]),
        previous_reference_id=(
            None if value["previous_reference_id"] is None else str(value["previous_reference_id"])
        ),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
        secret_values_retained=bool(value["secret_values_retained"]),
    )


def _capability(row: RowMapping) -> ProviderCapabilitySnapshot:
    value = _load(row)
    return ProviderCapabilitySnapshot(
        snapshot_id=str(value["snapshot_id"]),
        version=int(value["version"]),
        provider=Provider(str(value["provider"])),
        account_scope_id=str(value["account_scope_id"]),
        credential_reference_id=(
            None
            if value["credential_reference_id"] is None
            else str(value["credential_reference_id"])
        ),
        account_environment=AccountEnvironment(str(value["account_environment"])),
        authenticated_read_status=AuthenticatedReadStatus(str(value["authenticated_read_status"])),
        permissions=tuple(PermissionClass(item) for item in value["permissions"]),
        supported_products=tuple(str(item) for item in value["supported_products"]),
        restrictions=tuple(str(item) for item in value["restrictions"]),
        balances=tuple((str(item[0]), Decimal(str(item[1]))) for item in value["balances"]),
        fee_evidence=tuple((str(item[0]), str(item[1])) for item in value["fee_evidence"]),
        precision_constraints=tuple(
            (str(item[0]), str(item[1])) for item in value["precision_constraints"]
        ),
        minimum_size_constraints=tuple(
            (str(item[0]), str(item[1])) for item in value["minimum_size_constraints"]
        ),
        order_types=tuple(str(item) for item in value["order_types"]),
        preview_available=bool(value["preview_available"]),
        trade_available=bool(value["trade_available"]),
        transfer_available=bool(value["transfer_available"]),
        withdrawal_available=bool(value["withdrawal_available"]),
        account_status=ProviderAccountStatus(str(value["account_status"])),
        observed_at=cast(datetime, _dt(value["observed_at"])),
        ingested_at=cast(datetime, _dt(value["ingested_at"])),
        evidence_class=str(value["evidence_class"]),
        limitations=tuple(str(item) for item in value["limitations"]),
        provider_request_count=int(value["provider_request_count"]),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
        provider_write_capability=str(value["provider_write_capability"]),
        live_authority=str(value["live_authority"]),
    )


def _policy(row: RowMapping) -> LiveRiskPolicy:
    value = _load(row)
    return LiveRiskPolicy(
        policy_id=str(value["policy_id"]),
        version=int(value["version"]),
        allowed_venues=tuple(Provider(item) for item in value["allowed_venues"]),
        allowed_account_scopes=tuple(str(item) for item in value["allowed_account_scopes"]),
        allowed_products=tuple(str(item) for item in value["allowed_products"]),
        leverage_allowed=bool(value["leverage_allowed"]),
        shorting_allowed=bool(value["shorting_allowed"]),
        maximum_total_live_capital=Decimal(str(value["maximum_total_live_capital"])),
        minimum_retained_cash_ratio=Decimal(str(value["minimum_retained_cash_ratio"])),
        maximum_per_opportunity=Decimal(str(value["maximum_per_opportunity"])),
        maximum_per_strategy=Decimal(str(value["maximum_per_strategy"])),
        maximum_per_instrument=Decimal(str(value["maximum_per_instrument"])),
        maximum_per_venue=Decimal(str(value["maximum_per_venue"])),
        maximum_open_orders=int(value["maximum_open_orders"]),
        maximum_open_positions=int(value["maximum_open_positions"]),
        maximum_daily_realized_loss=Decimal(str(value["maximum_daily_realized_loss"])),
        maximum_daily_modeled_loss=Decimal(str(value["maximum_daily_modeled_loss"])),
        maximum_drawdown_ratio=Decimal(str(value["maximum_drawdown_ratio"])),
        maximum_unresolved_reconciliation_severity=int(
            value["maximum_unresolved_reconciliation_severity"]
        ),
        minimum_strategy_health=str(value["minimum_strategy_health"]),
        minimum_paper_sample_count=int(value["minimum_paper_sample_count"]),
        minimum_clean_fill_count=int(value["minimum_clean_fill_count"]),
        minimum_settlement_count=int(value["minimum_settlement_count"]),
        capability_freshness_seconds=int(value["capability_freshness_seconds"]),
        require_provider_healthy=bool(value["require_provider_healthy"]),
        require_operating_cycle_healthy=bool(value["require_operating_cycle_healthy"]),
        require_backup_verified=bool(value["require_backup_verified"]),
        emergency_stop_behavior=str(value["emergency_stop_behavior"]),
        created_at=cast(datetime, _dt(value["created_at"])),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
        capital_authority=str(value["capital_authority"]),
        live_authority=str(value["live_authority"]),
    )


def _decision(row: RowMapping) -> LiveRiskDecision:
    value = _load(row)
    return LiveRiskDecision(
        decision_id=str(value["decision_id"]),
        policy_id=str(value["policy_id"]),
        state=RiskDecisionState(str(value["state"])),
        blockers=tuple(str(item) for item in value["blockers"]),
        evaluated_metrics=tuple(
            (str(item[0]), str(item[1])) for item in value["evaluated_metrics"]
        ),
        evaluated_at=cast(datetime, _dt(value["evaluated_at"])),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
        capital_authority=str(value["capital_authority"]),
    )


def _control(row: RowMapping) -> ExecutionControlEvent:
    value = _load(row)
    return ExecutionControlEvent(
        event_id=str(value["event_id"]),
        control_id=str(value["control_id"]),
        ordinal=int(value["ordinal"]),
        state=ExecutionControlState(str(value["state"])),
        actor=str(value["actor"]),
        reason=str(value["reason"]),
        effective_at=cast(datetime, _dt(value["effective_at"])),
        expires_at=_dt(value["expires_at"]),
        previous_state=(
            None
            if value["previous_state"] is None
            else ExecutionControlState(str(value["previous_state"]))
        ),
        idempotency_key=str(value["idempotency_key"]),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
        provider_permission_changed=bool(value["provider_permission_changed"]),
    )


def _backup(row: RowMapping) -> BackupReadinessRecord:
    value = _load(row)
    return BackupReadinessRecord(
        record_id=str(value["record_id"]),
        database_backup_method=str(value["database_backup_method"]),
        latest_verified_backup_at=_dt(value["latest_verified_backup_at"]),
        restore_test_status=str(value["restore_test_status"]),
        configuration_backup_status=str(value["configuration_backup_status"]),
        credential_reference_recovery_procedure=str(
            value["credential_reference_recovery_procedure"]
        ),
        recovery_point_objective_seconds=int(value["recovery_point_objective_seconds"]),
        recovery_time_objective_seconds=int(value["recovery_time_objective_seconds"]),
        known_gaps=tuple(str(item) for item in value["known_gaps"]),
        status=BackupReadinessStatus(str(value["status"])),
        assessed_at=cast(datetime, _dt(value["assessed_at"])),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
        secret_values_included=bool(value["secret_values_included"]),
    )


def _assessment(row: RowMapping) -> LiveReadinessAssessment:
    value = _load(row)
    return LiveReadinessAssessment(
        assessment_id=str(value["assessment_id"]),
        policy_id=str(value["policy_id"]),
        venue=Provider(str(value["venue"])),
        account_scope_id=str(value["account_scope_id"]),
        capability_snapshot_id=(
            None
            if value["capability_snapshot_id"] is None
            else str(value["capability_snapshot_id"])
        ),
        credential_status=str(value["credential_status"]),
        permission_status=str(value["permission_status"]),
        strategy_or_method_id=(
            None if value["strategy_or_method_id"] is None else str(value["strategy_or_method_id"])
        ),
        paper_sample_count=int(value["paper_sample_count"]),
        fill_count=int(value["fill_count"]),
        settlement_count=int(value["settlement_count"]),
        strategy_health_state=str(value["strategy_health_state"]),
        reconciliation_quality=str(value["reconciliation_quality"]),
        operating_cycle_health=str(value["operating_cycle_health"]),
        backup_readiness_status=str(value["backup_readiness_status"]),
        execution_control_state=ExecutionControlState(str(value["execution_control_state"])),
        risk_decision_id=str(value["risk_decision_id"]),
        risk_policy_result=RiskDecisionState(str(value["risk_policy_result"])),
        unresolved_blockers=tuple(str(item) for item in value["unresolved_blockers"]),
        recommendation=ReadinessRecommendation(str(value["recommendation"])),
        actor=str(value["actor"]),
        assessed_at=cast(datetime, _dt(value["assessed_at"])),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
        financial_mutation_capability=str(value["financial_mutation_capability"]),
        live_authority=str(value["live_authority"]),
    )


def _export(row: RowMapping) -> ReadinessExport:
    value = _load(row)
    return ReadinessExport(
        export_id=str(value["export_id"]),
        assessment_id=str(value["assessment_id"]),
        categories=tuple((str(item[0]), int(item[1])) for item in value["categories"]),
        evidence_ids=tuple(str(item) for item in value["evidence_ids"]),
        evidence_hashes=tuple(str(item) for item in value["evidence_hashes"]),
        paper_records=int(value["paper_records"]),
        live_records=int(value["live_records"]),
        redactions_applied=tuple(str(item) for item in value["redactions_applied"]),
        generated_at=cast(datetime, _dt(value["generated_at"])),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
        financial_mutation_capability=str(value["financial_mutation_capability"]),
        secrets_included=bool(value["secrets_included"]),
    )


T = TypeVar("T")


class PostgresLiveReadinessRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _save(
        self,
        *,
        table: sa.Table,
        identity_column: str,
        identity: str,
        value: CredentialReference
        | ProviderCapabilitySnapshot
        | LiveRiskPolicy
        | LiveRiskDecision
        | ExecutionControlEvent
        | BackupReadinessRecord
        | LiveReadinessAssessment
        | ReadinessExport,
        extra: dict[str, object] | None = None,
    ) -> None:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(table.c.canonical_hash).where(table.c[identity_column] == identity)
            ).scalar_one_or_none()
            canonical_hash = str(value.canonical_hash)
            if existing is not None:
                if existing != canonical_hash:
                    raise LiveReadinessError("LIVE_READINESS_IDEMPOTENCY_CONFLICT")
                return
            connection.execute(
                table.insert().values(
                    **{
                        identity_column: identity,
                        "payload": _payload(value),
                        "canonical_hash": canonical_hash,
                        "authority": "NONE",
                        **(extra or {}),
                    }
                )
            )

    def _one(
        self,
        table: sa.Table,
        identity_column: str,
        identity: str,
        loader: Callable[[RowMapping], T],
    ) -> T | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(sa.select(table).where(table.c[identity_column] == identity))
                .mappings()
                .one_or_none()
            )
        return None if row is None else loader(row)

    def save_credential_reference(self, value: CredentialReference) -> CredentialReference:
        self._save(
            table=credential_reference_table,
            identity_column="reference_id",
            identity=value.reference_id,
            value=value,
        )
        return value

    def credential_reference(self, reference_id: str) -> CredentialReference | None:
        return self._one(credential_reference_table, "reference_id", reference_id, _credential)

    def credential_references(self) -> tuple[CredentialReference, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(sa.select(credential_reference_table)).mappings().all()
        return tuple(
            sorted(
                (_credential(row) for row in rows),
                key=lambda item: (item.provider.value, item.reference_id),
            )
        )

    def save_capability(self, value: ProviderCapabilitySnapshot) -> ProviderCapabilitySnapshot:
        self._save(
            table=capability_snapshot_table,
            identity_column="snapshot_id",
            identity=value.snapshot_id,
            value=value,
        )
        return value

    def capability(self, snapshot_id: str) -> ProviderCapabilitySnapshot | None:
        return self._one(capability_snapshot_table, "snapshot_id", snapshot_id, _capability)

    def latest_capability(
        self, provider: Provider, account_scope_id: str
    ) -> ProviderCapabilitySnapshot | None:
        with self._engine.connect() as connection:
            rows = connection.execute(sa.select(capability_snapshot_table)).mappings().all()
        values = [
            item
            for row in rows
            if (item := _capability(row)).provider is provider
            and item.account_scope_id == account_scope_id
        ]
        return (
            None
            if not values
            else max(values, key=lambda item: (item.observed_at, item.snapshot_id))
        )

    def save_risk_policy(self, value: LiveRiskPolicy) -> LiveRiskPolicy:
        self._save(
            table=live_risk_policy_table,
            identity_column="policy_id",
            identity=value.policy_id,
            value=value,
        )
        return value

    def risk_policy(self, policy_id: str) -> LiveRiskPolicy | None:
        return self._one(live_risk_policy_table, "policy_id", policy_id, _policy)

    def save_risk_decision(self, value: LiveRiskDecision) -> LiveRiskDecision:
        self._save(
            table=live_risk_decision_table,
            identity_column="decision_id",
            identity=value.decision_id,
            value=value,
        )
        return value

    def save_control_event(self, value: ExecutionControlEvent) -> ExecutionControlEvent:
        with self._engine.begin() as connection:
            rows = (
                connection.execute(
                    sa.select(execution_control_table)
                    .where(execution_control_table.c.control_id == value.control_id)
                    .order_by(execution_control_table.c.ordinal)
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            existing = next((row for row in rows if row["event_id"] == value.event_id), None)
            if existing is not None:
                if existing["canonical_hash"] != value.canonical_hash:
                    raise LiveReadinessError("LIVE_READINESS_IDEMPOTENCY_CONFLICT")
                return value
            if rows:
                previous = _control(rows[-1])
                if (
                    value.ordinal != previous.ordinal + 1
                    or value.previous_state is not previous.state
                ):
                    raise LiveReadinessError("EXECUTION_CONTROL_TRANSITION_CONFLICT")
            elif value.ordinal != 1 or value.previous_state is not None:
                raise LiveReadinessError("EXECUTION_CONTROL_INITIAL_STATE_INVALID")
            connection.execute(
                execution_control_table.insert().values(
                    event_id=value.event_id,
                    control_id=value.control_id,
                    ordinal=value.ordinal,
                    payload=_payload(value),
                    canonical_hash=value.canonical_hash,
                    authority="NONE",
                )
            )
        return value

    def control_events(self, control_id: str) -> tuple[ExecutionControlEvent, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(execution_control_table)
                    .where(execution_control_table.c.control_id == control_id)
                    .order_by(execution_control_table.c.ordinal)
                )
                .mappings()
                .all()
            )
        return tuple(_control(row) for row in rows)

    def save_backup_readiness(self, value: BackupReadinessRecord) -> BackupReadinessRecord:
        self._save(
            table=backup_readiness_table,
            identity_column="record_id",
            identity=value.record_id,
            value=value,
        )
        return value

    def backup_readiness(self, record_id: str) -> BackupReadinessRecord | None:
        return self._one(backup_readiness_table, "record_id", record_id, _backup)

    def latest_backup_readiness(self) -> BackupReadinessRecord | None:
        with self._engine.connect() as connection:
            rows = connection.execute(sa.select(backup_readiness_table)).mappings().all()
        values = tuple(_backup(row) for row in rows)
        return (
            None if not values else max(values, key=lambda item: (item.assessed_at, item.record_id))
        )

    def save_assessment(self, value: LiveReadinessAssessment) -> LiveReadinessAssessment:
        self._save(
            table=live_readiness_assessment_table,
            identity_column="assessment_id",
            identity=value.assessment_id,
            value=value,
        )
        return value

    def assessment(self, assessment_id: str) -> LiveReadinessAssessment | None:
        return self._one(
            live_readiness_assessment_table, "assessment_id", assessment_id, _assessment
        )

    def assessments(self) -> tuple[LiveReadinessAssessment, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(sa.select(live_readiness_assessment_table)).mappings().all()
        return tuple(
            sorted(
                (_assessment(row) for row in rows),
                key=lambda item: (item.assessed_at, item.assessment_id),
            )
        )

    def save_export(self, value: ReadinessExport) -> ReadinessExport:
        self._save(
            table=readiness_export_table,
            identity_column="export_id",
            identity=value.export_id,
            value=value,
        )
        return value

    def export(self, export_id: str) -> ReadinessExport | None:
        return self._one(readiness_export_table, "export_id", export_id, _export)
