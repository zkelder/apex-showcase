"""Pure Kalshi private read-only capability evidence and safety rules.

This module contains no secret resolver, authentication signer, network client,
order path, financial state, or execution authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.live_readiness import (
    AccountEnvironment,
    AuthenticatedReadStatus,
    PermissionClass,
    Provider,
    ProviderAccountStatus,
    ProviderCapabilitySnapshot,
    assert_secret_free,
)
from apex.domain.values import canonical_decimal, content_digest, parse_decimal

KALSHI_PRIVATE_READ_APPROVAL_V1 = "KALSHI_PRIVATE_READ_APPROVAL_V1"
KALSHI_PRIVATE_READ_ALLOWLIST_V1 = "KALSHI_PRIVATE_READ_ALLOWLIST_V1"
KALSHI_READ_ONLY_CAPABILITY_V1 = "KALSHI_READ_ONLY_CAPABILITY_V1"
KALSHI_READ_ONLY_REFRESH_V1 = "KALSHI_READ_ONLY_REFRESH_V1"
KALSHI_READ_ONLY_WARNING = "KALSHI PRIVATE VIEW ONLY — BALANCES ARE EVIDENCE, NOT CAPITAL"
KALSHI_PRIVATE_HOST = "external-api.kalshi.com"
MAX_PRIVATE_RESPONSE_BYTES = 1_000_000


class KalshiReadOnlyError(ValueError):
    """The private read-only boundary failed closed."""


class KalshiEndpointClass(StrEnum):
    ACCOUNT_IDENTITY_READ = "ACCOUNT_IDENTITY_READ"
    BALANCE_READ = "BALANCE_READ"
    PERMISSIONS_READ = "PERMISSIONS_READ"
    ACCOUNT_TIER_READ = "ACCOUNT_TIER_READ"


class KalshiInspectionStatus(StrEnum):
    BLOCKED = "BLOCKED"
    SUCCEEDED = "SUCCEEDED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    PROVIDER_FAILED = "PROVIDER_FAILED"


class KalshiRefreshDisposition(StrEnum):
    COMPLETED = "COMPLETED"
    NOT_DUE = "NOT_DUE"
    BLOCKED = "BLOCKED"


class KalshiCapabilityClass(StrEnum):
    PRIVATE_CREDENTIALS_ABSENT = "PRIVATE_CREDENTIALS_ABSENT"
    PRIVATE_VIEW_AVAILABLE = "PRIVATE_VIEW_AVAILABLE"
    ORDER_PREVIEW_AVAILABLE = "ORDER_PREVIEW_AVAILABLE"
    LIVE_TRADE_PERMISSION_AVAILABLE = "LIVE_TRADE_PERMISSION_AVAILABLE"
    TRANSFER_PERMISSION_AVAILABLE = "TRANSFER_PERMISSION_AVAILABLE"
    WITHDRAWAL_PERMISSION_AVAILABLE = "WITHDRAWAL_PERMISSION_AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


_EXACT_PATHS: Mapping[KalshiEndpointClass, str] = {
    KalshiEndpointClass.ACCOUNT_IDENTITY_READ: "/trade-api/v2/communications/id",
    KalshiEndpointClass.BALANCE_READ: "/trade-api/v2/portfolio/balance?subaccount=0",
    KalshiEndpointClass.PERMISSIONS_READ: "/trade-api/v2/api_keys",
    KalshiEndpointClass.ACCOUNT_TIER_READ: "/trade-api/v2/account/limits",
}
KALSHI_PRIVATE_READ_ENDPOINTS = tuple(item.value for item in KalshiEndpointClass)


def _id(value: str, reason: str, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise KalshiReadOnlyError(reason)
    return value.strip()


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise KalshiReadOnlyError(reason)
    return value.astimezone(UTC)


def _decimal(value: Decimal | str, reason: str) -> Decimal:
    try:
        result = parse_decimal(value)
    except ValueError as error:
        raise KalshiReadOnlyError(reason) from error
    if result < 0:
        raise KalshiReadOnlyError(reason)
    return result


def validate_kalshi_private_read_request(
    *, method: str, host: str, path: str, endpoint_class: KalshiEndpointClass
) -> None:
    """Reject every non-GET, non-production-host, or non-allowlisted path pre-I/O."""

    if method != "GET" or host != KALSHI_PRIVATE_HOST or "#" in path:
        raise KalshiReadOnlyError("KALSHI_PRIVATE_REQUEST_NOT_ALLOWLISTED")
    if _EXACT_PATHS.get(endpoint_class) != path:
        raise KalshiReadOnlyError("KALSHI_PRIVATE_REQUEST_NOT_ALLOWLISTED")


@dataclass(frozen=True, slots=True)
class KalshiReadApproval:
    approval_id: str
    provider: Provider
    credential_reference_id: str
    account_purpose: str
    allowed_endpoint_classes: tuple[KalshiEndpointClass, ...]
    maximum_request_count: int
    timeout_seconds: int
    approved_at: datetime
    expires_at: datetime
    actor: str
    reason: str
    idempotency_key: str
    read_only_only: bool
    zero_provider_writes: bool
    preview_allowed: bool
    trade_allowed: bool
    cancellation_allowed: bool
    fund_movement_allowed: bool
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
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
    ) -> KalshiReadApproval:
        endpoints = tuple(sorted(set(allowed_endpoint_classes), key=lambda item: item.value))
        start = _time(approved_at, "KALSHI_APPROVAL_TIME_INVALID")
        end = _time(expires_at, "KALSHI_APPROVAL_EXPIRATION_INVALID")
        if (
            not endpoints
            or KalshiEndpointClass.PERMISSIONS_READ not in endpoints
            or not {
                KalshiEndpointClass.ACCOUNT_IDENTITY_READ,
                KalshiEndpointClass.BALANCE_READ,
                KalshiEndpointClass.PERMISSIONS_READ,
            }.issubset(endpoints)
            or not 1 <= maximum_request_count <= 10
            or not 1 <= timeout_seconds <= 30
            or end <= start
        ):
            raise KalshiReadOnlyError("KALSHI_READ_APPROVAL_SCOPE_INVALID")
        values = (
            _id(approval_id, "KALSHI_APPROVAL_ID_INVALID"),
            Provider.KALSHI,
            _id(credential_reference_id, "KALSHI_APPROVAL_CREDENTIAL_INVALID"),
            _id(account_purpose, "KALSHI_APPROVAL_PURPOSE_INVALID"),
            endpoints,
            maximum_request_count,
            timeout_seconds,
            start,
            end,
            _id(actor, "KALSHI_APPROVAL_ACTOR_INVALID"),
            _id(reason, "KALSHI_APPROVAL_REASON_INVALID"),
            _id(idempotency_key, "KALSHI_APPROVAL_IDEMPOTENCY_INVALID"),
            True,
            True,
            False,
            False,
            False,
            False,
            "NONE",
        )
        assert_secret_free(values)
        return cls(
            *values[:-1],
            content_digest((KALSHI_PRIVATE_READ_APPROVAL_V1, *values)),
            values[-1],
        )

    def active_at(self, value: datetime) -> bool:
        moment = _time(value, "KALSHI_APPROVAL_EVALUATION_TIME_INVALID")
        return self.approved_at <= moment < self.expires_at


@dataclass(frozen=True, slots=True)
class KalshiRequestEvidence:
    request_id: str
    endpoint_class: KalshiEndpointClass
    method: str
    host: str
    path: str
    response_status: int
    response_hash: str
    requested_at: datetime
    completed_at: datetime
    response_bytes: int
    redacted: bool = True
    provider_write_count: int = 0

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        endpoint_class: KalshiEndpointClass,
        path: str,
        response_status: int,
        response_hash: str,
        requested_at: datetime,
        completed_at: datetime,
        response_bytes: int,
    ) -> KalshiRequestEvidence:
        validate_kalshi_private_read_request(
            method="GET", host=KALSHI_PRIVATE_HOST, path=path, endpoint_class=endpoint_class
        )
        if (
            not 100 <= response_status <= 599
            or len(response_hash) != 64
            or any(character not in "0123456789abcdef" for character in response_hash)
            or not 0 <= response_bytes <= MAX_PRIVATE_RESPONSE_BYTES
        ):
            raise KalshiReadOnlyError("KALSHI_REQUEST_EVIDENCE_INVALID")
        requested = _time(requested_at, "KALSHI_REQUEST_TIME_INVALID")
        completed = _time(completed_at, "KALSHI_RESPONSE_TIME_INVALID")
        if completed < requested:
            raise KalshiReadOnlyError("KALSHI_REQUEST_TIME_INVALID")
        return cls(
            _id(request_id, "KALSHI_REQUEST_ID_INVALID"),
            endpoint_class,
            "GET",
            KALSHI_PRIVATE_HOST,
            path,
            response_status,
            response_hash,
            requested,
            completed,
            response_bytes,
        )


@dataclass(frozen=True, slots=True)
class KalshiBalanceEvidence:
    currency: str
    available_amount: Decimal
    portfolio_value: Decimal | None
    observed_at: datetime
    source_hash: str

    @classmethod
    def create(
        cls,
        *,
        currency: str,
        available_amount: Decimal | str,
        portfolio_value: Decimal | str | None,
        observed_at: datetime,
        source_hash: str,
    ) -> KalshiBalanceEvidence:
        if len(source_hash) != 64:
            raise KalshiReadOnlyError("KALSHI_BALANCE_SOURCE_HASH_INVALID")
        return cls(
            _id(currency, "KALSHI_BALANCE_CURRENCY_INVALID", 40),
            _decimal(available_amount, "KALSHI_BALANCE_AVAILABLE_INVALID"),
            None
            if portfolio_value is None
            else _decimal(portfolio_value, "KALSHI_PORTFOLIO_VALUE_INVALID"),
            _time(observed_at, "KALSHI_BALANCE_TIME_INVALID"),
            source_hash,
        )


@dataclass(frozen=True, slots=True)
class KalshiCapabilityCapture:
    capture_id: str
    approval_id: str
    credential_reference_id: str
    account_purpose: str
    redacted_account_fingerprint: str
    account_status: ProviderAccountStatus
    account_type: str
    permission_evidence: tuple[tuple[str, str], ...]
    capability_classes: tuple[KalshiCapabilityClass, ...]
    balances: tuple[KalshiBalanceEvidence, ...]
    maker_fee_rate: Decimal | None
    taker_fee_rate: Decimal | None
    account_tier: str | None
    fee_limitations: tuple[str, ...]
    account_restrictions: tuple[str, ...]
    requests: tuple[KalshiRequestEvidence, ...]
    observed_at: datetime
    fresh_until: datetime
    redaction_proof: tuple[str, ...]
    limitations: tuple[str, ...]
    warnings: tuple[str, ...]
    canonical_hash: str
    provider_request_count: int
    provider_write_count: int = 0
    authority: str = "NONE"
    capital_authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        capture_id: str,
        approval: KalshiReadApproval,
        account_purpose: str,
        redacted_account_fingerprint: str,
        account_status: ProviderAccountStatus,
        account_type: str,
        permission_evidence: Sequence[tuple[str, str]],
        balances: Sequence[KalshiBalanceEvidence],
        maker_fee_rate: Decimal | str | None,
        taker_fee_rate: Decimal | str | None,
        account_tier: str | None,
        fee_limitations: Sequence[str],
        account_restrictions: Sequence[str],
        requests: Sequence[KalshiRequestEvidence],
        observed_at: datetime,
        fresh_until: datetime,
        redaction_proof: Sequence[str],
        limitations: Sequence[str],
    ) -> KalshiCapabilityCapture:
        if account_purpose != approval.account_purpose:
            raise KalshiReadOnlyError("KALSHI_CAPTURE_APPROVAL_PURPOSE_MISMATCH")
        normalized_permissions = tuple(
            sorted(
                (
                    _id(key, "KALSHI_PERMISSION_KEY_INVALID"),
                    _id(value, "KALSHI_PERMISSION_VALUE_INVALID"),
                )
                for key, value in permission_evidence
            )
        )
        permission_map = dict(normalized_permissions)
        classes: set[KalshiCapabilityClass] = set()
        if permission_map.get("private_view") == "EXPLICIT_TRUE":
            classes.add(KalshiCapabilityClass.PRIVATE_VIEW_AVAILABLE)
        if permission_map.get("trade") == "EXPLICIT_TRUE":
            classes.add(KalshiCapabilityClass.LIVE_TRADE_PERMISSION_AVAILABLE)
        if permission_map.get("transfer") == "EXPLICIT_TRUE":
            classes.add(KalshiCapabilityClass.TRANSFER_PERMISSION_AVAILABLE)
        if permission_map.get("withdrawal") == "EXPLICIT_TRUE":
            classes.add(KalshiCapabilityClass.WITHDRAWAL_PERMISSION_AVAILABLE)
        if permission_map.get("order_preview") == "EXPLICIT_TRUE":
            classes.add(KalshiCapabilityClass.ORDER_PREVIEW_AVAILABLE)
        if KalshiCapabilityClass.PRIVATE_VIEW_AVAILABLE not in classes:
            raise KalshiReadOnlyError("KALSHI_PRIVATE_VIEW_NOT_EXPLICIT")
        request_values = tuple(requests)
        if (
            not request_values
            or len(request_values) > approval.maximum_request_count
            or len({item.request_id for item in request_values}) != len(request_values)
            or any(
                item.endpoint_class not in approval.allowed_endpoint_classes
                for item in request_values
            )
            or any(item.provider_write_count for item in request_values)
        ):
            raise KalshiReadOnlyError("KALSHI_CAPTURE_REQUEST_SCOPE_INVALID")
        observed = _time(observed_at, "KALSHI_CAPTURE_TIME_INVALID")
        fresh = _time(fresh_until, "KALSHI_CAPTURE_FRESHNESS_INVALID")
        if fresh <= observed:
            raise KalshiReadOnlyError("KALSHI_CAPTURE_FRESHNESS_INVALID")
        proofs = tuple(
            sorted({_id(item, "KALSHI_REDACTION_PROOF_INVALID") for item in redaction_proof})
        )
        required_proofs = {
            "AUTHORIZATION_HEADER_NOT_RETAINED",
            "SECRET_VALUES_NOT_RETAINED",
            "RAW_PAYLOAD_NOT_RETAINED",
        }
        if not required_proofs.issubset(proofs):
            raise KalshiReadOnlyError("KALSHI_REDACTION_PROOF_INCOMPLETE")
        warnings = set()
        if KalshiCapabilityClass.LIVE_TRADE_PERMISSION_AVAILABLE in classes:
            warnings.add("UNEXPECTED_TRADE_PERMISSION_PRESENT")
        if KalshiCapabilityClass.ORDER_PREVIEW_AVAILABLE in classes:
            warnings.add("UNEXPECTED_ORDER_PREVIEW_PERMISSION_PRESENT")
        if classes & {
            KalshiCapabilityClass.TRANSFER_PERMISSION_AVAILABLE,
            KalshiCapabilityClass.WITHDRAWAL_PERMISSION_AVAILABLE,
        }:
            warnings.add("DANGEROUS_TRANSFER_AND_WITHDRAWAL_PERMISSION_PRESENT")
        if account_status is ProviderAccountStatus.RESTRICTED:
            warnings.add("KALSHI_ACCOUNT_RESTRICTED")
        if account_status is ProviderAccountStatus.UNAVAILABLE:
            warnings.add("KALSHI_ACCOUNT_STATUS_UNAVAILABLE")
        if maker_fee_rate is None or taker_fee_rate is None:
            warnings.add("KALSHI_FEE_EVIDENCE_MISSING")
        normalized_balances = tuple(sorted(balances, key=lambda item: item.currency))
        normalized_restrictions = tuple(
            sorted(
                {_id(item, "KALSHI_ACCOUNT_RESTRICTION_INVALID") for item in account_restrictions}
            )
        )
        values = (
            _id(capture_id, "KALSHI_CAPTURE_ID_INVALID"),
            approval.approval_id,
            approval.credential_reference_id,
            account_purpose,
            _id(redacted_account_fingerprint, "KALSHI_ACCOUNT_FINGERPRINT_INVALID"),
            account_status,
            _id(account_type, "KALSHI_ACCOUNT_TYPE_INVALID"),
            normalized_permissions,
            tuple(sorted(classes, key=lambda item: item.value)),
            normalized_balances,
            None
            if maker_fee_rate is None
            else _decimal(maker_fee_rate, "KALSHI_MAKER_FEE_INVALID"),
            None
            if taker_fee_rate is None
            else _decimal(taker_fee_rate, "KALSHI_TAKER_FEE_INVALID"),
            None if account_tier is None else _id(account_tier, "KALSHI_ACCOUNT_TIER_INVALID"),
            tuple(sorted({_id(item, "KALSHI_FEE_LIMITATION_INVALID") for item in fee_limitations})),
            normalized_restrictions,
            request_values,
            observed,
            fresh,
            proofs,
            tuple(sorted({_id(item, "KALSHI_CAPTURE_LIMITATION_INVALID") for item in limitations})),
            tuple(sorted(warnings)),
            len(request_values),
            0,
            "NONE",
            "NONE",
            "NONE",
        )
        assert_secret_free(values)
        return cls(
            *values[:21], content_digest((KALSHI_READ_ONLY_CAPABILITY_V1, *values)), *values[21:]
        )

    @property
    def permissions(self) -> tuple[PermissionClass, ...]:
        classes = set(self.capability_classes)
        values = {PermissionClass.PRIVATE_VIEW}
        if KalshiCapabilityClass.ORDER_PREVIEW_AVAILABLE in classes:
            values.add(PermissionClass.ORDER_PREVIEW)
        if KalshiCapabilityClass.LIVE_TRADE_PERMISSION_AVAILABLE in classes:
            values.add(PermissionClass.TRADE)
        if KalshiCapabilityClass.TRANSFER_PERMISSION_AVAILABLE in classes:
            values.add(PermissionClass.TRANSFER)
        if KalshiCapabilityClass.WITHDRAWAL_PERMISSION_AVAILABLE in classes:
            values.add(PermissionClass.WITHDRAWAL)
        return tuple(sorted(values, key=lambda item: item.value))

    def to_live_readiness_snapshot(
        self, *, snapshot_id: str, ingested_at: datetime
    ) -> ProviderCapabilitySnapshot:
        fee_values = [
            (name, canonical_decimal(value))
            for name, value in (
                ("MAKER_FEE_RATE", self.maker_fee_rate),
                ("TAKER_FEE_RATE", self.taker_fee_rate),
            )
            if value is not None
        ]
        fees = tuple(fee_values)
        return ProviderCapabilitySnapshot.create(
            snapshot_id=snapshot_id,
            version=1,
            provider=Provider.KALSHI,
            account_scope_id="kalshi-default",
            credential_reference_id=self.credential_reference_id,
            account_environment=AccountEnvironment.LIVE,
            authenticated_read_status=AuthenticatedReadStatus.AVAILABLE,
            permissions=self.permissions,
            supported_products=(),
            restrictions=self.account_restrictions,
            balances=tuple((item.currency, item.available_amount) for item in self.balances),
            fee_evidence=fees,
            precision_constraints=(),
            minimum_size_constraints=(),
            account_status=self.account_status,
            observed_at=self.observed_at,
            ingested_at=ingested_at,
            evidence_class="GENUINE_PRIVATE_READ_ONLY_REDACTED",
            limitations=(KALSHI_READ_ONLY_WARNING, *self.limitations, *self.warnings),
            provider_request_count=self.provider_request_count,
        )


@dataclass(frozen=True, slots=True)
class KalshiInspectionAttempt:
    attempt_id: str
    approval_id: str | None
    credential_reference_id: str | None
    status: KalshiInspectionStatus
    blocked_reason: str | None
    capture_id: str | None
    request_count: int
    provider_write_count: int
    warnings: tuple[str, ...]
    attempted_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        attempt_id: str,
        approval_id: str | None,
        credential_reference_id: str | None,
        status: KalshiInspectionStatus,
        blocked_reason: str | None,
        capture_id: str | None,
        request_count: int,
        warnings: Sequence[str],
        attempted_at: datetime,
    ) -> KalshiInspectionAttempt:
        if request_count < 0 or (status is KalshiInspectionStatus.SUCCEEDED) != (
            capture_id is not None
        ):
            raise KalshiReadOnlyError("KALSHI_INSPECTION_ATTEMPT_INVALID")
        values = (
            _id(attempt_id, "KALSHI_ATTEMPT_ID_INVALID"),
            approval_id,
            credential_reference_id,
            status,
            blocked_reason,
            capture_id,
            request_count,
            0,
            tuple(sorted({_id(item, "KALSHI_ATTEMPT_WARNING_INVALID") for item in warnings})),
            _time(attempted_at, "KALSHI_ATTEMPT_TIME_INVALID"),
            "NONE",
        )
        assert_secret_free(values)
        return cls(*values[:-1], content_digest(values), values[-1])


@dataclass(frozen=True, slots=True)
class KalshiCapabilityRefresh:
    refresh_id: str
    due: bool
    disposition: KalshiRefreshDisposition
    reasons: tuple[str, ...]
    approval_id: str | None
    request_count: int
    output_id: str | None
    status: str
    warnings: tuple[str, ...]
    evaluated_at: datetime
    canonical_hash: str
    provider_write_count: int = 0
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        refresh_id: str,
        due: bool,
        disposition: KalshiRefreshDisposition,
        reasons: Sequence[str],
        approval_id: str | None,
        request_count: int,
        output_id: str | None,
        status: str,
        warnings: Sequence[str],
        evaluated_at: datetime,
    ) -> KalshiCapabilityRefresh:
        if request_count < 0 or (
            disposition is KalshiRefreshDisposition.COMPLETED and output_id is None
        ):
            raise KalshiReadOnlyError("KALSHI_REFRESH_INVALID")
        values = (
            _id(refresh_id, "KALSHI_REFRESH_ID_INVALID"),
            due,
            disposition,
            tuple(sorted({_id(item, "KALSHI_REFRESH_REASON_INVALID") for item in reasons})),
            approval_id,
            request_count,
            output_id,
            _id(status, "KALSHI_REFRESH_STATUS_INVALID"),
            tuple(sorted({_id(item, "KALSHI_REFRESH_WARNING_INVALID") for item in warnings})),
            _time(evaluated_at, "KALSHI_REFRESH_TIME_INVALID"),
            0,
            "NONE",
        )
        return cls(
            *values[:10], content_digest((KALSHI_READ_ONLY_REFRESH_V1, *values)), *values[10:]
        )
