"""Use cases for secure, read-only provider and LIVE-readiness evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol, TypeVar, cast

from apex.domain.live_readiness import (
    AccountEnvironment,
    AuthenticatedReadStatus,
    BackupReadinessRecord,
    CredentialReference,
    ExecutionControlEvent,
    ExecutionControlState,
    LiveReadinessAssessment,
    LiveReadinessError,
    LiveReadinessInputs,
    LiveRiskDecision,
    LiveRiskPolicy,
    PermissionClass,
    Provider,
    ProviderAccountStatus,
    ProviderCapabilitySnapshot,
    ReadinessExport,
    SecretBackend,
    build_live_readiness_operating_tasks,
    evaluate_live_risk,
    redact_payload,
)


class LiveReadinessRepository(Protocol):
    def save_credential_reference(self, value: CredentialReference) -> CredentialReference: ...
    def credential_reference(self, reference_id: str) -> CredentialReference | None: ...
    def credential_references(self) -> tuple[CredentialReference, ...]: ...
    def save_capability(self, value: ProviderCapabilitySnapshot) -> ProviderCapabilitySnapshot: ...
    def capability(self, snapshot_id: str) -> ProviderCapabilitySnapshot | None: ...
    def latest_capability(
        self, provider: Provider, account_scope_id: str
    ) -> ProviderCapabilitySnapshot | None: ...
    def save_risk_policy(self, value: LiveRiskPolicy) -> LiveRiskPolicy: ...
    def risk_policy(self, policy_id: str) -> LiveRiskPolicy | None: ...
    def save_risk_decision(self, value: LiveRiskDecision) -> LiveRiskDecision: ...
    def save_control_event(self, value: ExecutionControlEvent) -> ExecutionControlEvent: ...
    def control_events(self, control_id: str) -> tuple[ExecutionControlEvent, ...]: ...
    def save_backup_readiness(self, value: BackupReadinessRecord) -> BackupReadinessRecord: ...
    def backup_readiness(self, record_id: str) -> BackupReadinessRecord | None: ...
    def latest_backup_readiness(self) -> BackupReadinessRecord | None: ...
    def save_assessment(self, value: LiveReadinessAssessment) -> LiveReadinessAssessment: ...
    def assessment(self, assessment_id: str) -> LiveReadinessAssessment | None: ...
    def assessments(self) -> tuple[LiveReadinessAssessment, ...]: ...
    def save_export(self, value: ReadinessExport) -> ReadinessExport: ...
    def export(self, export_id: str) -> ReadinessExport | None: ...


class ReadOnlyCapabilityInspector(Protocol):
    @property
    def provider(self) -> Provider: ...

    def inspect(
        self,
        *,
        snapshot_id: str,
        account_scope_id: str,
        account_environment: AccountEnvironment,
        credential_reference: CredentialReference | None,
        inspected_at: datetime,
        request_limit: int,
    ) -> ProviderCapabilitySnapshot: ...


class CredentialsAbsentCapabilityInspector:
    """Safe default: no resolver, credential read, or provider request."""

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    @property
    def provider(self) -> Provider:
        return self._provider

    def inspect(
        self,
        *,
        snapshot_id: str,
        account_scope_id: str,
        account_environment: AccountEnvironment,
        credential_reference: CredentialReference | None,
        inspected_at: datetime,
        request_limit: int,
    ) -> ProviderCapabilitySnapshot:
        if request_limit < 0:
            raise LiveReadinessError("PROVIDER_INSPECTION_REQUEST_LIMIT_INVALID")
        if credential_reference is not None:
            return ProviderCapabilitySnapshot.create(
                snapshot_id=snapshot_id,
                version=1,
                provider=self.provider,
                account_scope_id=account_scope_id,
                credential_reference_id=credential_reference.reference_id,
                account_environment=account_environment,
                authenticated_read_status=AuthenticatedReadStatus.NOT_ATTEMPTED,
                permissions=(),
                account_status=ProviderAccountStatus.UNAVAILABLE,
                observed_at=inspected_at,
                ingested_at=inspected_at,
                evidence_class="READ_ONLY_INSPECTION_UNAVAILABLE",
                limitations=(
                    "PRIVATE_PROVIDER_ADAPTER_NOT_CONFIGURED",
                    "ZERO_PROVIDER_REQUESTS",
                ),
            )
        return ProviderCapabilitySnapshot.credentials_absent(
            snapshot_id=snapshot_id,
            provider=self.provider,
            account_scope_id=account_scope_id,
            account_environment=account_environment,
            observed_at=inspected_at,
        )


class RegisterCredentialReference:
    def __init__(self, repository: LiveReadinessRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        reference_id: str,
        version: int,
        provider: Provider,
        account_environment: AccountEnvironment,
        account_purpose: str,
        required_permissions: Sequence[PermissionClass],
        secret_backend: SecretBackend,
        backend_reference_id: str,
        actor: str,
        reason: str,
        confirmation: str,
        idempotency_key: str,
        created_at: datetime,
        previous_reference_id: str | None = None,
    ) -> CredentialReference:
        expected = f"REGISTER_CREDENTIAL_REFERENCE:{reference_id}:{version}"
        if (
            confirmation != expected
            or not actor.strip()
            or not reason.strip()
            or not idempotency_key.strip()
        ):
            raise LiveReadinessError("CREDENTIAL_REFERENCE_EXACT_CONFIRMATION_REQUIRED")
        existing = self._repository.credential_reference(reference_id)
        value = CredentialReference.create(
            reference_id=reference_id,
            version=version,
            provider=provider,
            account_environment=account_environment,
            account_purpose=account_purpose,
            required_permissions=required_permissions,
            secret_backend=secret_backend,
            backend_reference_id=backend_reference_id,
            created_at=created_at,
            previous_reference_id=previous_reference_id,
            warnings=(
                "REFERENCE_ONLY_SECRET_VALUES_NEVER_RETAINED",
                f"REGISTERED_BY:{actor}",
                f"REASON:{reason}",
                f"IDEMPOTENCY:{idempotency_key}",
            ),
        )
        if existing is not None and existing.canonical_hash != value.canonical_hash:
            raise LiveReadinessError("CREDENTIAL_REFERENCE_ID_CONFLICT")
        return self._repository.save_credential_reference(value)


class InspectProviderCapability:
    def __init__(
        self, repository: LiveReadinessRepository, inspector: ReadOnlyCapabilityInspector
    ) -> None:
        self._repository = repository
        self._inspector = inspector

    def execute(
        self,
        *,
        snapshot_id: str,
        account_scope_id: str,
        account_environment: AccountEnvironment,
        credential_reference_id: str | None,
        inspected_at: datetime,
        request_limit: int,
        actor: str,
        reason: str,
        confirmation: str,
        idempotency_key: str,
    ) -> ProviderCapabilitySnapshot:
        expected = f"INSPECT_PROVIDER_CAPABILITY_READ_ONLY:{snapshot_id}"
        if (
            confirmation != expected
            or not actor.strip()
            or not reason.strip()
            or not idempotency_key.strip()
        ):
            raise LiveReadinessError("PROVIDER_CAPABILITY_EXACT_CONFIRMATION_REQUIRED")
        reference = (
            None
            if credential_reference_id is None
            else self._repository.credential_reference(credential_reference_id)
        )
        if credential_reference_id is not None and reference is None:
            raise LiveReadinessError("PROVIDER_CREDENTIAL_REFERENCE_NOT_FOUND")
        if reference is not None and reference.provider is not self._inspector.provider:
            raise LiveReadinessError("PROVIDER_CREDENTIAL_REFERENCE_PROVIDER_MISMATCH")
        value = self._inspector.inspect(
            snapshot_id=snapshot_id,
            account_scope_id=account_scope_id,
            account_environment=account_environment,
            credential_reference=reference,
            inspected_at=inspected_at,
            request_limit=request_limit,
        )
        if (
            value.provider_write_capability != "NONE"
            or value.provider_request_count > request_limit
        ):
            raise LiveReadinessError("PROVIDER_CAPABILITY_READ_ONLY_BOUNDARY_VIOLATED")
        return self._repository.save_capability(value)


class SetExecutionControl:
    _ALLOWED: ClassVar[frozenset[ExecutionControlState]] = frozenset(
        {
            ExecutionControlState.DISABLED,
            ExecutionControlState.PAPER_ONLY,
            ExecutionControlState.LIVE_BLOCKED,
            ExecutionControlState.EMERGENCY_STOP,
        }
    )

    def __init__(self, repository: LiveReadinessRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        event_id: str,
        control_id: str,
        state: ExecutionControlState,
        actor: str,
        reason: str,
        confirmation: str,
        idempotency_key: str,
        effective_at: datetime,
        expires_at: datetime | None = None,
    ) -> ExecutionControlEvent:
        if state not in self._ALLOWED:
            raise LiveReadinessError("EXECUTION_CONTROL_STATE_UNAVAILABLE")
        retained = self._repository.control_events(control_id)
        previous = None if not retained else retained[-1].state
        expected = (
            f"CLEAR_EMERGENCY_STOP:{control_id}:{state.value}"
            if previous is ExecutionControlState.EMERGENCY_STOP
            and state is not ExecutionControlState.EMERGENCY_STOP
            else f"SET_EXECUTION_CONTROL:{control_id}:{state.value}"
        )
        if (
            confirmation != expected
            or not actor.strip()
            or not reason.strip()
            or not idempotency_key.strip()
        ):
            raise LiveReadinessError("EXECUTION_CONTROL_EXACT_CONFIRMATION_REQUIRED")
        if (
            previous is ExecutionControlState.EMERGENCY_STOP
            and state is not ExecutionControlState.EMERGENCY_STOP
            and state
            not in {
                ExecutionControlState.DISABLED,
                ExecutionControlState.PAPER_ONLY,
                ExecutionControlState.LIVE_BLOCKED,
            }
        ):
            raise LiveReadinessError("EMERGENCY_STOP_CLEAR_TARGET_INVALID")
        event = ExecutionControlEvent.create(
            event_id=event_id,
            control_id=control_id,
            ordinal=len(retained) + 1,
            state=state,
            actor=actor,
            reason=reason,
            effective_at=effective_at,
            expires_at=expires_at,
            previous_state=previous,
            idempotency_key=idempotency_key,
        )
        return self._repository.save_control_event(event)


class AuditBackupReadiness:
    def __init__(self, repository: LiveReadinessRepository) -> None:
        self._repository = repository

    def execute(self, value: BackupReadinessRecord) -> BackupReadinessRecord:
        if value.secret_values_included:
            raise LiveReadinessError("BACKUP_MUST_NOT_INCLUDE_SECRET_VALUES")
        return self._repository.save_backup_readiness(value)


class MaterializeLiveReadiness:
    def __init__(self, repository: LiveReadinessRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        assessment_id: str,
        decision_id: str,
        policy: LiveRiskPolicy,
        inputs: LiveReadinessInputs,
        actor: str,
    ) -> LiveReadinessAssessment:
        self._repository.save_risk_policy(policy)
        decision = evaluate_live_risk(decision_id=decision_id, policy=policy, inputs=inputs)
        self._repository.save_risk_decision(decision)
        return self._repository.save_assessment(
            LiveReadinessAssessment.create(
                assessment_id=assessment_id,
                policy=policy,
                inputs=inputs,
                decision=decision,
                actor=actor,
            )
        )


class ExportLiveReadinessEvidence:
    def __init__(self, repository: LiveReadinessRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        export_id: str,
        assessment_id: str,
        categories: Sequence[tuple[str, int]],
        evidence_ids: Sequence[str],
        evidence_hashes: Sequence[str],
        paper_records: int,
        generated_at: datetime,
    ) -> ReadinessExport:
        assessment = self._repository.assessment(assessment_id)
        if assessment is None:
            raise LiveReadinessError("LIVE_READINESS_ASSESSMENT_NOT_FOUND")
        return self._repository.save_export(
            ReadinessExport.create(
                export_id=export_id,
                assessment=assessment,
                categories=categories,
                evidence_ids=evidence_ids,
                evidence_hashes=evidence_hashes,
                paper_records=paper_records,
                generated_at=generated_at,
            )
        )


def live_readiness_workspace(repository: LiveReadinessRepository) -> dict[str, object]:
    controls = tuple(
        event
        for control_id in ("apex-global-execution-control-v1",)
        for event in repository.control_events(control_id)
    )
    assessments = repository.assessments()
    references = repository.credential_references()
    capabilities = tuple(
        value
        for provider in Provider
        if (value := repository.latest_capability(provider, f"{provider.value.lower()}-default"))
        is not None
    )
    policy = repository.risk_policy("LIVE_RISK_POLICY_V1")
    backup = repository.latest_backup_readiness()
    assessment = None if not assessments else assessments[-1]
    control = None if not controls else controls[-1]
    timestamps = (
        *(item.created_at for item in references),
        *(item.observed_at for item in capabilities),
        *((policy.created_at,) if policy is not None else ()),
        *((backup.assessed_at,) if backup is not None else ()),
        *((assessment.assessed_at,) if assessment is not None else ()),
        *((control.effective_at,) if control is not None else ()),
    )
    as_of = max(timestamps) if timestamps else datetime(1970, 1, 1, tzinfo=UTC)
    safe_references = tuple(
        {key: item for key, item in asdict(reference).items() if key != "backend_reference_id"}
        for reference in references
    )
    return {
        "credential_references": safe_references,
        "capabilities": capabilities,
        "risk_policy": policy,
        "execution_control_events": controls,
        "current_execution_control": control,
        "backup_readiness": backup,
        "assessments": assessments,
        "current_assessment": assessment,
        "operating_cycle_tasks": build_live_readiness_operating_tasks(
            credential_references=references,
            capabilities=capabilities,
            control=control,
            policy=policy,
            assessment=assessment,
            backup=backup,
            as_of=as_of,
        ),
        "safe_controls": (
            "REGISTER_CREDENTIAL_REFERENCE",
            "INSPECT_PROVIDER_CAPABILITY_READ_ONLY",
            "REQUEST_OR_INSPECT_COINBASE_READ_ONLY_APPROVAL",
            "RUN_APPROVED_COINBASE_READ_ONLY_INSPECTION",
            "REQUEST_OR_INSPECT_KALSHI_READ_ONLY_APPROVAL",
            "RUN_APPROVED_KALSHI_READ_ONLY_INSPECTION",
            "CONSTRUCT_EXACT_LIVE_INTENT_FROM_NONZERO_TRIAL_PROPOSAL",
            "RUN_INTERNAL_LIVE_PREFLIGHT",
            "REQUEST_OR_INSPECT_EXACT_PROVIDER_PREVIEW_APPROVAL",
            "RUN_APPROVED_COINBASE_PROVIDER_PREVIEW",
            "EXPIRE_OR_INVALIDATE_LIVE_INTENT",
            "SET_PAPER_ONLY",
            "SET_LIVE_BLOCKED",
            "TRIGGER_EMERGENCY_STOP",
            "CLEAR_EMERGENCY_STOP_EXACT_CONFIRMATION",
            "MATERIALIZE_READINESS",
            "EXPORT_REDACTED_EVIDENCE",
        ),
        "prohibited_controls": (
            "LIVE_PILOT_ARMED",
            "UNAPPROVED_OR_OUT_OF_SCOPE_PROVIDER_PREVIEW",
            "GENUINE_ORDER_SUBMISSION",
            "GENUINE_CANCELLATION",
            "TRANSFER",
            "WITHDRAWAL",
        ),
        "warning": "LIVE READINESS IS EVIDENCE ONLY — NO LIVE AUTHORITY",
        "authority": "NONE",
        "provider_write_capability": "NONE",
        "live_authority": "NONE",
    }


def safe_json_payload(value: object, *, secret_values: Sequence[str] = ()) -> object:
    """Backend redaction used by CLI/API/export rendering."""

    if hasattr(value, "__dataclass_fields__"):
        value = asdict(cast(Any, value))
    return redact_payload(value, secret_values=secret_values)


class _CanonicalEvidence(Protocol):
    @property
    def canonical_hash(self) -> str: ...


_EvidenceT = TypeVar("_EvidenceT", bound=_CanonicalEvidence)


class InMemoryLiveReadinessRepository:
    def __init__(self) -> None:
        self.credentials: dict[str, CredentialReference] = {}
        self.capabilities: dict[str, ProviderCapabilitySnapshot] = {}
        self.policies: dict[str, LiveRiskPolicy] = {}
        self.decisions: dict[str, LiveRiskDecision] = {}
        self.controls: dict[str, ExecutionControlEvent] = {}
        self.backups: dict[str, BackupReadinessRecord] = {}
        self.readiness: dict[str, LiveReadinessAssessment] = {}
        self.exports: dict[str, ReadinessExport] = {}

    @staticmethod
    def _save(
        store: dict[str, _EvidenceT],
        identity: str,
        value: _EvidenceT,
        canonical_hash: str,
    ) -> _EvidenceT:
        existing = store.get(identity)
        if existing is not None and existing.canonical_hash != canonical_hash:
            raise LiveReadinessError("LIVE_READINESS_IDEMPOTENCY_CONFLICT")
        store[identity] = existing or value
        return store[identity]

    def save_credential_reference(self, value: CredentialReference) -> CredentialReference:
        return self._save(self.credentials, value.reference_id, value, value.canonical_hash)

    def credential_reference(self, reference_id: str) -> CredentialReference | None:
        return self.credentials.get(reference_id)

    def credential_references(self) -> tuple[CredentialReference, ...]:
        return tuple(
            sorted(
                self.credentials.values(), key=lambda item: (item.provider.value, item.reference_id)
            )
        )

    def save_capability(self, value: ProviderCapabilitySnapshot) -> ProviderCapabilitySnapshot:
        return self._save(self.capabilities, value.snapshot_id, value, value.canonical_hash)

    def capability(self, snapshot_id: str) -> ProviderCapabilitySnapshot | None:
        return self.capabilities.get(snapshot_id)

    def latest_capability(
        self, provider: Provider, account_scope_id: str
    ) -> ProviderCapabilitySnapshot | None:
        values = [
            item
            for item in self.capabilities.values()
            if item.provider is provider and item.account_scope_id == account_scope_id
        ]
        return (
            None
            if not values
            else max(values, key=lambda item: (item.observed_at, item.snapshot_id))
        )

    def save_risk_policy(self, value: LiveRiskPolicy) -> LiveRiskPolicy:
        return self._save(self.policies, value.policy_id, value, value.canonical_hash)

    def risk_policy(self, policy_id: str) -> LiveRiskPolicy | None:
        return self.policies.get(policy_id)

    def save_risk_decision(self, value: LiveRiskDecision) -> LiveRiskDecision:
        return self._save(self.decisions, value.decision_id, value, value.canonical_hash)

    def save_control_event(self, value: ExecutionControlEvent) -> ExecutionControlEvent:
        retained = self.control_events(value.control_id)
        if retained and (
            value.ordinal != retained[-1].ordinal + 1
            or value.previous_state is not retained[-1].state
        ):
            raise LiveReadinessError("EXECUTION_CONTROL_TRANSITION_CONFLICT")
        return self._save(self.controls, value.event_id, value, value.canonical_hash)

    def control_events(self, control_id: str) -> tuple[ExecutionControlEvent, ...]:
        return tuple(
            sorted(
                (item for item in self.controls.values() if item.control_id == control_id),
                key=lambda item: item.ordinal,
            )
        )

    def save_backup_readiness(self, value: BackupReadinessRecord) -> BackupReadinessRecord:
        return self._save(self.backups, value.record_id, value, value.canonical_hash)

    def backup_readiness(self, record_id: str) -> BackupReadinessRecord | None:
        return self.backups.get(record_id)

    def latest_backup_readiness(self) -> BackupReadinessRecord | None:
        return (
            None
            if not self.backups
            else max(self.backups.values(), key=lambda item: (item.assessed_at, item.record_id))
        )

    def save_assessment(self, value: LiveReadinessAssessment) -> LiveReadinessAssessment:
        return self._save(self.readiness, value.assessment_id, value, value.canonical_hash)

    def assessment(self, assessment_id: str) -> LiveReadinessAssessment | None:
        return self.readiness.get(assessment_id)

    def assessments(self) -> tuple[LiveReadinessAssessment, ...]:
        return tuple(
            sorted(self.readiness.values(), key=lambda item: (item.assessed_at, item.assessment_id))
        )

    def save_export(self, value: ReadinessExport) -> ReadinessExport:
        return self._save(self.exports, value.export_id, value, value.canonical_hash)

    def export(self, export_id: str) -> ReadinessExport | None:
        return self.exports.get(export_id)
