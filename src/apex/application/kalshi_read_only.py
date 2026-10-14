"""Approval-gated Kalshi private read-only capability orchestration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, cast

from apex.application.live_readiness import LiveReadinessRepository
from apex.domain.kalshi_read_only import (
    KALSHI_PRIVATE_HOST,
    KalshiBalanceEvidence,
    KalshiCapabilityCapture,
    KalshiCapabilityClass,
    KalshiCapabilityRefresh,
    KalshiEndpointClass,
    KalshiInspectionAttempt,
    KalshiInspectionStatus,
    KalshiReadApproval,
    KalshiReadOnlyError,
    KalshiRefreshDisposition,
    KalshiRequestEvidence,
    validate_kalshi_private_read_request,
)
from apex.domain.live_readiness import (
    CredentialReference,
    ExecutionControlState,
    PermissionClass,
    Provider,
    ProviderAccountStatus,
    assert_secret_free,
)

_APPROVED_PATHS = {
    KalshiEndpointClass.ACCOUNT_IDENTITY_READ: "/trade-api/v2/communications/id",
    KalshiEndpointClass.BALANCE_READ: ("/trade-api/v2/portfolio/balance?subaccount=0"),
    KalshiEndpointClass.PERMISSIONS_READ: "/trade-api/v2/api_keys",
    KalshiEndpointClass.ACCOUNT_TIER_READ: "/trade-api/v2/account/limits",
}


class KalshiReadOnlyRepository(Protocol):
    def save_approval(self, value: KalshiReadApproval) -> KalshiReadApproval: ...
    def approval(self, approval_id: str) -> KalshiReadApproval | None: ...
    def approvals(self) -> tuple[KalshiReadApproval, ...]: ...
    def save_attempt(self, value: KalshiInspectionAttempt) -> KalshiInspectionAttempt: ...
    def attempt(self, attempt_id: str) -> KalshiInspectionAttempt | None: ...
    def attempts(self) -> tuple[KalshiInspectionAttempt, ...]: ...
    def save_capture(self, value: KalshiCapabilityCapture) -> KalshiCapabilityCapture: ...
    def capture(self, capture_id: str) -> KalshiCapabilityCapture | None: ...
    def captures(self) -> tuple[KalshiCapabilityCapture, ...]: ...
    def save_refresh(self, value: KalshiCapabilityRefresh) -> KalshiCapabilityRefresh: ...
    def refreshes(self) -> tuple[KalshiCapabilityRefresh, ...]: ...


class KalshiCredentialMaterial:
    """Ephemeral authentication material with a deliberately redacted representation."""

    __slots__ = ("key_id", "private_key")

    def __init__(self, *, key_id: str, private_key: str) -> None:
        if not key_id or not private_key:
            raise KalshiReadOnlyError("KALSHI_CREDENTIAL_MATERIAL_INVALID")
        self.key_id = key_id
        self.private_key = private_key

    def __repr__(self) -> str:
        return "KalshiCredentialMaterial([REDACTED])"


class KalshiCredentialResolver(Protocol):
    def resolve(self, reference: CredentialReference) -> KalshiCredentialMaterial: ...


@dataclass(frozen=True, slots=True)
class KalshiPrivateResponse:
    status: int
    payload: bytes
    completed_at: datetime


class KalshiPrivateReadTransport(Protocol):
    def get(
        self,
        *,
        credential: KalshiCredentialMaterial,
        endpoint_class: KalshiEndpointClass,
        path: str,
        timeout_seconds: int,
        requested_at: datetime,
    ) -> KalshiPrivateResponse: ...


@dataclass(frozen=True, slots=True)
class KalshiInspectionResult:
    attempt: KalshiInspectionAttempt
    capture: KalshiCapabilityCapture | None
    replayed: bool
    readiness_snapshot_id: str | None


class ApproveKalshiReadOnlyInspection:
    def __init__(
        self,
        repository: KalshiReadOnlyRepository,
        live_repository: LiveReadinessRepository,
    ) -> None:
        self._repository = repository
        self._live_repository = live_repository

    def execute(
        self,
        *,
        approval_id: str,
        credential_reference_id: str,
        account_purpose: str,
        allowed_endpoint_classes: Sequence[KalshiEndpointClass],
        maximum_request_count: int,
        timeout_seconds: int,
        approved_at: datetime,
        expires_at: datetime,
        actor: str,
        reason: str,
        idempotency_key: str,
        confirmation: str,
    ) -> KalshiReadApproval:
        expected = f"APPROVE_KALSHI_READ_ONLY:{approval_id}:{credential_reference_id}"
        if confirmation != expected:
            raise KalshiReadOnlyError("KALSHI_READ_APPROVAL_EXACT_CONFIRMATION_REQUIRED")
        reference = self._live_repository.credential_reference(credential_reference_id)
        if reference is None:
            raise KalshiReadOnlyError("KALSHI_CREDENTIAL_REFERENCE_NOT_FOUND")
        if (
            reference.provider is not Provider.KALSHI
            or reference.account_purpose != account_purpose
            or set(reference.required_permissions) != {PermissionClass.PRIVATE_VIEW}
        ):
            raise KalshiReadOnlyError("KALSHI_CREDENTIAL_REFERENCE_SCOPE_INVALID")
        value = KalshiReadApproval.create(
            approval_id=approval_id,
            credential_reference_id=credential_reference_id,
            account_purpose=account_purpose,
            allowed_endpoint_classes=allowed_endpoint_classes,
            maximum_request_count=maximum_request_count,
            timeout_seconds=timeout_seconds,
            approved_at=approved_at,
            expires_at=expires_at,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
        )
        existing = self._repository.approval(approval_id)
        if existing is not None and existing.canonical_hash != value.canonical_hash:
            raise KalshiReadOnlyError("KALSHI_READ_APPROVAL_ID_CONFLICT")
        return self._repository.save_approval(value)


def _object(value: object, reason: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise KalshiReadOnlyError(reason)
    return cast(dict[str, object], value)


def _decode(payload: bytes, reason: str) -> dict[str, object]:
    try:
        return _object(json.loads(payload), reason)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KalshiReadOnlyError(reason) from error


def _safe_response(
    payload: bytes,
    *,
    endpoint_class: KalshiEndpointClass,
    credential: KalshiCredentialMaterial,
) -> bytes:
    """Return the only response representation eligible for hashing or projection."""

    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise KalshiReadOnlyError("KALSHI_RESPONSE_SECRET_SAFETY_UNPROVEN") from error
    rendered = payload.decode("utf-8", errors="ignore")
    lowered = rendered.lower()
    if credential.private_key in rendered or any(
        fragment in lowered
        for fragment in ('"private_key"', '"secret"', '"token"', '"signature"', "bearer ")
    ):
        raise KalshiReadOnlyError("KALSHI_RESPONSE_SECRET_SAFETY_UNPROVEN")
    if endpoint_class is not KalshiEndpointClass.PERMISSIONS_READ:
        try:
            assert_secret_free(value)
        except ValueError as error:
            raise KalshiReadOnlyError("KALSHI_RESPONSE_SECRET_SAFETY_UNPROVEN") from error
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    root = _object(value, "KALSHI_PERMISSION_RESPONSE_INVALID")
    records = root.get("api_keys")
    if not isinstance(records, list):
        raise KalshiReadOnlyError("KALSHI_PERMISSION_RESPONSE_INVALID")
    matching: list[dict[str, object]] = []
    for raw in records:
        item = _object(raw, "KALSHI_PERMISSION_RESPONSE_INVALID")
        if item.get("api_key_id") == credential.key_id:
            matching.append(item)
    if len(matching) != 1:
        raise KalshiReadOnlyError("KALSHI_PERMISSION_EVIDENCE_AMBIGUOUS")
    scopes = matching[0]["scopes"]
    sanitized_scopes: object = scopes
    if isinstance(scopes, list) and all(isinstance(item, str) and item for item in scopes):
        sanitized_scopes = sorted(set(cast(list[str], scopes)))
    return json.dumps(
        {"current_credential_scopes": sanitized_scopes},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _string(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value:
        raise KalshiReadOnlyError(reason)
    return value


def _account_evidence(
    identity_payload: bytes,
    balance_payload: bytes,
    *,
    source_hash: str,
    observed_at: datetime,
) -> tuple[str, ProviderAccountStatus, str, tuple[KalshiBalanceEvidence, ...], tuple[str, ...]]:
    identity = _decode(identity_payload, "KALSHI_ACCOUNT_IDENTITY_RESPONSE_INVALID")
    identifier = _string(identity.get("communications_id"), "KALSHI_ACCOUNT_IDENTITY_INVALID")
    status_raw = identity.get("account_status")
    if status_raw == "ACTIVE":
        provider_status = ProviderAccountStatus.ACTIVE
    elif status_raw == "RESTRICTED":
        provider_status = ProviderAccountStatus.RESTRICTED
    elif status_raw is None:
        provider_status = ProviderAccountStatus.UNAVAILABLE
    else:
        raise KalshiReadOnlyError("KALSHI_ACCOUNT_STATUS_AMBIGUOUS")
    restrictions: set[str] = set()
    if provider_status is ProviderAccountStatus.RESTRICTED:
        restrictions.add("ACCOUNT_RESTRICTED")
    balance = _decode(balance_payload, "KALSHI_BALANCE_RESPONSE_INVALID")
    balance_cents = balance.get("balance")
    portfolio_cents = balance.get("portfolio_value")
    if not isinstance(balance_cents, int) or isinstance(balance_cents, bool) or balance_cents < 0:
        raise KalshiReadOnlyError("KALSHI_BALANCE_RESPONSE_INVALID")
    if portfolio_cents is not None and (
        not isinstance(portfolio_cents, int)
        or isinstance(portfolio_cents, bool)
        or portfolio_cents < 0
    ):
        raise KalshiReadOnlyError("KALSHI_BALANCE_RESPONSE_INVALID")
    balances = (
        KalshiBalanceEvidence.create(
            currency="USD",
            available_amount=Decimal(balance_cents) / Decimal(100),
            portfolio_value=None
            if portfolio_cents is None
            else Decimal(portfolio_cents) / Decimal(100),
            observed_at=observed_at,
            source_hash=source_hash,
        ),
    )
    fingerprint = "sha256:" + hashlib.sha256(identifier.encode()).hexdigest()[:24]
    return (
        fingerprint,
        provider_status,
        "KALSHI_ACCOUNT",
        balances,
        tuple(sorted(restrictions)),
    )


def _permission_evidence(payload: bytes) -> tuple[tuple[str, str], ...]:
    root = _decode(payload, "KALSHI_PERMISSION_RESPONSE_INVALID")
    scopes = root.get("current_credential_scopes")
    if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
        raise KalshiReadOnlyError("KALSHI_PERMISSION_EVIDENCE_AMBIGUOUS")
    normalized = {str(item).lower() for item in scopes}
    known = {"read", "write", "transfer", "withdrawal", "order_preview"}
    if "read" not in normalized or normalized - known:
        raise KalshiReadOnlyError("KALSHI_PERMISSION_EVIDENCE_AMBIGUOUS")
    return tuple(
        (name, "EXPLICIT_TRUE" if scope in normalized else "EXPLICIT_FALSE")
        for name, scope in (
            ("private_view", "read"),
            ("trade", "write"),
            ("transfer", "transfer"),
            ("withdrawal", "withdrawal"),
            ("order_preview", "order_preview"),
        )
    )


def _capture_from_responses(
    *,
    capture_id: str,
    approval: KalshiReadApproval,
    account_purpose: str,
    responses: Mapping[KalshiEndpointClass, bytes],
    request_evidence: Sequence[KalshiRequestEvidence],
    inspected_at: datetime,
) -> KalshiCapabilityCapture:
    balance_request = next(
        item for item in request_evidence if item.endpoint_class is KalshiEndpointClass.BALANCE_READ
    )
    fingerprint, account_status, account_type, balances, account_restrictions = _account_evidence(
        responses[KalshiEndpointClass.ACCOUNT_IDENTITY_READ],
        responses[KalshiEndpointClass.BALANCE_READ],
        source_hash=balance_request.response_hash,
        observed_at=inspected_at,
    )
    permission_evidence = _permission_evidence(responses[KalshiEndpointClass.PERMISSIONS_READ])
    account_tier: str | None = None
    tier_limitations: tuple[str, ...] = ("ACCOUNT_TIER_EVIDENCE_NOT_REQUESTED",)
    if KalshiEndpointClass.ACCOUNT_TIER_READ in responses:
        tier_root = _decode(
            responses[KalshiEndpointClass.ACCOUNT_TIER_READ],
            "KALSHI_ACCOUNT_TIER_RESPONSE_INVALID",
        )
        tier = tier_root.get("usage_tier")
        if tier is not None and not isinstance(tier, str):
            raise KalshiReadOnlyError("KALSHI_ACCOUNT_TIER_RESPONSE_INVALID")
        account_tier = tier
        tier_limitations = () if tier is not None else ("ACCOUNT_TIER_UNAVAILABLE",)
    return KalshiCapabilityCapture.create(
        capture_id=capture_id,
        approval=approval,
        account_purpose=account_purpose,
        redacted_account_fingerprint=fingerprint,
        account_status=account_status,
        account_type=account_type,
        permission_evidence=permission_evidence,
        balances=balances,
        maker_fee_rate=None,
        taker_fee_rate=None,
        account_tier=account_tier,
        fee_limitations=("PUBLIC_SERIES_FEE_EVIDENCE_REMAINS_AUTHORITATIVE", *tier_limitations),
        account_restrictions=account_restrictions,
        requests=request_evidence,
        observed_at=inspected_at,
        fresh_until=inspected_at + timedelta(minutes=15),
        redaction_proof=(
            "AUTHORIZATION_HEADER_NOT_RETAINED",
            "SECRET_VALUES_NOT_RETAINED",
            "RAW_PAYLOAD_NOT_RETAINED",
            "ACCOUNT_IDENTITY_FINGERPRINTED",
            "API_KEY_IDENTITY_NOT_RETAINED_OR_HASHED",
        ),
        limitations=(
            "BALANCES_ARE_READ_ONLY_EVIDENCE_NOT_APEX_CAPITAL",
            "PUBLIC_PRODUCT_EVIDENCE_REMAINS_AUTHORITATIVE",
            "ACCOUNT_USAGE_TIER_IS_NOT_A_FEE_SCHEDULE",
            *account_restrictions,
        ),
    )


class InspectKalshiReadOnlyCapability:
    def __init__(
        self,
        repository: KalshiReadOnlyRepository,
        live_repository: LiveReadinessRepository,
        resolver: KalshiCredentialResolver,
        transport: KalshiPrivateReadTransport,
    ) -> None:
        self._repository = repository
        self._live_repository = live_repository
        self._resolver = resolver
        self._transport = transport

    def _blocked(
        self,
        *,
        attempt_id: str,
        approval_id: str | None,
        credential_reference_id: str | None,
        reason: str,
        at: datetime,
    ) -> KalshiInspectionResult:
        attempt = self._repository.save_attempt(
            KalshiInspectionAttempt.create(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                status=KalshiInspectionStatus.BLOCKED,
                blocked_reason=reason,
                capture_id=None,
                request_count=0,
                warnings=(reason,),
                attempted_at=at,
            )
        )
        return KalshiInspectionResult(attempt, None, False, None)

    def execute(
        self,
        *,
        attempt_id: str,
        capture_id: str,
        readiness_snapshot_id: str,
        approval_id: str | None,
        credential_reference_id: str | None,
        account_purpose: str,
        inspected_at: datetime,
        execution_control: ExecutionControlState,
        due: bool = True,
    ) -> KalshiInspectionResult:
        existing = self._repository.attempt(attempt_id)
        if existing is not None:
            capture = (
                None
                if existing.capture_id is None
                else self._repository.capture(existing.capture_id)
            )
            return KalshiInspectionResult(
                existing, capture, True, None if capture is None else readiness_snapshot_id
            )
        if credential_reference_id is None:
            return self._blocked(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=None,
                reason="PRIVATE_CREDENTIALS_ABSENT",
                at=inspected_at,
            )
        reference = self._live_repository.credential_reference(credential_reference_id)
        if reference is None:
            return self._blocked(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                reason="CREDENTIAL_REFERENCE_INVALID",
                at=inspected_at,
            )
        if approval_id is None:
            return self._blocked(
                attempt_id=attempt_id,
                approval_id=None,
                credential_reference_id=credential_reference_id,
                reason="EXACT_READ_ONLY_APPROVAL_ABSENT",
                at=inspected_at,
            )
        approval = self._repository.approval(approval_id)
        if (
            approval is None
            or approval.credential_reference_id != credential_reference_id
            or approval.account_purpose != account_purpose
        ):
            return self._blocked(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                reason="EXACT_READ_ONLY_APPROVAL_INVALID",
                at=inspected_at,
            )
        if not approval.active_at(inspected_at):
            return self._blocked(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                reason="EXACT_READ_ONLY_APPROVAL_EXPIRED",
                at=inspected_at,
            )
        if not due:
            return self._blocked(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                reason="CAPABILITY_REFRESH_NOT_DUE",
                at=inspected_at,
            )
        if execution_control is ExecutionControlState.EMERGENCY_STOP:
            return self._blocked(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                reason="EMERGENCY_STOP_BLOCKS_PRIVATE_READ",
                at=inspected_at,
            )

        plan: list[tuple[KalshiEndpointClass, str]] = []
        endpoints = set(approval.allowed_endpoint_classes)
        for endpoint_class in KalshiEndpointClass:
            if endpoint_class in endpoints:
                plan.append(
                    (
                        endpoint_class,
                        _APPROVED_PATHS[endpoint_class],
                    )
                )
        if not plan or len(plan) > approval.maximum_request_count:
            return self._blocked(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                reason="APPROVED_REQUEST_BUDGET_INSUFFICIENT",
                at=inspected_at,
            )
        for endpoint_class, path in plan:
            validate_kalshi_private_read_request(
                method="GET", host=KALSHI_PRIVATE_HOST, path=path, endpoint_class=endpoint_class
            )
        try:
            credential = self._resolver.resolve(reference)
        except Exception:
            return self._blocked(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                reason="CREDENTIAL_REFERENCE_INVALID",
                at=inspected_at,
            )

        responses: dict[KalshiEndpointClass, bytes] = {}
        request_evidence: list[KalshiRequestEvidence] = []
        for ordinal, (endpoint_class, path) in enumerate(plan, 1):
            try:
                response = self._transport.get(
                    credential=credential,
                    endpoint_class=endpoint_class,
                    path=path,
                    timeout_seconds=approval.timeout_seconds,
                    requested_at=inspected_at,
                )
            except Exception:
                attempt = self._repository.save_attempt(
                    KalshiInspectionAttempt.create(
                        attempt_id=attempt_id,
                        approval_id=approval_id,
                        credential_reference_id=credential_reference_id,
                        status=KalshiInspectionStatus.PROVIDER_FAILED,
                        blocked_reason="PROVIDER_READ_FAILED_REDACTED",
                        capture_id=None,
                        request_count=len(request_evidence),
                        warnings=(
                            "PROVIDER_READ_FAILED_REDACTED",
                            "AUTHENTICATION_HEADERS_NOT_RETAINED",
                        ),
                        attempted_at=inspected_at,
                    )
                )
                return KalshiInspectionResult(attempt, None, False, None)
            if response.status in {401, 403}:
                safe_payload = json.dumps(
                    {"authentication_status": response.status},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                request_evidence.append(
                    KalshiRequestEvidence.create(
                        request_id=f"{attempt_id}-request-{ordinal}",
                        endpoint_class=endpoint_class,
                        path=path,
                        response_status=response.status,
                        response_hash=hashlib.sha256(safe_payload).hexdigest(),
                        requested_at=inspected_at,
                        completed_at=response.completed_at,
                        response_bytes=len(safe_payload),
                    )
                )
                attempt = self._repository.save_attempt(
                    KalshiInspectionAttempt.create(
                        attempt_id=attempt_id,
                        approval_id=approval_id,
                        credential_reference_id=credential_reference_id,
                        status=KalshiInspectionStatus.AUTHENTICATION_FAILED,
                        blocked_reason="AUTHENTICATION_FAILURE_REDACTED",
                        capture_id=None,
                        request_count=len(request_evidence),
                        warnings=(
                            "AUTHENTICATION_FAILURE_REDACTED",
                            "AUTHENTICATION_HEADERS_NOT_RETAINED",
                            "AUTHENTICATION_RESPONSE_PAYLOAD_NOT_RETAINED",
                        ),
                        attempted_at=inspected_at,
                    )
                )
                return KalshiInspectionResult(attempt, None, False, None)
            try:
                safe_payload = _safe_response(
                    response.payload,
                    endpoint_class=endpoint_class,
                    credential=credential,
                )
            except KalshiReadOnlyError:
                attempt = self._repository.save_attempt(
                    KalshiInspectionAttempt.create(
                        attempt_id=attempt_id,
                        approval_id=approval_id,
                        credential_reference_id=credential_reference_id,
                        status=KalshiInspectionStatus.PROVIDER_FAILED,
                        blocked_reason="PROVIDER_RESPONSE_SECRET_SAFETY_UNPROVEN",
                        capture_id=None,
                        request_count=ordinal,
                        warnings=("PROVIDER_RESPONSE_DISCARDED_WITHOUT_HASH_OR_PAYLOAD_RETENTION",),
                        attempted_at=inspected_at,
                    )
                )
                return KalshiInspectionResult(attempt, None, False, None)
            response_hash = hashlib.sha256(safe_payload).hexdigest()
            request_evidence.append(
                KalshiRequestEvidence.create(
                    request_id=f"{attempt_id}-request-{ordinal}",
                    endpoint_class=endpoint_class,
                    path=path,
                    response_status=response.status,
                    response_hash=response_hash,
                    requested_at=inspected_at,
                    completed_at=response.completed_at,
                    response_bytes=len(safe_payload),
                )
            )
            if response.status != 200:
                attempt = self._repository.save_attempt(
                    KalshiInspectionAttempt.create(
                        attempt_id=attempt_id,
                        approval_id=approval_id,
                        credential_reference_id=credential_reference_id,
                        status=KalshiInspectionStatus.PROVIDER_FAILED,
                        blocked_reason=f"PROVIDER_HTTP_{response.status}",
                        capture_id=None,
                        request_count=len(request_evidence),
                        warnings=("PROVIDER_RESPONSE_PAYLOAD_NOT_RETAINED",),
                        attempted_at=inspected_at,
                    )
                )
                return KalshiInspectionResult(attempt, None, False, None)
            responses[endpoint_class] = safe_payload

        required = {
            KalshiEndpointClass.ACCOUNT_IDENTITY_READ,
            KalshiEndpointClass.BALANCE_READ,
            KalshiEndpointClass.PERMISSIONS_READ,
        }
        if not required.issubset(responses):
            return self._blocked(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                reason="PRIVATE_VIEW_EVIDENCE_INSUFFICIENT",
                at=inspected_at,
            )
        try:
            capture = _capture_from_responses(
                capture_id=capture_id,
                approval=approval,
                account_purpose=account_purpose,
                responses=responses,
                request_evidence=request_evidence,
                inspected_at=inspected_at,
            )
        except (KalshiReadOnlyError, KeyError, StopIteration):
            attempt = self._repository.save_attempt(
                KalshiInspectionAttempt.create(
                    attempt_id=attempt_id,
                    approval_id=approval_id,
                    credential_reference_id=credential_reference_id,
                    status=KalshiInspectionStatus.PROVIDER_FAILED,
                    blocked_reason="CAPABILITY_EVIDENCE_INVALID_REDACTED",
                    capture_id=None,
                    request_count=len(request_evidence),
                    warnings=(
                        "AMBIGUOUS_OR_INVALID_PERMISSION_OR_ACCOUNT_EVIDENCE",
                        "PROVIDER_RESPONSE_PAYLOAD_NOT_RETAINED",
                    ),
                    attempted_at=inspected_at,
                )
            )
            return KalshiInspectionResult(attempt, None, False, None)
        self._repository.save_capture(capture)
        snapshot = capture.to_live_readiness_snapshot(
            snapshot_id=readiness_snapshot_id, ingested_at=inspected_at
        )
        self._live_repository.save_capability(snapshot)
        attempt = self._repository.save_attempt(
            KalshiInspectionAttempt.create(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                status=KalshiInspectionStatus.SUCCEEDED,
                blocked_reason=None,
                capture_id=capture.capture_id,
                request_count=capture.provider_request_count,
                warnings=capture.warnings,
                attempted_at=inspected_at,
            )
        )
        return KalshiInspectionResult(attempt, capture, False, snapshot.snapshot_id)


class RefreshKalshiReadOnlyCapability:
    def __init__(
        self, repository: KalshiReadOnlyRepository, inspector: InspectKalshiReadOnlyCapability
    ) -> None:
        self._repository = repository
        self._inspector = inspector

    def execute(
        self,
        *,
        refresh_id: str,
        attempt_id: str,
        capture_id: str,
        readiness_snapshot_id: str,
        approval_id: str | None,
        credential_reference_id: str | None,
        account_purpose: str,
        evaluated_at: datetime,
        due: bool,
        execution_control: ExecutionControlState,
    ) -> KalshiCapabilityRefresh:
        if not due:
            value = KalshiCapabilityRefresh.create(
                refresh_id=refresh_id,
                due=False,
                disposition=KalshiRefreshDisposition.NOT_DUE,
                reasons=("CAPABILITY_EVIDENCE_FRESH",),
                approval_id=approval_id,
                request_count=0,
                output_id=None,
                status="NOT_DUE",
                warnings=(),
                evaluated_at=evaluated_at,
            )
        else:
            result = self._inspector.execute(
                attempt_id=attempt_id,
                capture_id=capture_id,
                readiness_snapshot_id=readiness_snapshot_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                account_purpose=account_purpose,
                inspected_at=evaluated_at,
                execution_control=execution_control,
                due=True,
            )
            value = KalshiCapabilityRefresh.create(
                refresh_id=refresh_id,
                due=True,
                disposition=KalshiRefreshDisposition.COMPLETED
                if result.capture is not None
                else KalshiRefreshDisposition.BLOCKED,
                reasons=()
                if result.capture is not None
                else ((result.attempt.blocked_reason or result.attempt.status.value),),
                approval_id=approval_id,
                request_count=result.attempt.request_count,
                output_id=None if result.capture is None else result.capture.capture_id,
                status=result.attempt.status.value,
                warnings=result.attempt.warnings,
                evaluated_at=evaluated_at,
            )
        return self._repository.save_refresh(value)


def kalshi_read_only_attention(
    *,
    capture: KalshiCapabilityCapture | None,
    attempt: KalshiInspectionAttempt | None,
    as_of: datetime,
    previous_readiness: str | None = None,
    current_readiness: str | None = None,
) -> tuple[str, ...]:
    values: set[str] = set()
    if attempt is not None:
        if attempt.blocked_reason == "CREDENTIAL_REFERENCE_INVALID":
            values.add("INVALID_CREDENTIAL_REFERENCE")
        if attempt.status is KalshiInspectionStatus.AUTHENTICATION_FAILED:
            values.add("AUTHENTICATION_FAILURE")
    if capture is not None:
        classes = set(capture.capability_classes)
        if any(
            item.value.endswith("PERMISSION_AVAILABLE")
            for item in classes
            if item.value.startswith(("TRANSFER", "WITHDRAWAL"))
        ):
            values.add("DANGEROUS_TRANSFER_OR_WITHDRAWAL_CAPABILITY")
        if any(
            item
            in {
                KalshiCapabilityClass.LIVE_TRADE_PERMISSION_AVAILABLE,
                KalshiCapabilityClass.ORDER_PREVIEW_AVAILABLE,
            }
            for item in classes
        ):
            values.add("UNEXPECTED_PERMISSION_SCOPE")
        if capture.account_status is ProviderAccountStatus.RESTRICTED:
            values.add("ACCOUNT_RESTRICTION")
        moment = as_of.astimezone(UTC)
        if moment >= capture.fresh_until:
            values.add("STALE_CAPABILITY_EVIDENCE")
    if (
        previous_readiness is not None
        and current_readiness is not None
        and previous_readiness != "LIVE_NOT_READY"
        and current_readiness == "LIVE_NOT_READY"
    ):
        values.add("READINESS_REGRESSION")
    return tuple(sorted(values))


def kalshi_read_only_workspace(repository: KalshiReadOnlyRepository) -> dict[str, object]:
    approvals = repository.approvals()
    attempts = repository.attempts()
    captures = repository.captures()
    refreshes = repository.refreshes()
    return {
        "provider": "KALSHI",
        "endpoint_allowlist_policy": "KALSHI_PRIVATE_READ_ALLOWLIST_V1",
        "allowed_endpoint_classes": tuple(item.value for item in KalshiEndpointClass),
        "approvals": approvals,
        "attempts": attempts,
        "captures": captures,
        "refreshes": refreshes,
        "latest_approval": None if not approvals else approvals[-1],
        "latest_attempt": None if not attempts else attempts[-1],
        "latest_capture": None if not captures else captures[-1],
        "provider_request_count": sum(item.request_count for item in attempts),
        "provider_write_count": 0,
        "balances_are_capital": False,
        "preview_capability": "NONE",
        "trade_capability": "NONE",
        "transfer_capability": "NONE",
        "withdrawal_capability": "NONE",
        "authority": "NONE",
        "live_authority": "NONE",
    }


class InMemoryKalshiReadOnlyRepository:
    def __init__(self) -> None:
        self._approvals: dict[str, KalshiReadApproval] = {}
        self._attempts: dict[str, KalshiInspectionAttempt] = {}
        self._captures: dict[str, KalshiCapabilityCapture] = {}
        self._refreshes: dict[str, KalshiCapabilityRefresh] = {}

    @staticmethod
    def _save[T](store: dict[str, T], key: str, value: T) -> T:
        existing = store.get(key)
        if existing is not None and existing != value:
            raise KalshiReadOnlyError("KALSHI_READ_ONLY_ID_CONFLICT")
        store[key] = value
        return value

    def save_approval(self, value: KalshiReadApproval) -> KalshiReadApproval:
        return self._save(self._approvals, value.approval_id, value)

    def approval(self, approval_id: str) -> KalshiReadApproval | None:
        return self._approvals.get(approval_id)

    def approvals(self) -> tuple[KalshiReadApproval, ...]:
        return tuple(self._approvals.values())

    def save_attempt(self, value: KalshiInspectionAttempt) -> KalshiInspectionAttempt:
        return self._save(self._attempts, value.attempt_id, value)

    def attempt(self, attempt_id: str) -> KalshiInspectionAttempt | None:
        return self._attempts.get(attempt_id)

    def attempts(self) -> tuple[KalshiInspectionAttempt, ...]:
        return tuple(self._attempts.values())

    def save_capture(self, value: KalshiCapabilityCapture) -> KalshiCapabilityCapture:
        return self._save(self._captures, value.capture_id, value)

    def capture(self, capture_id: str) -> KalshiCapabilityCapture | None:
        return self._captures.get(capture_id)

    def captures(self) -> tuple[KalshiCapabilityCapture, ...]:
        return tuple(self._captures.values())

    def save_refresh(self, value: KalshiCapabilityRefresh) -> KalshiCapabilityRefresh:
        return self._save(self._refreshes, value.refresh_id, value)

    def refreshes(self) -> tuple[KalshiCapabilityRefresh, ...]:
        return tuple(self._refreshes.values())
