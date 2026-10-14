"""Approval-gated Coinbase private read-only capability orchestration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, cast

from apex.application.live_readiness import LiveReadinessRepository
from apex.domain.coinbase_read_only import (
    COINBASE_PRIVATE_HOST,
    CoinbaseBalanceEvidence,
    CoinbaseCapabilityCapture,
    CoinbaseCapabilityClass,
    CoinbaseCapabilityRefresh,
    CoinbaseEndpointClass,
    CoinbaseInspectionAttempt,
    CoinbaseInspectionStatus,
    CoinbaseProductConstraint,
    CoinbaseReadApproval,
    CoinbaseReadOnlyError,
    CoinbaseRefreshDisposition,
    CoinbaseRequestEvidence,
    validate_coinbase_private_read_request,
)
from apex.domain.live_readiness import (
    CredentialReference,
    ExecutionControlState,
    PermissionClass,
    Provider,
    ProviderAccountStatus,
    assert_secret_free,
)


class CoinbaseReadOnlyRepository(Protocol):
    def save_approval(self, value: CoinbaseReadApproval) -> CoinbaseReadApproval: ...
    def approval(self, approval_id: str) -> CoinbaseReadApproval | None: ...
    def approvals(self) -> tuple[CoinbaseReadApproval, ...]: ...
    def save_attempt(self, value: CoinbaseInspectionAttempt) -> CoinbaseInspectionAttempt: ...
    def attempt(self, attempt_id: str) -> CoinbaseInspectionAttempt | None: ...
    def attempts(self) -> tuple[CoinbaseInspectionAttempt, ...]: ...
    def save_capture(self, value: CoinbaseCapabilityCapture) -> CoinbaseCapabilityCapture: ...
    def capture(self, capture_id: str) -> CoinbaseCapabilityCapture | None: ...
    def captures(self) -> tuple[CoinbaseCapabilityCapture, ...]: ...
    def save_refresh(self, value: CoinbaseCapabilityRefresh) -> CoinbaseCapabilityRefresh: ...
    def refreshes(self) -> tuple[CoinbaseCapabilityRefresh, ...]: ...


class CoinbaseCredentialMaterial:
    """Ephemeral authentication material with a deliberately redacted representation."""

    __slots__ = ("key_name", "private_key")

    def __init__(self, *, key_name: str, private_key: str) -> None:
        if not key_name or not private_key:
            raise CoinbaseReadOnlyError("COINBASE_CREDENTIAL_MATERIAL_INVALID")
        self.key_name = key_name
        self.private_key = private_key

    def __repr__(self) -> str:
        return "CoinbaseCredentialMaterial([REDACTED])"


class CoinbaseCredentialResolver(Protocol):
    def resolve(self, reference: CredentialReference) -> CoinbaseCredentialMaterial: ...


@dataclass(frozen=True, slots=True)
class CoinbasePrivateResponse:
    status: int
    payload: bytes
    completed_at: datetime


class CoinbasePrivateReadTransport(Protocol):
    def get(
        self,
        *,
        credential: CoinbaseCredentialMaterial,
        endpoint_class: CoinbaseEndpointClass,
        path: str,
        timeout_seconds: int,
        requested_at: datetime,
    ) -> CoinbasePrivateResponse: ...


@dataclass(frozen=True, slots=True)
class CoinbaseInspectionResult:
    attempt: CoinbaseInspectionAttempt
    capture: CoinbaseCapabilityCapture | None
    replayed: bool
    readiness_snapshot_id: str | None


class ApproveCoinbaseReadOnlyInspection:
    def __init__(
        self,
        repository: CoinbaseReadOnlyRepository,
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
        allowed_endpoint_classes: Sequence[CoinbaseEndpointClass],
        maximum_request_count: int,
        timeout_seconds: int,
        approved_at: datetime,
        expires_at: datetime,
        actor: str,
        reason: str,
        idempotency_key: str,
        confirmation: str,
    ) -> CoinbaseReadApproval:
        expected = f"APPROVE_COINBASE_READ_ONLY:{approval_id}:{credential_reference_id}"
        if confirmation != expected:
            raise CoinbaseReadOnlyError("COINBASE_READ_APPROVAL_EXACT_CONFIRMATION_REQUIRED")
        reference = self._live_repository.credential_reference(credential_reference_id)
        if reference is None:
            raise CoinbaseReadOnlyError("COINBASE_CREDENTIAL_REFERENCE_NOT_FOUND")
        if (
            reference.provider is not Provider.COINBASE
            or reference.account_purpose != account_purpose
            or set(reference.required_permissions) != {PermissionClass.PRIVATE_VIEW}
        ):
            raise CoinbaseReadOnlyError("COINBASE_CREDENTIAL_REFERENCE_SCOPE_INVALID")
        value = CoinbaseReadApproval.create(
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
            raise CoinbaseReadOnlyError("COINBASE_READ_APPROVAL_ID_CONFLICT")
        return self._repository.save_approval(value)


def _object(value: object, reason: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CoinbaseReadOnlyError(reason)
    return cast(dict[str, object], value)


def _decode(payload: bytes, reason: str) -> dict[str, object]:
    try:
        return _object(json.loads(payload), reason)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoinbaseReadOnlyError(reason) from error


def _assert_response_secret_free(payload: bytes, *, credential: CoinbaseCredentialMaterial) -> None:
    try:
        value = json.loads(payload)
        assert_secret_free(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CoinbaseReadOnlyError("COINBASE_RESPONSE_SECRET_SAFETY_UNPROVEN") from error
    rendered = payload.decode("utf-8", errors="ignore")
    if credential.private_key in rendered or "Bearer " in rendered:
        raise CoinbaseReadOnlyError("COINBASE_RESPONSE_SECRET_SAFETY_UNPROVEN")


def _string(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value:
        raise CoinbaseReadOnlyError(reason)
    return value


def _permission(value: object) -> str:
    if value is True:
        return "EXPLICIT_TRUE"
    if value is False:
        return "EXPLICIT_FALSE"
    return "UNKNOWN"


def _account_evidence(
    payload: bytes, *, source_hash: str, observed_at: datetime
) -> tuple[str, ProviderAccountStatus, str, tuple[CoinbaseBalanceEvidence, ...], tuple[str, ...]]:
    root = _decode(payload, "COINBASE_ACCOUNTS_RESPONSE_INVALID")
    records = root.get("accounts")
    if not isinstance(records, list) or not records:
        raise CoinbaseReadOnlyError("COINBASE_ACCOUNTS_RESPONSE_INVALID")
    balances: list[CoinbaseBalanceEvidence] = []
    fingerprints: list[str] = []
    account_types: set[str] = set()
    restrictions: set[str] = set()
    for raw in records:
        account = _object(raw, "COINBASE_ACCOUNT_RESPONSE_INVALID")
        identifier = _string(account.get("uuid"), "COINBASE_ACCOUNT_IDENTITY_INVALID")
        fingerprints.append(hashlib.sha256(identifier.encode()).hexdigest())
        currency = _string(account.get("currency"), "COINBASE_ACCOUNT_CURRENCY_INVALID")
        available = _object(account.get("available_balance"), "COINBASE_AVAILABLE_BALANCE_INVALID")
        hold_raw = account.get("hold")
        hold = None if hold_raw is None else _object(hold_raw, "COINBASE_HELD_BALANCE_INVALID")
        active = account.get("active")
        ready = account.get("ready")
        status = "ACTIVE" if active is True and ready is True else "RESTRICTED"
        if status == "RESTRICTED":
            restrictions.add(f"ACCOUNT_RESTRICTED:{currency}")
        balances.append(
            CoinbaseBalanceEvidence.create(
                currency=currency,
                available_amount=_string(
                    available.get("value"), "COINBASE_AVAILABLE_BALANCE_INVALID"
                ),
                held_amount=None
                if hold is None
                else _string(hold.get("value"), "COINBASE_HELD_BALANCE_INVALID"),
                account_status=status,
                observed_at=observed_at,
                source_hash=source_hash,
            )
        )
        account_types.add(str(account.get("type", "UNKNOWN")))
    fingerprint = (
        "sha256:" + hashlib.sha256("|".join(sorted(fingerprints)).encode()).hexdigest()[:24]
    )
    provider_status = (
        ProviderAccountStatus.RESTRICTED if restrictions else ProviderAccountStatus.ACTIVE
    )
    return (
        fingerprint,
        provider_status,
        ",".join(sorted(account_types)),
        tuple(balances),
        tuple(sorted(restrictions)),
    )


def _fee_evidence(
    payload: bytes,
) -> tuple[Decimal | None, Decimal | None, str | None, tuple[str, ...]]:
    root = _decode(payload, "COINBASE_FEE_RESPONSE_INVALID")
    tier = root.get("fee_tier")
    if not isinstance(tier, Mapping):
        return None, None, None, ("FEE_TIER_UNAVAILABLE",)
    maker_raw = tier.get("maker_fee_rate")
    taker_raw = tier.get("taker_fee_rate")
    if not isinstance(maker_raw, str) or not isinstance(taker_raw, str):
        return None, None, None, ("MAKER_OR_TAKER_FEE_UNAVAILABLE",)
    try:
        maker = Decimal(maker_raw)
        taker = Decimal(taker_raw)
    except Exception as error:
        raise CoinbaseReadOnlyError("COINBASE_FEE_RESPONSE_INVALID") from error
    limitations: tuple[str, ...] = ()
    pricing = tier.get("pricing_tier")
    if not isinstance(pricing, str):
        limitations = ("FEE_PRICING_TIER_IDENTITY_UNAVAILABLE",)
    return maker, taker, pricing if isinstance(pricing, str) else None, limitations


def _product_evidence(payload: bytes, *, source_hash: str) -> CoinbaseProductConstraint:
    value = _decode(payload, "COINBASE_PRODUCT_RESPONSE_INVALID")
    restrictions = tuple(
        name
        for name, enabled in (
            ("TRADING_DISABLED", value.get("trading_disabled")),
            ("PRODUCT_DISABLED", value.get("is_disabled")),
            ("CANCEL_ONLY", value.get("cancel_only")),
            ("LIMIT_ONLY", value.get("limit_only")),
            ("POST_ONLY", value.get("post_only")),
            ("VIEW_ONLY", value.get("view_only")),
        )
        if enabled is True
    )
    return CoinbaseProductConstraint.create(
        product_id=_string(value.get("product_id"), "COINBASE_PRODUCT_ID_INVALID"),
        base_currency=_string(value.get("base_currency_id"), "COINBASE_BASE_CURRENCY_INVALID"),
        quote_currency=_string(value.get("quote_currency_id"), "COINBASE_QUOTE_CURRENCY_INVALID"),
        base_increment=_string(value.get("base_increment"), "COINBASE_BASE_INCREMENT_INVALID"),
        quote_increment=_string(value.get("quote_increment"), "COINBASE_QUOTE_INCREMENT_INVALID"),
        minimum_base_size=_string(value.get("base_min_size"), "COINBASE_MINIMUM_BASE_INVALID"),
        minimum_quote_size=_string(value.get("quote_min_size"), "COINBASE_MINIMUM_QUOTE_INVALID"),
        product_available=not restrictions,
        account_restrictions=restrictions,
        source_hash=source_hash,
    )


def _capture_from_responses(
    *,
    capture_id: str,
    approval: CoinbaseReadApproval,
    account_purpose: str,
    account_endpoint: CoinbaseEndpointClass,
    plan: Sequence[tuple[CoinbaseEndpointClass, str]],
    responses: Mapping[CoinbaseEndpointClass | str, bytes],
    request_evidence: Sequence[CoinbaseRequestEvidence],
    inspected_at: datetime,
) -> CoinbaseCapabilityCapture:
    account_request = next(
        item for item in request_evidence if item.endpoint_class is account_endpoint
    )
    fingerprint, account_status, account_type, balances, account_restrictions = _account_evidence(
        responses[account_endpoint],
        source_hash=account_request.response_hash,
        observed_at=inspected_at,
    )
    permission_root = _decode(
        responses[CoinbaseEndpointClass.PERMISSIONS_READ],
        "COINBASE_PERMISSION_RESPONSE_INVALID",
    )
    permission_evidence = tuple(
        (name, _permission(permission_root.get(name)))
        for name in ("can_view", "can_trade", "can_transfer", "can_receive", "preview")
    )
    maker: Decimal | None = None
    taker: Decimal | None = None
    fee_tier: str | None = None
    fee_limitations: tuple[str, ...] = ("FEE_EVIDENCE_NOT_REQUESTED",)
    if CoinbaseEndpointClass.FEES_READ in responses:
        maker, taker, fee_tier, fee_limitations = _fee_evidence(
            responses[CoinbaseEndpointClass.FEES_READ]
        )
    products = tuple(
        _product_evidence(
            responses[path],
            source_hash=next(item.response_hash for item in request_evidence if item.path == path),
        )
        for endpoint_class, path in plan
        if endpoint_class is CoinbaseEndpointClass.PRODUCT_RESTRICTIONS_READ
    )
    return CoinbaseCapabilityCapture.create(
        capture_id=capture_id,
        approval=approval,
        account_purpose=account_purpose,
        redacted_account_fingerprint=fingerprint,
        account_status=account_status,
        account_type=account_type,
        permission_evidence=permission_evidence,
        balances=balances,
        maker_fee_rate=maker,
        taker_fee_rate=taker,
        fee_tier=fee_tier,
        fee_limitations=fee_limitations,
        products=products,
        requests=request_evidence,
        observed_at=inspected_at,
        fresh_until=inspected_at + timedelta(minutes=15),
        redaction_proof=(
            "AUTHORIZATION_HEADER_NOT_RETAINED",
            "SECRET_VALUES_NOT_RETAINED",
            "RAW_PAYLOAD_NOT_RETAINED",
            "ACCOUNT_IDENTITY_FINGERPRINTED",
        ),
        limitations=(
            "BALANCES_ARE_READ_ONLY_EVIDENCE_NOT_APEX_CAPITAL",
            "PUBLIC_PRODUCT_EVIDENCE_REMAINS_AUTHORITATIVE",
            *account_restrictions,
        ),
    )


class InspectCoinbaseReadOnlyCapability:
    def __init__(
        self,
        repository: CoinbaseReadOnlyRepository,
        live_repository: LiveReadinessRepository,
        resolver: CoinbaseCredentialResolver,
        transport: CoinbasePrivateReadTransport,
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
    ) -> CoinbaseInspectionResult:
        attempt = self._repository.save_attempt(
            CoinbaseInspectionAttempt.create(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                status=CoinbaseInspectionStatus.BLOCKED,
                blocked_reason=reason,
                capture_id=None,
                request_count=0,
                warnings=(reason,),
                attempted_at=at,
            )
        )
        return CoinbaseInspectionResult(attempt, None, False, None)

    def execute(
        self,
        *,
        attempt_id: str,
        capture_id: str,
        readiness_snapshot_id: str,
        approval_id: str | None,
        credential_reference_id: str | None,
        account_purpose: str,
        product_ids: Sequence[str],
        inspected_at: datetime,
        execution_control: ExecutionControlState,
        due: bool = True,
    ) -> CoinbaseInspectionResult:
        existing = self._repository.attempt(attempt_id)
        if existing is not None:
            capture = (
                None
                if existing.capture_id is None
                else self._repository.capture(existing.capture_id)
            )
            return CoinbaseInspectionResult(
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

        plan: list[tuple[CoinbaseEndpointClass, str]] = []
        endpoints = set(approval.allowed_endpoint_classes)
        account_endpoint = (
            CoinbaseEndpointClass.ACCOUNT_STATUS_READ
            if CoinbaseEndpointClass.ACCOUNT_STATUS_READ in endpoints
            else CoinbaseEndpointClass.BALANCES_READ
        )
        if endpoints & {
            CoinbaseEndpointClass.ACCOUNT_STATUS_READ,
            CoinbaseEndpointClass.BALANCES_READ,
        }:
            plan.append((account_endpoint, "/api/v3/brokerage/accounts"))
        if CoinbaseEndpointClass.PERMISSIONS_READ in endpoints:
            plan.append(
                (CoinbaseEndpointClass.PERMISSIONS_READ, "/api/v3/brokerage/key_permissions")
            )
        if CoinbaseEndpointClass.FEES_READ in endpoints:
            plan.append((CoinbaseEndpointClass.FEES_READ, "/api/v3/brokerage/transaction_summary"))
        if CoinbaseEndpointClass.PRODUCT_RESTRICTIONS_READ in endpoints:
            plan.extend(
                (
                    CoinbaseEndpointClass.PRODUCT_RESTRICTIONS_READ,
                    f"/api/v3/brokerage/products/{product_id}",
                )
                for product_id in product_ids
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
            validate_coinbase_private_read_request(
                method="GET", host=COINBASE_PRIVATE_HOST, path=path, endpoint_class=endpoint_class
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

        responses: dict[CoinbaseEndpointClass | str, bytes] = {}
        request_evidence: list[CoinbaseRequestEvidence] = []
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
                    CoinbaseInspectionAttempt.create(
                        attempt_id=attempt_id,
                        approval_id=approval_id,
                        credential_reference_id=credential_reference_id,
                        status=CoinbaseInspectionStatus.PROVIDER_FAILED,
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
                return CoinbaseInspectionResult(attempt, None, False, None)
            try:
                _assert_response_secret_free(response.payload, credential=credential)
            except CoinbaseReadOnlyError:
                attempt = self._repository.save_attempt(
                    CoinbaseInspectionAttempt.create(
                        attempt_id=attempt_id,
                        approval_id=approval_id,
                        credential_reference_id=credential_reference_id,
                        status=CoinbaseInspectionStatus.PROVIDER_FAILED,
                        blocked_reason="PROVIDER_RESPONSE_SECRET_SAFETY_UNPROVEN",
                        capture_id=None,
                        request_count=ordinal,
                        warnings=("PROVIDER_RESPONSE_DISCARDED_WITHOUT_HASH_OR_PAYLOAD_RETENTION",),
                        attempted_at=inspected_at,
                    )
                )
                return CoinbaseInspectionResult(attempt, None, False, None)
            response_hash = hashlib.sha256(response.payload).hexdigest()
            request_evidence.append(
                CoinbaseRequestEvidence.create(
                    request_id=f"{attempt_id}-request-{ordinal}",
                    endpoint_class=endpoint_class,
                    path=path,
                    response_status=response.status,
                    response_hash=response_hash,
                    requested_at=inspected_at,
                    completed_at=response.completed_at,
                    response_bytes=len(response.payload),
                )
            )
            if response.status in {401, 403}:
                attempt = self._repository.save_attempt(
                    CoinbaseInspectionAttempt.create(
                        attempt_id=attempt_id,
                        approval_id=approval_id,
                        credential_reference_id=credential_reference_id,
                        status=CoinbaseInspectionStatus.AUTHENTICATION_FAILED,
                        blocked_reason="AUTHENTICATION_FAILURE_REDACTED",
                        capture_id=None,
                        request_count=len(request_evidence),
                        warnings=(
                            "AUTHENTICATION_FAILURE_REDACTED",
                            "AUTHENTICATION_HEADERS_NOT_RETAINED",
                        ),
                        attempted_at=inspected_at,
                    )
                )
                return CoinbaseInspectionResult(attempt, None, False, None)
            if response.status != 200:
                attempt = self._repository.save_attempt(
                    CoinbaseInspectionAttempt.create(
                        attempt_id=attempt_id,
                        approval_id=approval_id,
                        credential_reference_id=credential_reference_id,
                        status=CoinbaseInspectionStatus.PROVIDER_FAILED,
                        blocked_reason=f"PROVIDER_HTTP_{response.status}",
                        capture_id=None,
                        request_count=len(request_evidence),
                        warnings=("PROVIDER_RESPONSE_PAYLOAD_NOT_RETAINED",),
                        attempted_at=inspected_at,
                    )
                )
                return CoinbaseInspectionResult(attempt, None, False, None)
            key: CoinbaseEndpointClass | str = (
                path
                if endpoint_class is CoinbaseEndpointClass.PRODUCT_RESTRICTIONS_READ
                else endpoint_class
            )
            responses[key] = response.payload

        account_key = account_endpoint
        if account_key not in responses or CoinbaseEndpointClass.PERMISSIONS_READ not in responses:
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
                account_endpoint=account_endpoint,
                plan=plan,
                responses=responses,
                request_evidence=request_evidence,
                inspected_at=inspected_at,
            )
        except (CoinbaseReadOnlyError, KeyError, StopIteration):
            attempt = self._repository.save_attempt(
                CoinbaseInspectionAttempt.create(
                    attempt_id=attempt_id,
                    approval_id=approval_id,
                    credential_reference_id=credential_reference_id,
                    status=CoinbaseInspectionStatus.PROVIDER_FAILED,
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
            return CoinbaseInspectionResult(attempt, None, False, None)
        self._repository.save_capture(capture)
        snapshot = capture.to_live_readiness_snapshot(
            snapshot_id=readiness_snapshot_id, ingested_at=inspected_at
        )
        self._live_repository.save_capability(snapshot)
        attempt = self._repository.save_attempt(
            CoinbaseInspectionAttempt.create(
                attempt_id=attempt_id,
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                status=CoinbaseInspectionStatus.SUCCEEDED,
                blocked_reason=None,
                capture_id=capture.capture_id,
                request_count=capture.provider_request_count,
                warnings=capture.warnings,
                attempted_at=inspected_at,
            )
        )
        return CoinbaseInspectionResult(attempt, capture, False, snapshot.snapshot_id)


class RefreshCoinbaseReadOnlyCapability:
    def __init__(
        self, repository: CoinbaseReadOnlyRepository, inspector: InspectCoinbaseReadOnlyCapability
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
        product_ids: Sequence[str],
        evaluated_at: datetime,
        due: bool,
        execution_control: ExecutionControlState,
    ) -> CoinbaseCapabilityRefresh:
        if not due:
            value = CoinbaseCapabilityRefresh.create(
                refresh_id=refresh_id,
                due=False,
                disposition=CoinbaseRefreshDisposition.NOT_DUE,
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
                product_ids=product_ids,
                inspected_at=evaluated_at,
                execution_control=execution_control,
                due=True,
            )
            value = CoinbaseCapabilityRefresh.create(
                refresh_id=refresh_id,
                due=True,
                disposition=CoinbaseRefreshDisposition.COMPLETED
                if result.capture is not None
                else CoinbaseRefreshDisposition.BLOCKED,
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


def coinbase_read_only_attention(
    *,
    capture: CoinbaseCapabilityCapture | None,
    attempt: CoinbaseInspectionAttempt | None,
    as_of: datetime,
    previous_readiness: str | None = None,
    current_readiness: str | None = None,
) -> tuple[str, ...]:
    values: set[str] = set()
    if attempt is not None:
        if attempt.blocked_reason == "CREDENTIAL_REFERENCE_INVALID":
            values.add("INVALID_CREDENTIAL_REFERENCE")
        if attempt.status is CoinbaseInspectionStatus.AUTHENTICATION_FAILED:
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
                CoinbaseCapabilityClass.LIVE_TRADE_PERMISSION_AVAILABLE,
                CoinbaseCapabilityClass.ORDER_PREVIEW_AVAILABLE,
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


def coinbase_read_only_workspace(repository: CoinbaseReadOnlyRepository) -> dict[str, object]:
    approvals = repository.approvals()
    attempts = repository.attempts()
    captures = repository.captures()
    refreshes = repository.refreshes()
    return {
        "provider": "COINBASE",
        "endpoint_allowlist_policy": "COINBASE_PRIVATE_READ_ALLOWLIST_V1",
        "allowed_endpoint_classes": tuple(item.value for item in CoinbaseEndpointClass),
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


class InMemoryCoinbaseReadOnlyRepository:
    def __init__(self) -> None:
        self._approvals: dict[str, CoinbaseReadApproval] = {}
        self._attempts: dict[str, CoinbaseInspectionAttempt] = {}
        self._captures: dict[str, CoinbaseCapabilityCapture] = {}
        self._refreshes: dict[str, CoinbaseCapabilityRefresh] = {}

    @staticmethod
    def _save[T](store: dict[str, T], key: str, value: T) -> T:
        existing = store.get(key)
        if existing is not None and existing != value:
            raise CoinbaseReadOnlyError("COINBASE_READ_ONLY_ID_CONFLICT")
        store[key] = value
        return value

    def save_approval(self, value: CoinbaseReadApproval) -> CoinbaseReadApproval:
        return self._save(self._approvals, value.approval_id, value)

    def approval(self, approval_id: str) -> CoinbaseReadApproval | None:
        return self._approvals.get(approval_id)

    def approvals(self) -> tuple[CoinbaseReadApproval, ...]:
        return tuple(self._approvals.values())

    def save_attempt(self, value: CoinbaseInspectionAttempt) -> CoinbaseInspectionAttempt:
        return self._save(self._attempts, value.attempt_id, value)

    def attempt(self, attempt_id: str) -> CoinbaseInspectionAttempt | None:
        return self._attempts.get(attempt_id)

    def attempts(self) -> tuple[CoinbaseInspectionAttempt, ...]:
        return tuple(self._attempts.values())

    def save_capture(self, value: CoinbaseCapabilityCapture) -> CoinbaseCapabilityCapture:
        return self._save(self._captures, value.capture_id, value)

    def capture(self, capture_id: str) -> CoinbaseCapabilityCapture | None:
        return self._captures.get(capture_id)

    def captures(self) -> tuple[CoinbaseCapabilityCapture, ...]:
        return tuple(self._captures.values())

    def save_refresh(self, value: CoinbaseCapabilityRefresh) -> CoinbaseCapabilityRefresh:
        return self._save(self._refreshes, value.refresh_id, value)

    def refreshes(self) -> tuple[CoinbaseCapabilityRefresh, ...]:
        return tuple(self._refreshes.values())
