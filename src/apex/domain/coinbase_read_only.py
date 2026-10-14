"""Pure Coinbase private read-only capability evidence and safety rules.

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

COINBASE_PRIVATE_READ_APPROVAL_V1 = "COINBASE_PRIVATE_READ_APPROVAL_V1"
COINBASE_PRIVATE_READ_ALLOWLIST_V1 = "COINBASE_PRIVATE_READ_ALLOWLIST_V1"
COINBASE_READ_ONLY_CAPABILITY_V1 = "COINBASE_READ_ONLY_CAPABILITY_V1"
COINBASE_READ_ONLY_REFRESH_V1 = "COINBASE_READ_ONLY_REFRESH_V1"
COINBASE_READ_ONLY_WARNING = "COINBASE PRIVATE VIEW ONLY — BALANCES ARE EVIDENCE, NOT CAPITAL"
COINBASE_PRIVATE_HOST = "api.coinbase.com"
MAX_PRIVATE_RESPONSE_BYTES = 1_000_000


class CoinbaseReadOnlyError(ValueError):
    """The private read-only boundary failed closed."""


class CoinbaseEndpointClass(StrEnum):
    ACCOUNT_STATUS_READ = "ACCOUNT_STATUS_READ"
    BALANCES_READ = "BALANCES_READ"
    PERMISSIONS_READ = "PERMISSIONS_READ"
    FEES_READ = "FEES_READ"
    PRODUCT_RESTRICTIONS_READ = "PRODUCT_RESTRICTIONS_READ"


class CoinbaseInspectionStatus(StrEnum):
    BLOCKED = "BLOCKED"
    SUCCEEDED = "SUCCEEDED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    PROVIDER_FAILED = "PROVIDER_FAILED"


class CoinbaseRefreshDisposition(StrEnum):
    COMPLETED = "COMPLETED"
    NOT_DUE = "NOT_DUE"
    BLOCKED = "BLOCKED"


class CoinbaseCapabilityClass(StrEnum):
    PRIVATE_CREDENTIALS_ABSENT = "PRIVATE_CREDENTIALS_ABSENT"
    PRIVATE_VIEW_AVAILABLE = "PRIVATE_VIEW_AVAILABLE"
    ORDER_PREVIEW_AVAILABLE = "ORDER_PREVIEW_AVAILABLE"
    LIVE_TRADE_PERMISSION_AVAILABLE = "LIVE_TRADE_PERMISSION_AVAILABLE"
    TRANSFER_PERMISSION_AVAILABLE = "TRANSFER_PERMISSION_AVAILABLE"
    WITHDRAWAL_PERMISSION_AVAILABLE = "WITHDRAWAL_PERMISSION_AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


_EXACT_PATHS: Mapping[CoinbaseEndpointClass, str] = {
    CoinbaseEndpointClass.ACCOUNT_STATUS_READ: "/api/v3/brokerage/accounts",
    CoinbaseEndpointClass.BALANCES_READ: "/api/v3/brokerage/accounts",
    CoinbaseEndpointClass.PERMISSIONS_READ: "/api/v3/brokerage/key_permissions",
    CoinbaseEndpointClass.FEES_READ: "/api/v3/brokerage/transaction_summary",
}
_PRODUCT_PREFIX = "/api/v3/brokerage/products/"
COINBASE_PRIVATE_READ_ENDPOINTS = tuple(item.value for item in CoinbaseEndpointClass)


def _id(value: str, reason: str, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise CoinbaseReadOnlyError(reason)
    return value.strip()


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CoinbaseReadOnlyError(reason)
    return value.astimezone(UTC)


def _decimal(value: Decimal | str, reason: str) -> Decimal:
    try:
        result = parse_decimal(value)
    except ValueError as error:
        raise CoinbaseReadOnlyError(reason) from error
    if result < 0:
        raise CoinbaseReadOnlyError(reason)
    return result


def validate_coinbase_private_read_request(
    *, method: str, host: str, path: str, endpoint_class: CoinbaseEndpointClass
) -> None:
    """Reject every non-GET, non-production-host, or non-allowlisted path pre-I/O."""

    if method != "GET" or host != COINBASE_PRIVATE_HOST or "?" in path or "#" in path:
        raise CoinbaseReadOnlyError("COINBASE_PRIVATE_REQUEST_NOT_ALLOWLISTED")
    if endpoint_class is CoinbaseEndpointClass.PRODUCT_RESTRICTIONS_READ:
        product_id = path.removeprefix(_PRODUCT_PREFIX)
        if (
            not path.startswith(_PRODUCT_PREFIX)
            or not product_id
            or "/" in product_id
            or len(product_id) > 40
            or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for character in product_id
            )
        ):
            raise CoinbaseReadOnlyError("COINBASE_PRIVATE_REQUEST_NOT_ALLOWLISTED")
        return
    if _EXACT_PATHS.get(endpoint_class) != path:
        raise CoinbaseReadOnlyError("COINBASE_PRIVATE_REQUEST_NOT_ALLOWLISTED")


@dataclass(frozen=True, slots=True)
class CoinbaseReadApproval:
    approval_id: str
    provider: Provider
    credential_reference_id: str
    account_purpose: str
    allowed_endpoint_classes: tuple[CoinbaseEndpointClass, ...]
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
    transfer_allowed: bool
    withdrawal_allowed: bool
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
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
    ) -> CoinbaseReadApproval:
        endpoints = tuple(sorted(set(allowed_endpoint_classes), key=lambda item: item.value))
        start = _time(approved_at, "COINBASE_APPROVAL_TIME_INVALID")
        end = _time(expires_at, "COINBASE_APPROVAL_EXPIRATION_INVALID")
        if (
            not endpoints
            or CoinbaseEndpointClass.PERMISSIONS_READ not in endpoints
            or not set(endpoints)
            & {
                CoinbaseEndpointClass.ACCOUNT_STATUS_READ,
                CoinbaseEndpointClass.BALANCES_READ,
            }
            or not 1 <= maximum_request_count <= 10
            or not 1 <= timeout_seconds <= 30
            or end <= start
        ):
            raise CoinbaseReadOnlyError("COINBASE_READ_APPROVAL_SCOPE_INVALID")
        values = (
            _id(approval_id, "COINBASE_APPROVAL_ID_INVALID"),
            Provider.COINBASE,
            _id(credential_reference_id, "COINBASE_APPROVAL_CREDENTIAL_INVALID"),
            _id(account_purpose, "COINBASE_APPROVAL_PURPOSE_INVALID"),
            endpoints,
            maximum_request_count,
            timeout_seconds,
            start,
            end,
            _id(actor, "COINBASE_APPROVAL_ACTOR_INVALID"),
            _id(reason, "COINBASE_APPROVAL_REASON_INVALID"),
            _id(idempotency_key, "COINBASE_APPROVAL_IDEMPOTENCY_INVALID"),
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
            content_digest((COINBASE_PRIVATE_READ_APPROVAL_V1, *values)),
            values[-1],
        )

    def active_at(self, value: datetime) -> bool:
        moment = _time(value, "COINBASE_APPROVAL_EVALUATION_TIME_INVALID")
        return self.approved_at <= moment < self.expires_at


@dataclass(frozen=True, slots=True)
class CoinbaseRequestEvidence:
    request_id: str
    endpoint_class: CoinbaseEndpointClass
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
        endpoint_class: CoinbaseEndpointClass,
        path: str,
        response_status: int,
        response_hash: str,
        requested_at: datetime,
        completed_at: datetime,
        response_bytes: int,
    ) -> CoinbaseRequestEvidence:
        validate_coinbase_private_read_request(
            method="GET", host=COINBASE_PRIVATE_HOST, path=path, endpoint_class=endpoint_class
        )
        if (
            not 100 <= response_status <= 599
            or len(response_hash) != 64
            or any(character not in "0123456789abcdef" for character in response_hash)
            or not 0 <= response_bytes <= MAX_PRIVATE_RESPONSE_BYTES
        ):
            raise CoinbaseReadOnlyError("COINBASE_REQUEST_EVIDENCE_INVALID")
        requested = _time(requested_at, "COINBASE_REQUEST_TIME_INVALID")
        completed = _time(completed_at, "COINBASE_RESPONSE_TIME_INVALID")
        if completed < requested:
            raise CoinbaseReadOnlyError("COINBASE_REQUEST_TIME_INVALID")
        return cls(
            _id(request_id, "COINBASE_REQUEST_ID_INVALID"),
            endpoint_class,
            "GET",
            COINBASE_PRIVATE_HOST,
            path,
            response_status,
            response_hash,
            requested,
            completed,
            response_bytes,
        )


@dataclass(frozen=True, slots=True)
class CoinbaseBalanceEvidence:
    currency: str
    available_amount: Decimal
    held_amount: Decimal | None
    account_status: str
    observed_at: datetime
    source_hash: str

    @classmethod
    def create(
        cls,
        *,
        currency: str,
        available_amount: Decimal | str,
        held_amount: Decimal | str | None,
        account_status: str,
        observed_at: datetime,
        source_hash: str,
    ) -> CoinbaseBalanceEvidence:
        if len(source_hash) != 64:
            raise CoinbaseReadOnlyError("COINBASE_BALANCE_SOURCE_HASH_INVALID")
        return cls(
            _id(currency, "COINBASE_BALANCE_CURRENCY_INVALID", 40),
            _decimal(available_amount, "COINBASE_BALANCE_AVAILABLE_INVALID"),
            None if held_amount is None else _decimal(held_amount, "COINBASE_BALANCE_HELD_INVALID"),
            _id(account_status, "COINBASE_BALANCE_STATUS_INVALID", 80),
            _time(observed_at, "COINBASE_BALANCE_TIME_INVALID"),
            source_hash,
        )


@dataclass(frozen=True, slots=True)
class CoinbaseProductConstraint:
    product_id: str
    base_currency: str
    quote_currency: str
    base_increment: Decimal
    quote_increment: Decimal
    minimum_base_size: Decimal
    minimum_quote_size: Decimal
    product_available: bool
    account_restrictions: tuple[str, ...]
    source_hash: str

    @classmethod
    def create(
        cls,
        *,
        product_id: str,
        base_currency: str,
        quote_currency: str,
        base_increment: Decimal | str,
        quote_increment: Decimal | str,
        minimum_base_size: Decimal | str,
        minimum_quote_size: Decimal | str,
        product_available: bool,
        account_restrictions: Sequence[str],
        source_hash: str,
    ) -> CoinbaseProductConstraint:
        if len(source_hash) != 64:
            raise CoinbaseReadOnlyError("COINBASE_PRODUCT_SOURCE_HASH_INVALID")
        values = (
            _id(product_id, "COINBASE_PRODUCT_ID_INVALID", 40),
            _id(base_currency, "COINBASE_BASE_CURRENCY_INVALID", 40),
            _id(quote_currency, "COINBASE_QUOTE_CURRENCY_INVALID", 40),
            _decimal(base_increment, "COINBASE_BASE_INCREMENT_INVALID"),
            _decimal(quote_increment, "COINBASE_QUOTE_INCREMENT_INVALID"),
            _decimal(minimum_base_size, "COINBASE_MINIMUM_BASE_INVALID"),
            _decimal(minimum_quote_size, "COINBASE_MINIMUM_QUOTE_INVALID"),
            product_available,
            tuple(
                sorted(
                    {
                        _id(item, "COINBASE_PRODUCT_RESTRICTION_INVALID")
                        for item in account_restrictions
                    }
                )
            ),
            source_hash,
        )
        if min(values[3:7]) <= 0:
            raise CoinbaseReadOnlyError("COINBASE_PRODUCT_CONSTRAINT_NONPOSITIVE")
        return cls(*values)


@dataclass(frozen=True, slots=True)
class CoinbaseCapabilityCapture:
    capture_id: str
    approval_id: str
    credential_reference_id: str
    account_purpose: str
    redacted_account_fingerprint: str
    account_status: ProviderAccountStatus
    account_type: str
    permission_evidence: tuple[tuple[str, str], ...]
    capability_classes: tuple[CoinbaseCapabilityClass, ...]
    balances: tuple[CoinbaseBalanceEvidence, ...]
    maker_fee_rate: Decimal | None
    taker_fee_rate: Decimal | None
    fee_tier: str | None
    fee_limitations: tuple[str, ...]
    products: tuple[CoinbaseProductConstraint, ...]
    requests: tuple[CoinbaseRequestEvidence, ...]
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
        approval: CoinbaseReadApproval,
        account_purpose: str,
        redacted_account_fingerprint: str,
        account_status: ProviderAccountStatus,
        account_type: str,
        permission_evidence: Sequence[tuple[str, str]],
        balances: Sequence[CoinbaseBalanceEvidence],
        maker_fee_rate: Decimal | str | None,
        taker_fee_rate: Decimal | str | None,
        fee_tier: str | None,
        fee_limitations: Sequence[str],
        products: Sequence[CoinbaseProductConstraint],
        requests: Sequence[CoinbaseRequestEvidence],
        observed_at: datetime,
        fresh_until: datetime,
        redaction_proof: Sequence[str],
        limitations: Sequence[str],
    ) -> CoinbaseCapabilityCapture:
        if account_purpose != approval.account_purpose:
            raise CoinbaseReadOnlyError("COINBASE_CAPTURE_APPROVAL_PURPOSE_MISMATCH")
        normalized_permissions = tuple(
            sorted(
                (
                    _id(key, "COINBASE_PERMISSION_KEY_INVALID"),
                    _id(value, "COINBASE_PERMISSION_VALUE_INVALID"),
                )
                for key, value in permission_evidence
            )
        )
        permission_map = dict(normalized_permissions)
        classes: set[CoinbaseCapabilityClass] = set()
        if permission_map.get("can_view") == "EXPLICIT_TRUE":
            classes.add(CoinbaseCapabilityClass.PRIVATE_VIEW_AVAILABLE)
        if permission_map.get("can_trade") == "EXPLICIT_TRUE":
            classes.add(CoinbaseCapabilityClass.LIVE_TRADE_PERMISSION_AVAILABLE)
        if permission_map.get("can_transfer") == "EXPLICIT_TRUE":
            classes.update(
                (
                    CoinbaseCapabilityClass.TRANSFER_PERMISSION_AVAILABLE,
                    CoinbaseCapabilityClass.WITHDRAWAL_PERMISSION_AVAILABLE,
                )
            )
        if permission_map.get("preview") == "EXPLICIT_TRUE":
            classes.add(CoinbaseCapabilityClass.ORDER_PREVIEW_AVAILABLE)
        if CoinbaseCapabilityClass.PRIVATE_VIEW_AVAILABLE not in classes:
            raise CoinbaseReadOnlyError("COINBASE_PRIVATE_VIEW_NOT_EXPLICIT")
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
            raise CoinbaseReadOnlyError("COINBASE_CAPTURE_REQUEST_SCOPE_INVALID")
        observed = _time(observed_at, "COINBASE_CAPTURE_TIME_INVALID")
        fresh = _time(fresh_until, "COINBASE_CAPTURE_FRESHNESS_INVALID")
        if fresh <= observed:
            raise CoinbaseReadOnlyError("COINBASE_CAPTURE_FRESHNESS_INVALID")
        proofs = tuple(
            sorted({_id(item, "COINBASE_REDACTION_PROOF_INVALID") for item in redaction_proof})
        )
        required_proofs = {
            "AUTHORIZATION_HEADER_NOT_RETAINED",
            "SECRET_VALUES_NOT_RETAINED",
            "RAW_PAYLOAD_NOT_RETAINED",
        }
        if not required_proofs.issubset(proofs):
            raise CoinbaseReadOnlyError("COINBASE_REDACTION_PROOF_INCOMPLETE")
        warnings = set()
        if CoinbaseCapabilityClass.LIVE_TRADE_PERMISSION_AVAILABLE in classes:
            warnings.add("UNEXPECTED_TRADE_PERMISSION_PRESENT")
        if CoinbaseCapabilityClass.ORDER_PREVIEW_AVAILABLE in classes:
            warnings.add("UNEXPECTED_ORDER_PREVIEW_PERMISSION_PRESENT")
        if CoinbaseCapabilityClass.TRANSFER_PERMISSION_AVAILABLE in classes:
            warnings.add("DANGEROUS_TRANSFER_AND_WITHDRAWAL_PERMISSION_PRESENT")
        if account_status is ProviderAccountStatus.RESTRICTED:
            warnings.add("COINBASE_ACCOUNT_RESTRICTED")
        if maker_fee_rate is None or taker_fee_rate is None:
            warnings.add("COINBASE_FEE_EVIDENCE_MISSING")
        if not products:
            warnings.add("COINBASE_PRODUCT_EVIDENCE_INSUFFICIENT")
        normalized_balances = tuple(sorted(balances, key=lambda item: item.currency))
        normalized_products = tuple(sorted(products, key=lambda item: item.product_id))
        values = (
            _id(capture_id, "COINBASE_CAPTURE_ID_INVALID"),
            approval.approval_id,
            approval.credential_reference_id,
            account_purpose,
            _id(redacted_account_fingerprint, "COINBASE_ACCOUNT_FINGERPRINT_INVALID"),
            account_status,
            _id(account_type, "COINBASE_ACCOUNT_TYPE_INVALID"),
            normalized_permissions,
            tuple(sorted(classes, key=lambda item: item.value)),
            normalized_balances,
            None
            if maker_fee_rate is None
            else _decimal(maker_fee_rate, "COINBASE_MAKER_FEE_INVALID"),
            None
            if taker_fee_rate is None
            else _decimal(taker_fee_rate, "COINBASE_TAKER_FEE_INVALID"),
            None if fee_tier is None else _id(fee_tier, "COINBASE_FEE_TIER_INVALID"),
            tuple(
                sorted({_id(item, "COINBASE_FEE_LIMITATION_INVALID") for item in fee_limitations})
            ),
            normalized_products,
            request_values,
            observed,
            fresh,
            proofs,
            tuple(
                sorted({_id(item, "COINBASE_CAPTURE_LIMITATION_INVALID") for item in limitations})
            ),
            tuple(sorted(warnings)),
            len(request_values),
            0,
            "NONE",
            "NONE",
            "NONE",
        )
        assert_secret_free(values)
        return cls(
            *values[:21], content_digest((COINBASE_READ_ONLY_CAPABILITY_V1, *values)), *values[21:]
        )

    @property
    def permissions(self) -> tuple[PermissionClass, ...]:
        classes = set(self.capability_classes)
        values = {PermissionClass.PRIVATE_VIEW}
        if CoinbaseCapabilityClass.ORDER_PREVIEW_AVAILABLE in classes:
            values.add(PermissionClass.ORDER_PREVIEW)
        if CoinbaseCapabilityClass.LIVE_TRADE_PERMISSION_AVAILABLE in classes:
            values.add(PermissionClass.TRADE)
        if CoinbaseCapabilityClass.TRANSFER_PERMISSION_AVAILABLE in classes:
            values.add(PermissionClass.TRANSFER)
        if CoinbaseCapabilityClass.WITHDRAWAL_PERMISSION_AVAILABLE in classes:
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
        if self.fee_tier is not None:
            fee_values.append(("FEE_TIER", self.fee_tier))
        fees = tuple(fee_values)
        precision = tuple(
            (f"{item.product_id}:{name}", canonical_decimal(value))
            for item in self.products
            for name, value in (
                ("BASE_INCREMENT", item.base_increment),
                ("QUOTE_INCREMENT", item.quote_increment),
            )
        )
        minimums = tuple(
            (f"{item.product_id}:{name}", canonical_decimal(value))
            for item in self.products
            for name, value in (
                ("MINIMUM_BASE_SIZE", item.minimum_base_size),
                ("MINIMUM_QUOTE_SIZE", item.minimum_quote_size),
            )
        )
        return ProviderCapabilitySnapshot.create(
            snapshot_id=snapshot_id,
            version=1,
            provider=Provider.COINBASE,
            account_scope_id="coinbase-default",
            credential_reference_id=self.credential_reference_id,
            account_environment=AccountEnvironment.LIVE,
            authenticated_read_status=AuthenticatedReadStatus.AVAILABLE,
            permissions=self.permissions,
            supported_products=("SPOT",) if self.products else (),
            restrictions=tuple(
                sorted(
                    {
                        restriction
                        for product in self.products
                        for restriction in product.account_restrictions
                    }
                )
            ),
            balances=tuple((item.currency, item.available_amount) for item in self.balances),
            fee_evidence=fees,
            precision_constraints=precision,
            minimum_size_constraints=minimums,
            account_status=self.account_status,
            observed_at=self.observed_at,
            ingested_at=ingested_at,
            evidence_class="GENUINE_PRIVATE_READ_ONLY_REDACTED",
            limitations=(COINBASE_READ_ONLY_WARNING, *self.limitations, *self.warnings),
            provider_request_count=self.provider_request_count,
        )


@dataclass(frozen=True, slots=True)
class CoinbaseInspectionAttempt:
    attempt_id: str
    approval_id: str | None
    credential_reference_id: str | None
    status: CoinbaseInspectionStatus
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
        status: CoinbaseInspectionStatus,
        blocked_reason: str | None,
        capture_id: str | None,
        request_count: int,
        warnings: Sequence[str],
        attempted_at: datetime,
    ) -> CoinbaseInspectionAttempt:
        if request_count < 0 or (status is CoinbaseInspectionStatus.SUCCEEDED) != (
            capture_id is not None
        ):
            raise CoinbaseReadOnlyError("COINBASE_INSPECTION_ATTEMPT_INVALID")
        values = (
            _id(attempt_id, "COINBASE_ATTEMPT_ID_INVALID"),
            approval_id,
            credential_reference_id,
            status,
            blocked_reason,
            capture_id,
            request_count,
            0,
            tuple(sorted({_id(item, "COINBASE_ATTEMPT_WARNING_INVALID") for item in warnings})),
            _time(attempted_at, "COINBASE_ATTEMPT_TIME_INVALID"),
            "NONE",
        )
        assert_secret_free(values)
        return cls(*values[:-1], content_digest(values), values[-1])


@dataclass(frozen=True, slots=True)
class CoinbaseCapabilityRefresh:
    refresh_id: str
    due: bool
    disposition: CoinbaseRefreshDisposition
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
        disposition: CoinbaseRefreshDisposition,
        reasons: Sequence[str],
        approval_id: str | None,
        request_count: int,
        output_id: str | None,
        status: str,
        warnings: Sequence[str],
        evaluated_at: datetime,
    ) -> CoinbaseCapabilityRefresh:
        if request_count < 0 or (
            disposition is CoinbaseRefreshDisposition.COMPLETED and output_id is None
        ):
            raise CoinbaseReadOnlyError("COINBASE_REFRESH_INVALID")
        values = (
            _id(refresh_id, "COINBASE_REFRESH_ID_INVALID"),
            due,
            disposition,
            tuple(sorted({_id(item, "COINBASE_REFRESH_REASON_INVALID") for item in reasons})),
            approval_id,
            request_count,
            output_id,
            _id(status, "COINBASE_REFRESH_STATUS_INVALID"),
            tuple(sorted({_id(item, "COINBASE_REFRESH_WARNING_INVALID") for item in warnings})),
            _time(evaluated_at, "COINBASE_REFRESH_TIME_INVALID"),
            0,
            "NONE",
        )
        return cls(
            *values[:10], content_digest((COINBASE_READ_ONLY_REFRESH_V1, *values)), *values[10:]
        )
