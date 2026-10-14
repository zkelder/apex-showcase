"""Pure provider-security and LIVE-readiness evidence rules.

This module deliberately owns no credential resolver, provider transport, order
path, capital authority, or mutable financial state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.values import content_digest, parse_decimal

PROVIDER_CREDENTIAL_REFERENCE_V1 = "PROVIDER_CREDENTIAL_REFERENCE_V1"
PROVIDER_ACCOUNT_CAPABILITY_V1 = "PROVIDER_ACCOUNT_CAPABILITY_V1"
LIVE_RISK_POLICY_V1 = "LIVE_RISK_POLICY_V1"
EXECUTION_CONTROL_POLICY_V1 = "EXECUTION_CONTROL_POLICY_V1"
BACKUP_READINESS_POLICY_V1 = "BACKUP_READINESS_POLICY_V1"
LIVE_READINESS_POLICY_V1 = "LIVE_READINESS_POLICY_V1"
LIVE_READINESS_EXPORT_V1 = "LIVE_READINESS_EXPORT_V1"
LIVE_READINESS_WARNING = "LIVE READINESS IS EVIDENCE ONLY — NO LIVE AUTHORITY"


class LiveReadinessError(ValueError):
    """Provider-security or readiness evidence failed closed."""


class Provider(StrEnum):
    COINBASE = "COINBASE"
    KALSHI = "KALSHI"
    ALPACA = "ALPACA"


class AccountEnvironment(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class SecretBackend(StrEnum):
    ENVIRONMENT_REFERENCE = "ENVIRONMENT_REFERENCE"
    LOCAL_IGNORED_FILE_REFERENCE = "LOCAL_IGNORED_FILE_REFERENCE"
    OPERATING_SYSTEM_SECRET_REFERENCE = (  # pragma: allowlist secret
        "OPERATING_SYSTEM_SECRET_REFERENCE"  # nosec B105
    )


class CredentialValidationStatus(StrEnum):
    UNVALIDATED = "UNVALIDATED"
    REFERENCE_VALID = "REFERENCE_VALID"
    REFERENCE_INVALID = "REFERENCE_INVALID"
    REVOKED = "REVOKED"


class PermissionClass(StrEnum):
    PUBLIC_DATA = "PUBLIC_DATA"
    PRIVATE_VIEW = "PRIVATE_VIEW"
    ORDER_PREVIEW = "ORDER_PREVIEW"
    TRADE = "TRADE"
    TRANSFER = "TRANSFER"
    WITHDRAWAL = "WITHDRAWAL"


class AuthenticatedReadStatus(StrEnum):
    CREDENTIALS_ABSENT = "CREDENTIALS_ABSENT"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    AVAILABLE = "AVAILABLE"
    AUTHENTICATION_REJECTED = "AUTHENTICATION_REJECTED"
    PERMISSION_UNPROVEN = "PERMISSION_UNPROVEN"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


class ProviderAccountStatus(StrEnum):
    CREDENTIALS_ABSENT = "CREDENTIALS_ABSENT"
    ACTIVE = "ACTIVE"
    RESTRICTED = "RESTRICTED"
    UNAVAILABLE = "UNAVAILABLE"


class ExecutionControlState(StrEnum):
    DISABLED = "DISABLED"
    PAPER_ONLY = "PAPER_ONLY"
    LIVE_BLOCKED = "LIVE_BLOCKED"
    LIVE_PILOT_ARMED = "LIVE_PILOT_ARMED"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class BackupReadinessStatus(StrEnum):
    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class ReadinessRecommendation(StrEnum):
    LIVE_NOT_READY = "LIVE_NOT_READY"
    LIVE_REVIEW_REQUIRED = "LIVE_REVIEW_REQUIRED"
    LIVE_PILOT_ELIGIBLE = "LIVE_PILOT_ELIGIBLE"
    LIVE_SUSPENDED = "LIVE_SUSPENDED"


class RiskDecisionState(StrEnum):
    PASS = "PASS"  # nosec B105
    BLOCK = "BLOCK"


class LiveReadinessTaskType(StrEnum):
    CREDENTIAL_REFERENCE_HEALTH = "CREDENTIAL_REFERENCE_HEALTH"
    READ_ONLY_CAPABILITY_FRESHNESS = "READ_ONLY_CAPABILITY_FRESHNESS"
    EXECUTION_CONTROL_STATE = "EXECUTION_CONTROL_STATE"
    LIVE_RISK_STATUS = "LIVE_RISK_STATUS"
    READINESS_ASSESSMENT_FRESHNESS = "READINESS_ASSESSMENT_FRESHNESS"
    BACKUP_READINESS_STATUS = "BACKUP_READINESS_STATUS"
    COINBASE_READ_ONLY_CAPABILITY_REFRESH = "COINBASE_READ_ONLY_CAPABILITY_REFRESH"
    KALSHI_READ_ONLY_CAPABILITY_REFRESH = "KALSHI_READ_ONLY_CAPABILITY_REFRESH"


class LiveReadinessTaskDisposition(StrEnum):
    COMPLETED = "COMPLETED"
    CREDENTIALS_ABSENT = "CREDENTIALS_ABSENT"
    BLOCKED = "BLOCKED"
    STALE = "STALE"


_SENSITIVE_FRAGMENTS = (
    "api_key",
    "apikey",
    "client_secret",
    "secret_value",
    "passphrase",
    "password",
    "private_key",
    "authorization",
    "bearer",
    "token",
    "credential_value",
    "backend_reference",
)
_SENSITIVE_TEXT = (
    "-----BEGIN PRIVATE KEY-----",  # pragma: allowlist secret
    "-----BEGIN RSA PRIVATE KEY-----",  # pragma: allowlist secret
    "Bearer ",
)


def _id(value: str, reason: str, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise LiveReadinessError(reason)
    return value.strip()


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LiveReadinessError(reason)
    return value.astimezone(UTC)


def _decimal(value: Decimal | str, reason: str) -> Decimal:
    try:
        return parse_decimal(value)
    except ValueError as error:
        raise LiveReadinessError(reason) from error


def _is_sensitive_key(value: str) -> bool:
    normalized = value.casefold().replace("-", "_")
    if normalized in {"secret_backend", "secret_values_retained", "secrets_included"}:
        return False
    return normalized == "secret" or any(
        fragment in normalized for fragment in _SENSITIVE_FRAGMENTS
    )


def redact_text(value: str, *, secret_values: Sequence[str] = ()) -> str:
    """Redact known secret values and credential-shaped text before output."""

    redacted = value
    for marker in _SENSITIVE_TEXT:
        if marker in redacted:
            return "[REDACTED]"
    for secret in secret_values:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def redact_payload(value: object, *, secret_values: Sequence[str] = ()) -> object:
    """Recursively sanitize provider-shaped structures at the backend boundary."""

    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _is_sensitive_key(str(key))
                else redact_payload(item, secret_values=secret_values)
            )
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return tuple(redact_payload(item, secret_values=secret_values) for item in value)
    if isinstance(value, str):
        return redact_text(value, secret_values=secret_values)
    return value


def assert_secret_free(value: object) -> None:
    """Reject sensitive keys or recognizable secret material before retention."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                raise LiveReadinessError("LIVE_READINESS_SECRET_RETENTION_PROHIBITED")
            assert_secret_free(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            assert_secret_free(item)
    elif isinstance(value, str) and any(marker in value for marker in _SENSITIVE_TEXT):
        raise LiveReadinessError("LIVE_READINESS_SECRET_RETENTION_PROHIBITED")


@dataclass(frozen=True, slots=True)
class CredentialReference:
    reference_id: str
    version: int
    provider: Provider
    account_environment: AccountEnvironment
    account_purpose: str
    required_permissions: tuple[PermissionClass, ...]
    secret_backend: SecretBackend
    backend_reference_id: str
    created_at: datetime
    rotated_at: datetime | None
    validation_status: CredentialValidationStatus
    last_successful_read_at: datetime | None
    warnings: tuple[str, ...]
    previous_reference_id: str | None
    canonical_hash: str
    authority: str = "NONE"
    secret_values_retained: bool = False

    @classmethod
    def create(
        cls,
        *,
        reference_id: str,
        version: int,
        provider: Provider,
        account_environment: AccountEnvironment,
        account_purpose: str,
        required_permissions: Sequence[PermissionClass],
        secret_backend: SecretBackend,
        backend_reference_id: str,
        created_at: datetime,
        rotated_at: datetime | None = None,
        validation_status: CredentialValidationStatus = CredentialValidationStatus.UNVALIDATED,
        last_successful_read_at: datetime | None = None,
        warnings: Sequence[str] = (),
        previous_reference_id: str | None = None,
    ) -> CredentialReference:
        if version <= 0:
            raise LiveReadinessError("CREDENTIAL_REFERENCE_VERSION_INVALID")
        permissions = tuple(sorted(set(required_permissions), key=lambda item: item.value))
        if not permissions:
            raise LiveReadinessError("CREDENTIAL_REFERENCE_PERMISSIONS_REQUIRED")
        backend_id = _id(backend_reference_id, "CREDENTIAL_BACKEND_REFERENCE_INVALID")
        if any(value in backend_id.casefold() for value in ("/", "\\", "-----begin", "bearer ")):
            raise LiveReadinessError("CREDENTIAL_BACKEND_REFERENCE_MUST_BE_OPAQUE")
        values = (
            _id(reference_id, "CREDENTIAL_REFERENCE_ID_INVALID"),
            version,
            provider,
            account_environment,
            _id(account_purpose, "CREDENTIAL_ACCOUNT_PURPOSE_INVALID"),
            permissions,
            secret_backend,
            backend_id,
            _time(created_at, "CREDENTIAL_REFERENCE_TIME_INVALID"),
            None if rotated_at is None else _time(rotated_at, "CREDENTIAL_ROTATION_TIME_INVALID"),
            validation_status,
            None
            if last_successful_read_at is None
            else _time(last_successful_read_at, "CREDENTIAL_LAST_USE_TIME_INVALID"),
            tuple(sorted({_id(item, "CREDENTIAL_WARNING_INVALID") for item in warnings})),
            None
            if previous_reference_id is None
            else _id(previous_reference_id, "CREDENTIAL_PREVIOUS_REFERENCE_INVALID"),
            "NONE",
            False,
        )
        return cls(
            *values[:-2], content_digest((PROVIDER_CREDENTIAL_REFERENCE_V1, *values)), *values[-2:]
        )


@dataclass(frozen=True, slots=True)
class ProviderCapabilitySnapshot:
    snapshot_id: str
    version: int
    provider: Provider
    account_scope_id: str
    credential_reference_id: str | None
    account_environment: AccountEnvironment
    authenticated_read_status: AuthenticatedReadStatus
    permissions: tuple[PermissionClass, ...]
    supported_products: tuple[str, ...]
    restrictions: tuple[str, ...]
    balances: tuple[tuple[str, Decimal], ...]
    fee_evidence: tuple[tuple[str, str], ...]
    precision_constraints: tuple[tuple[str, str], ...]
    minimum_size_constraints: tuple[tuple[str, str], ...]
    order_types: tuple[str, ...]
    preview_available: bool
    trade_available: bool
    transfer_available: bool
    withdrawal_available: bool
    account_status: ProviderAccountStatus
    observed_at: datetime
    ingested_at: datetime
    evidence_class: str
    limitations: tuple[str, ...]
    provider_request_count: int
    canonical_hash: str
    authority: str = "NONE"
    provider_write_capability: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: str,
        version: int,
        provider: Provider,
        account_scope_id: str,
        credential_reference_id: str | None,
        account_environment: AccountEnvironment,
        authenticated_read_status: AuthenticatedReadStatus,
        permissions: Sequence[PermissionClass],
        supported_products: Sequence[str] = (),
        restrictions: Sequence[str] = (),
        balances: Sequence[tuple[str, Decimal | str]] = (),
        fee_evidence: Sequence[tuple[str, str]] = (),
        precision_constraints: Sequence[tuple[str, str]] = (),
        minimum_size_constraints: Sequence[tuple[str, str]] = (),
        order_types: Sequence[str] = (),
        account_status: ProviderAccountStatus,
        observed_at: datetime,
        ingested_at: datetime,
        evidence_class: str,
        limitations: Sequence[str] = (),
        provider_request_count: int = 0,
    ) -> ProviderCapabilitySnapshot:
        if version <= 0 or provider_request_count < 0:
            raise LiveReadinessError("PROVIDER_CAPABILITY_VERSION_OR_REQUEST_COUNT_INVALID")
        permission_values = tuple(sorted(set(permissions), key=lambda item: item.value))
        if credential_reference_id is None and (
            authenticated_read_status is not AuthenticatedReadStatus.CREDENTIALS_ABSENT
            or any(item is not PermissionClass.PUBLIC_DATA for item in permission_values)
            or provider_request_count
        ):
            raise LiveReadinessError("PROVIDER_CAPABILITY_CREDENTIALS_ABSENT_CONFLICT")
        if authenticated_read_status is AuthenticatedReadStatus.AVAILABLE and (
            PermissionClass.PRIVATE_VIEW not in permission_values
        ):
            raise LiveReadinessError("PROVIDER_CAPABILITY_PRIVATE_VIEW_UNPROVEN")
        dangerous = tuple(
            item.value
            for item in permission_values
            if item in {PermissionClass.TRANSFER, PermissionClass.WITHDRAWAL}
        )
        normalized_limitations = set(
            _id(item, "PROVIDER_CAPABILITY_LIMITATION_INVALID") for item in limitations
        )
        normalized_limitations.update(f"DANGEROUS_PERMISSION_PRESENT:{item}" for item in dangerous)
        normalized_balances = tuple(
            sorted(
                (
                    _id(currency, "PROVIDER_BALANCE_CURRENCY_INVALID", 40),
                    _decimal(amount, "PROVIDER_BALANCE_INVALID"),
                )
                for currency, amount in balances
            )
        )
        preview = PermissionClass.ORDER_PREVIEW in permission_values
        trade = PermissionClass.TRADE in permission_values
        transfer = PermissionClass.TRANSFER in permission_values
        withdrawal = PermissionClass.WITHDRAWAL in permission_values
        values = (
            _id(snapshot_id, "PROVIDER_CAPABILITY_ID_INVALID"),
            version,
            provider,
            _id(account_scope_id, "PROVIDER_ACCOUNT_SCOPE_INVALID"),
            None
            if credential_reference_id is None
            else _id(credential_reference_id, "PROVIDER_CREDENTIAL_REFERENCE_INVALID"),
            account_environment,
            authenticated_read_status,
            permission_values,
            tuple(sorted({_id(item, "PROVIDER_PRODUCT_INVALID") for item in supported_products})),
            tuple(sorted({_id(item, "PROVIDER_RESTRICTION_INVALID") for item in restrictions})),
            normalized_balances,
            tuple(
                sorted(
                    (_id(key, "PROVIDER_FEE_KEY_INVALID"), _id(value, "PROVIDER_FEE_VALUE_INVALID"))
                    for key, value in fee_evidence
                )
            ),
            tuple(
                sorted(
                    (
                        _id(key, "PROVIDER_PRECISION_KEY_INVALID"),
                        _id(value, "PROVIDER_PRECISION_VALUE_INVALID"),
                    )
                    for key, value in precision_constraints
                )
            ),
            tuple(
                sorted(
                    (
                        _id(key, "PROVIDER_MINIMUM_KEY_INVALID"),
                        _id(value, "PROVIDER_MINIMUM_VALUE_INVALID"),
                    )
                    for key, value in minimum_size_constraints
                )
            ),
            tuple(sorted({_id(item, "PROVIDER_ORDER_TYPE_INVALID") for item in order_types})),
            preview,
            trade,
            transfer,
            withdrawal,
            account_status,
            _time(observed_at, "PROVIDER_CAPABILITY_OBSERVED_TIME_INVALID"),
            _time(ingested_at, "PROVIDER_CAPABILITY_INGESTED_TIME_INVALID"),
            _id(evidence_class, "PROVIDER_EVIDENCE_CLASS_INVALID"),
            tuple(sorted(normalized_limitations)),
            provider_request_count,
            "NONE",
            "NONE",
            "NONE",
        )
        assert_secret_free(values)
        return cls(
            *values[:-3], content_digest((PROVIDER_ACCOUNT_CAPABILITY_V1, *values)), *values[-3:]
        )

    @classmethod
    def credentials_absent(
        cls,
        *,
        snapshot_id: str,
        provider: Provider,
        account_scope_id: str,
        account_environment: AccountEnvironment,
        observed_at: datetime,
        limitations: Sequence[str] = (),
    ) -> ProviderCapabilitySnapshot:
        return cls.create(
            snapshot_id=snapshot_id,
            version=1,
            provider=provider,
            account_scope_id=account_scope_id,
            credential_reference_id=None,
            account_environment=account_environment,
            authenticated_read_status=AuthenticatedReadStatus.CREDENTIALS_ABSENT,
            permissions=(),
            account_status=ProviderAccountStatus.CREDENTIALS_ABSENT,
            observed_at=observed_at,
            ingested_at=observed_at,
            evidence_class="GENUINE_RETAINED_STATE",
            limitations=("PRIVATE_CREDENTIALS_ABSENT", "NO_PROVIDER_REQUEST", *limitations),
        )


@dataclass(frozen=True, slots=True)
class LiveRiskPolicy:
    policy_id: str
    version: int
    allowed_venues: tuple[Provider, ...]
    allowed_account_scopes: tuple[str, ...]
    allowed_products: tuple[str, ...]
    leverage_allowed: bool
    shorting_allowed: bool
    maximum_total_live_capital: Decimal
    minimum_retained_cash_ratio: Decimal
    maximum_per_opportunity: Decimal
    maximum_per_strategy: Decimal
    maximum_per_instrument: Decimal
    maximum_per_venue: Decimal
    maximum_open_orders: int
    maximum_open_positions: int
    maximum_daily_realized_loss: Decimal
    maximum_daily_modeled_loss: Decimal
    maximum_drawdown_ratio: Decimal
    maximum_unresolved_reconciliation_severity: int
    minimum_strategy_health: str
    minimum_paper_sample_count: int
    minimum_clean_fill_count: int
    minimum_settlement_count: int
    capability_freshness_seconds: int
    require_provider_healthy: bool
    require_operating_cycle_healthy: bool
    require_backup_verified: bool
    emergency_stop_behavior: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    capital_authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def v1(
        cls, *, created_at: datetime, allowed_account_scopes: Sequence[str] = ()
    ) -> LiveRiskPolicy:
        values = (
            LIVE_RISK_POLICY_V1,
            1,
            (Provider.COINBASE, Provider.KALSHI, Provider.ALPACA),
            tuple(
                sorted(
                    {
                        _id(item, "LIVE_RISK_ACCOUNT_SCOPE_INVALID")
                        for item in allowed_account_scopes
                    }
                )
            ),
            ("SPOT", "FULLY_DEFINED_RISK"),
            False,
            False,
            Decimal("100"),
            Decimal("0.90"),
            Decimal("25"),
            Decimal("25"),
            Decimal("25"),
            Decimal("50"),
            1,
            1,
            Decimal("10"),
            Decimal("10"),
            Decimal("0.05"),
            0,
            "HEALTHY",
            30,
            20,
            20,
            900,
            True,
            True,
            True,
            "BLOCK_ALL_NEW_PAPER_AND_LIVE_SUBMISSIONS_PRESERVE_READS_AND_RECONCILIATION",
            _time(created_at, "LIVE_RISK_POLICY_TIME_INVALID"),
            "NONE",
            "NONE",
            "NONE",
        )
        return cls(*values[:-3], content_digest(values), *values[-3:])


@dataclass(frozen=True, slots=True)
class ExecutionControlEvent:
    event_id: str
    control_id: str
    ordinal: int
    state: ExecutionControlState
    actor: str
    reason: str
    effective_at: datetime
    expires_at: datetime | None
    previous_state: ExecutionControlState | None
    idempotency_key: str
    canonical_hash: str
    authority: str = "NONE"
    provider_permission_changed: bool = False

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        control_id: str,
        ordinal: int,
        state: ExecutionControlState,
        actor: str,
        reason: str,
        effective_at: datetime,
        expires_at: datetime | None,
        previous_state: ExecutionControlState | None,
        idempotency_key: str,
        fixture: bool = False,
    ) -> ExecutionControlEvent:
        if ordinal <= 0:
            raise LiveReadinessError("EXECUTION_CONTROL_ORDINAL_INVALID")
        if state is ExecutionControlState.LIVE_PILOT_ARMED and not fixture:
            raise LiveReadinessError("LIVE_PILOT_ARMED_UNAVAILABLE_IN_THIS_PHASE")
        if ordinal == 1 and previous_state is not None:
            raise LiveReadinessError("EXECUTION_CONTROL_INITIAL_PREVIOUS_STATE_INVALID")
        if ordinal > 1 and previous_state is None:
            raise LiveReadinessError("EXECUTION_CONTROL_PREVIOUS_STATE_REQUIRED")
        values = (
            _id(event_id, "EXECUTION_CONTROL_EVENT_ID_INVALID"),
            _id(control_id, "EXECUTION_CONTROL_ID_INVALID"),
            ordinal,
            state,
            _id(actor, "EXECUTION_CONTROL_ACTOR_INVALID"),
            _id(reason, "EXECUTION_CONTROL_REASON_INVALID"),
            _time(effective_at, "EXECUTION_CONTROL_TIME_INVALID"),
            None
            if expires_at is None
            else _time(expires_at, "EXECUTION_CONTROL_EXPIRATION_INVALID"),
            previous_state,
            _id(idempotency_key, "EXECUTION_CONTROL_IDEMPOTENCY_KEY_INVALID"),
            "NONE",
            False,
        )
        return cls(
            *values[:-2], content_digest((EXECUTION_CONTROL_POLICY_V1, *values)), *values[-2:]
        )

    @property
    def blocks_new_submission(self) -> bool:
        """Compatibility alias for whether a new LIVE submission is blocked."""

        return self.blocks_new_live_submission

    @property
    def blocks_new_live_submission(self) -> bool:
        return self.state is not ExecutionControlState.LIVE_PILOT_ARMED

    @property
    def blocks_new_paper_submission(self) -> bool:
        return self.state in {
            ExecutionControlState.DISABLED,
            ExecutionControlState.EMERGENCY_STOP,
        }


@dataclass(frozen=True, slots=True)
class BackupReadinessRecord:
    record_id: str
    database_backup_method: str
    latest_verified_backup_at: datetime | None
    restore_test_status: str
    configuration_backup_status: str
    credential_reference_recovery_procedure: str
    recovery_point_objective_seconds: int
    recovery_time_objective_seconds: int
    known_gaps: tuple[str, ...]
    status: BackupReadinessStatus
    assessed_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    secret_values_included: bool = False

    @classmethod
    def create(
        cls,
        *,
        record_id: str,
        database_backup_method: str,
        latest_verified_backup_at: datetime | None,
        restore_test_status: str,
        configuration_backup_status: str,
        credential_reference_recovery_procedure: str,
        recovery_point_objective_seconds: int,
        recovery_time_objective_seconds: int,
        known_gaps: Sequence[str],
        status: BackupReadinessStatus,
        assessed_at: datetime,
    ) -> BackupReadinessRecord:
        if recovery_point_objective_seconds <= 0 or recovery_time_objective_seconds <= 0:
            raise LiveReadinessError("BACKUP_READINESS_OBJECTIVE_INVALID")
        values = (
            _id(record_id, "BACKUP_READINESS_ID_INVALID"),
            _id(database_backup_method, "BACKUP_METHOD_INVALID"),
            None
            if latest_verified_backup_at is None
            else _time(latest_verified_backup_at, "BACKUP_VERIFIED_TIME_INVALID"),
            _id(restore_test_status, "BACKUP_RESTORE_STATUS_INVALID"),
            _id(configuration_backup_status, "BACKUP_CONFIGURATION_STATUS_INVALID"),
            _id(credential_reference_recovery_procedure, "BACKUP_CREDENTIAL_RECOVERY_INVALID"),
            recovery_point_objective_seconds,
            recovery_time_objective_seconds,
            tuple(sorted({_id(item, "BACKUP_GAP_INVALID") for item in known_gaps})),
            status,
            _time(assessed_at, "BACKUP_ASSESSED_TIME_INVALID"),
            "NONE",
            False,
        )
        assert_secret_free(values)
        return cls(
            *values[:-2], content_digest((BACKUP_READINESS_POLICY_V1, *values)), *values[-2:]
        )


@dataclass(frozen=True, slots=True)
class LiveReadinessInputs:
    venue: Provider
    account_scope_id: str
    capability: ProviderCapabilitySnapshot | None
    strategy_or_method_id: str | None
    paper_sample_count: int
    fill_count: int
    settlement_count: int
    total_live_capital: Decimal
    retained_cash_ratio: Decimal
    per_opportunity_exposure: Decimal
    per_strategy_exposure: Decimal
    per_instrument_exposure: Decimal
    per_venue_exposure: Decimal
    open_order_count: int
    open_position_count: int
    daily_realized_loss: Decimal
    daily_modeled_loss: Decimal
    drawdown_ratio: Decimal
    strategy_health_state: str
    reconciliation_quality: str
    unresolved_reconciliation_severity: int
    operating_cycle_health: str
    provider_health: str
    backup: BackupReadinessRecord | None
    control: ExecutionControlEvent
    qualifying_opportunity: bool
    explicit_operator_review: bool
    assessed_at: datetime


@dataclass(frozen=True, slots=True)
class LiveRiskDecision:
    decision_id: str
    policy_id: str
    state: RiskDecisionState
    blockers: tuple[str, ...]
    evaluated_metrics: tuple[tuple[str, str], ...]
    evaluated_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    capital_authority: str = "NONE"


def evaluate_live_risk(
    *, decision_id: str, policy: LiveRiskPolicy, inputs: LiveReadinessInputs
) -> LiveRiskDecision:
    capability = inputs.capability
    blockers: list[str] = []
    if (
        any(
            value < 0
            for value in (
                inputs.total_live_capital,
                inputs.per_opportunity_exposure,
                inputs.per_strategy_exposure,
                inputs.per_instrument_exposure,
                inputs.per_venue_exposure,
                inputs.daily_realized_loss,
                inputs.daily_modeled_loss,
                inputs.drawdown_ratio,
            )
        )
        or not Decimal("0") <= inputs.retained_cash_ratio <= Decimal("1")
        or min(inputs.open_order_count, inputs.open_position_count) < 0
    ):
        blockers.append("LIVE_RISK_INPUT_INVALID")
    if inputs.venue not in policy.allowed_venues:
        blockers.append("VENUE_NOT_ALLOWED")
    if inputs.account_scope_id not in policy.allowed_account_scopes:
        blockers.append("ACCOUNT_SCOPE_NOT_ALLOWED")
    if capability is None:
        blockers.append("PRIVATE_ACCOUNT_CAPABILITY_MISSING")
    else:
        if capability.authenticated_read_status is not AuthenticatedReadStatus.AVAILABLE:
            blockers.append("PRIVATE_ACCOUNT_VIEW_UNAVAILABLE")
        if PermissionClass.TRADE not in capability.permissions:
            blockers.append("TRADE_PERMISSION_NOT_EXPLICITLY_CONFIRMED")
        if capability.account_status is not ProviderAccountStatus.ACTIVE:
            blockers.append("PROVIDER_ACCOUNT_RESTRICTED_OR_UNAVAILABLE")
        if capability.restrictions:
            blockers.append("PRODUCT_OR_ACCOUNT_RESTRICTIONS_PRESENT")
        if capability.transfer_available:
            blockers.append("TRANSFER_PERMISSION_PRESENT")
        if capability.withdrawal_available:
            blockers.append("WITHDRAWAL_PERMISSION_PRESENT")
        if not capability.fee_evidence:
            blockers.append("FEE_EVIDENCE_MISSING")
        if not capability.minimum_size_constraints:
            blockers.append("MINIMUM_SIZE_EVIDENCE_MISSING")
        if not capability.supported_products or any(
            product not in policy.allowed_products for product in capability.supported_products
        ):
            blockers.append("PRODUCT_NOT_ALLOWED_OR_UNPROVEN")
        age = int((inputs.assessed_at - capability.observed_at).total_seconds())
        if age < 0 or age > policy.capability_freshness_seconds:
            blockers.append("PROVIDER_CAPABILITY_STALE")
    if inputs.total_live_capital > policy.maximum_total_live_capital:
        blockers.append("TOTAL_LIVE_CAPITAL_LIMIT_EXCEEDED")
    if inputs.retained_cash_ratio < policy.minimum_retained_cash_ratio:
        blockers.append("MINIMUM_RETAINED_CASH_NOT_MET")
    if inputs.per_opportunity_exposure > policy.maximum_per_opportunity:
        blockers.append("PER_OPPORTUNITY_LIMIT_EXCEEDED")
    if inputs.per_strategy_exposure > policy.maximum_per_strategy:
        blockers.append("PER_STRATEGY_LIMIT_EXCEEDED")
    if inputs.per_instrument_exposure > policy.maximum_per_instrument:
        blockers.append("PER_INSTRUMENT_LIMIT_EXCEEDED")
    if inputs.per_venue_exposure > policy.maximum_per_venue:
        blockers.append("PER_VENUE_LIMIT_EXCEEDED")
    if inputs.open_order_count > policy.maximum_open_orders:
        blockers.append("OPEN_ORDER_LIMIT_EXCEEDED")
    if inputs.open_position_count > policy.maximum_open_positions:
        blockers.append("OPEN_POSITION_LIMIT_EXCEEDED")
    if inputs.daily_realized_loss > policy.maximum_daily_realized_loss:
        blockers.append("DAILY_REALIZED_LOSS_LIMIT_EXCEEDED")
    if inputs.daily_modeled_loss > policy.maximum_daily_modeled_loss:
        blockers.append("DAILY_MODELED_LOSS_LIMIT_EXCEEDED")
    if inputs.drawdown_ratio > policy.maximum_drawdown_ratio:
        blockers.append("DRAWDOWN_LIMIT_EXCEEDED")
    if inputs.strategy_or_method_id is None or not inputs.qualifying_opportunity:
        blockers.append("NO_ELIGIBLE_STRATEGY_OR_CURRENT_OPPORTUNITY")
    if inputs.paper_sample_count < policy.minimum_paper_sample_count:
        blockers.append("INSUFFICIENT_GENUINE_PAPER_SAMPLE")
    if inputs.fill_count < policy.minimum_clean_fill_count:
        blockers.append("INSUFFICIENT_CLEAN_FILLS")
    if inputs.settlement_count < policy.minimum_settlement_count:
        blockers.append("INSUFFICIENT_SETTLEMENTS")
    if inputs.strategy_health_state != policy.minimum_strategy_health:
        blockers.append("STRATEGY_HEALTH_INSUFFICIENT")
    if (
        inputs.reconciliation_quality != "CLEAN"
        or inputs.unresolved_reconciliation_severity
        > policy.maximum_unresolved_reconciliation_severity
    ):
        blockers.append("RECONCILIATION_NOT_CLEAN")
    if policy.require_operating_cycle_healthy and inputs.operating_cycle_health != "HEALTHY":
        blockers.append("OPERATING_CYCLE_UNHEALTHY")
    if policy.require_provider_healthy and inputs.provider_health != "HEALTHY":
        blockers.append("PROVIDER_HEALTH_UNHEALTHY")
    if policy.require_backup_verified and (
        inputs.backup is None or inputs.backup.status is not BackupReadinessStatus.VERIFIED
    ):
        blockers.append("BACKUP_RESTORE_NOT_VERIFIED")
    if inputs.control.state is ExecutionControlState.EMERGENCY_STOP:
        blockers.append("EMERGENCY_STOP_ACTIVE")
    elif inputs.control.state is not ExecutionControlState.LIVE_PILOT_ARMED:
        blockers.append("LIVE_PILOT_NOT_ARMED")
    if not inputs.explicit_operator_review:
        blockers.append("EXPLICIT_OPERATOR_REVIEW_MISSING")
    retained = tuple(sorted(set(blockers)))
    evaluated_metrics = (
        ("DAILY_MODELED_LOSS", str(inputs.daily_modeled_loss)),
        ("DAILY_REALIZED_LOSS", str(inputs.daily_realized_loss)),
        ("DRAWDOWN_RATIO", str(inputs.drawdown_ratio)),
        ("OPEN_ORDER_COUNT", str(inputs.open_order_count)),
        ("OPEN_POSITION_COUNT", str(inputs.open_position_count)),
        ("PER_INSTRUMENT_EXPOSURE", str(inputs.per_instrument_exposure)),
        ("PER_OPPORTUNITY_EXPOSURE", str(inputs.per_opportunity_exposure)),
        ("PER_STRATEGY_EXPOSURE", str(inputs.per_strategy_exposure)),
        ("PER_VENUE_EXPOSURE", str(inputs.per_venue_exposure)),
        ("RETAINED_CASH_RATIO", str(inputs.retained_cash_ratio)),
        ("TOTAL_LIVE_CAPITAL", str(inputs.total_live_capital)),
    )
    values = (
        _id(decision_id, "LIVE_RISK_DECISION_ID_INVALID"),
        policy.policy_id,
        RiskDecisionState.PASS if not retained else RiskDecisionState.BLOCK,
        retained,
        evaluated_metrics,
        _time(inputs.assessed_at, "LIVE_RISK_DECISION_TIME_INVALID"),
        "NONE",
        "NONE",
    )
    return LiveRiskDecision(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class LiveReadinessAssessment:
    assessment_id: str
    policy_id: str
    venue: Provider
    account_scope_id: str
    capability_snapshot_id: str | None
    credential_status: str
    permission_status: str
    strategy_or_method_id: str | None
    paper_sample_count: int
    fill_count: int
    settlement_count: int
    strategy_health_state: str
    reconciliation_quality: str
    operating_cycle_health: str
    backup_readiness_status: str
    execution_control_state: ExecutionControlState
    risk_decision_id: str
    risk_policy_result: RiskDecisionState
    unresolved_blockers: tuple[str, ...]
    recommendation: ReadinessRecommendation
    actor: str
    assessed_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    financial_mutation_capability: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        assessment_id: str,
        policy: LiveRiskPolicy,
        inputs: LiveReadinessInputs,
        decision: LiveRiskDecision,
        actor: str,
    ) -> LiveReadinessAssessment:
        recommendation = (
            ReadinessRecommendation.LIVE_SUSPENDED
            if inputs.control.state is ExecutionControlState.EMERGENCY_STOP
            else ReadinessRecommendation.LIVE_PILOT_ELIGIBLE
            if decision.state is RiskDecisionState.PASS
            else ReadinessRecommendation.LIVE_NOT_READY
        )
        capability = inputs.capability
        values = (
            _id(assessment_id, "LIVE_READINESS_ASSESSMENT_ID_INVALID"),
            policy.policy_id,
            inputs.venue,
            _id(inputs.account_scope_id, "LIVE_READINESS_ACCOUNT_SCOPE_INVALID"),
            None if capability is None else capability.snapshot_id,
            (
                "ABSENT"
                if capability is None
                or capability.authenticated_read_status
                is AuthenticatedReadStatus.CREDENTIALS_ABSENT
                else capability.authenticated_read_status.value
            ),
            "UNPROVEN"
            if capability is None
            else ",".join(item.value for item in capability.permissions),
            inputs.strategy_or_method_id,
            inputs.paper_sample_count,
            inputs.fill_count,
            inputs.settlement_count,
            inputs.strategy_health_state,
            inputs.reconciliation_quality,
            inputs.operating_cycle_health,
            "MISSING" if inputs.backup is None else inputs.backup.status.value,
            inputs.control.state,
            decision.decision_id,
            decision.state,
            decision.blockers,
            recommendation,
            _id(actor, "LIVE_READINESS_ACTOR_INVALID"),
            _time(inputs.assessed_at, "LIVE_READINESS_TIME_INVALID"),
            "NONE",
            "NONE",
            "NONE",
        )
        return cls(*values[:-3], content_digest((LIVE_READINESS_POLICY_V1, *values)), *values[-3:])


@dataclass(frozen=True, slots=True)
class ReadinessExport:
    export_id: str
    assessment_id: str
    categories: tuple[tuple[str, int], ...]
    evidence_ids: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    paper_records: int
    live_records: int
    redactions_applied: tuple[str, ...]
    generated_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    financial_mutation_capability: str = "NONE"
    secrets_included: bool = False

    @classmethod
    def create(
        cls,
        *,
        export_id: str,
        assessment: LiveReadinessAssessment,
        categories: Sequence[tuple[str, int]],
        evidence_ids: Sequence[str],
        evidence_hashes: Sequence[str],
        paper_records: int,
        generated_at: datetime,
    ) -> ReadinessExport:
        allowed_categories = {
            "PROVIDER_CAPABILITIES",
            "READINESS_ASSESSMENTS",
            "RISK_DECISIONS",
            "EXECUTION_CONTROLS",
            "ORDERS_AND_FILLS",
            "POSITIONS_AND_SETTLEMENTS",
            "RECONCILIATION",
            "JOURNALS",
            "AUTHORITY_HISTORY",
            "BACKUP_READINESS",
        }
        normalized = tuple(
            sorted(
                (_id(key, "READINESS_EXPORT_CATEGORY_INVALID"), count) for key, count in categories
            )
        )
        if any(key not in allowed_categories or count < 0 for key, count in normalized):
            raise LiveReadinessError("READINESS_EXPORT_CATEGORY_INVALID")
        values = (
            _id(export_id, "READINESS_EXPORT_ID_INVALID"),
            assessment.assessment_id,
            normalized,
            tuple(
                sorted({_id(item, "READINESS_EXPORT_EVIDENCE_ID_INVALID") for item in evidence_ids})
            ),
            tuple(
                sorted(
                    {
                        _id(item, "READINESS_EXPORT_EVIDENCE_HASH_INVALID", 64)
                        for item in evidence_hashes
                    }
                )
            ),
            paper_records,
            0,
            (
                "AUTHENTICATION_HEADERS",
                "CREDENTIAL_VALUES",
                "PRIVATE_KEY_MATERIAL",
                "RAW_ACCOUNT_IDENTIFIERS",
                "SECRET_FILE_PATHS",
            ),
            _time(generated_at, "READINESS_EXPORT_TIME_INVALID"),
            "NONE",
            "NONE",
            False,
        )
        assert_secret_free(values)
        return cls(*values[:-3], content_digest((LIVE_READINESS_EXPORT_V1, *values)), *values[-3:])


@dataclass(frozen=True, slots=True)
class LiveReadinessOperatingTask:
    task_type: LiveReadinessTaskType
    disposition: LiveReadinessTaskDisposition
    evidence_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    provider_request_count: int
    provider_write_count: int
    authority: str = "NONE"


def build_live_readiness_operating_tasks(
    *,
    credential_references: Sequence[CredentialReference],
    capabilities: Sequence[ProviderCapabilitySnapshot],
    control: ExecutionControlEvent | None,
    policy: LiveRiskPolicy | None,
    assessment: LiveReadinessAssessment | None,
    backup: BackupReadinessRecord | None,
    as_of: datetime,
) -> tuple[LiveReadinessOperatingTask, ...]:
    """Build seven bounded read-only cycle tasks; credentials-absent is non-failing."""

    now = _time(as_of, "LIVE_READINESS_OPERATING_TIME_INVALID")
    has_references = bool(credential_references)
    fresh_capabilities = tuple(
        item for item in capabilities if 0 <= (now - item.observed_at).total_seconds() <= 900
    )
    tasks = (
        LiveReadinessOperatingTask(
            LiveReadinessTaskType.CREDENTIAL_REFERENCE_HEALTH,
            LiveReadinessTaskDisposition.COMPLETED
            if has_references
            else LiveReadinessTaskDisposition.CREDENTIALS_ABSENT,
            tuple(item.reference_id for item in credential_references),
            () if has_references else ("NO_CREDENTIAL_REFERENCES_CONFIGURED",),
            0,
            0,
        ),
        LiveReadinessOperatingTask(
            LiveReadinessTaskType.READ_ONLY_CAPABILITY_FRESHNESS,
            LiveReadinessTaskDisposition.COMPLETED
            if fresh_capabilities
            else LiveReadinessTaskDisposition.CREDENTIALS_ABSENT
            if not has_references
            else LiveReadinessTaskDisposition.STALE,
            tuple(item.snapshot_id for item in fresh_capabilities),
            () if fresh_capabilities else ("NO_DUE_APPROVED_READ_ONLY_PRIVATE_INSPECTION",),
            0,
            0,
        ),
        LiveReadinessOperatingTask(
            LiveReadinessTaskType.EXECUTION_CONTROL_STATE,
            LiveReadinessTaskDisposition.COMPLETED
            if control is not None
            else LiveReadinessTaskDisposition.BLOCKED,
            () if control is None else (control.event_id,),
            ("EXECUTION_CONTROL_MISSING",) if control is None else (),
            0,
            0,
        ),
        LiveReadinessOperatingTask(
            LiveReadinessTaskType.LIVE_RISK_STATUS,
            LiveReadinessTaskDisposition.COMPLETED
            if policy is not None
            else LiveReadinessTaskDisposition.BLOCKED,
            () if policy is None else (policy.policy_id,),
            ("LIVE_RISK_POLICY_MISSING",) if policy is None else (),
            0,
            0,
        ),
        LiveReadinessOperatingTask(
            LiveReadinessTaskType.READINESS_ASSESSMENT_FRESHNESS,
            LiveReadinessTaskDisposition.COMPLETED
            if assessment is not None
            and 0 <= (now - assessment.assessed_at).total_seconds() <= 86_400
            else LiveReadinessTaskDisposition.STALE,
            () if assessment is None else (assessment.assessment_id,),
            ("READINESS_ASSESSMENT_MISSING_OR_STALE",)
            if assessment is None
            or not 0 <= (now - assessment.assessed_at).total_seconds() <= 86_400
            else (),
            0,
            0,
        ),
        LiveReadinessOperatingTask(
            LiveReadinessTaskType.BACKUP_READINESS_STATUS,
            LiveReadinessTaskDisposition.COMPLETED
            if backup is not None and backup.status is BackupReadinessStatus.VERIFIED
            else LiveReadinessTaskDisposition.BLOCKED,
            () if backup is None else (backup.record_id,),
            ("BACKUP_RESTORE_NOT_VERIFIED",)
            if backup is None or backup.status is not BackupReadinessStatus.VERIFIED
            else (),
            0,
            0,
        ),
        LiveReadinessOperatingTask(
            LiveReadinessTaskType.COINBASE_READ_ONLY_CAPABILITY_REFRESH,
            (
                LiveReadinessTaskDisposition.COMPLETED
                if any(
                    item.provider is Provider.COINBASE
                    and item.authenticated_read_status is AuthenticatedReadStatus.AVAILABLE
                    for item in fresh_capabilities
                )
                else LiveReadinessTaskDisposition.CREDENTIALS_ABSENT
                if not any(item.provider is Provider.COINBASE for item in credential_references)
                else LiveReadinessTaskDisposition.BLOCKED
            ),
            tuple(
                item.snapshot_id
                for item in fresh_capabilities
                if item.provider is Provider.COINBASE
                and item.authenticated_read_status is AuthenticatedReadStatus.AVAILABLE
            ),
            (
                ()
                if any(
                    item.provider is Provider.COINBASE
                    and item.authenticated_read_status is AuthenticatedReadStatus.AVAILABLE
                    for item in fresh_capabilities
                )
                else ("PRIVATE_CREDENTIALS_ABSENT",)
                if not any(item.provider is Provider.COINBASE for item in credential_references)
                else ("EXACT_READ_ONLY_APPROVAL_OR_FRESH_CAPTURE_MISSING",)
            ),
            0,
            0,
        ),
        LiveReadinessOperatingTask(
            LiveReadinessTaskType.KALSHI_READ_ONLY_CAPABILITY_REFRESH,
            (
                LiveReadinessTaskDisposition.COMPLETED
                if any(
                    item.provider is Provider.KALSHI
                    and item.authenticated_read_status is AuthenticatedReadStatus.AVAILABLE
                    for item in fresh_capabilities
                )
                else LiveReadinessTaskDisposition.CREDENTIALS_ABSENT
                if not any(item.provider is Provider.KALSHI for item in credential_references)
                else LiveReadinessTaskDisposition.BLOCKED
            ),
            tuple(
                item.snapshot_id
                for item in fresh_capabilities
                if item.provider is Provider.KALSHI
                and item.authenticated_read_status is AuthenticatedReadStatus.AVAILABLE
            ),
            (
                ()
                if any(
                    item.provider is Provider.KALSHI
                    and item.authenticated_read_status is AuthenticatedReadStatus.AVAILABLE
                    for item in fresh_capabilities
                )
                else ("PRIVATE_CREDENTIALS_ABSENT",)
                if not any(item.provider is Provider.KALSHI for item in credential_references)
                else ("EXACT_READ_ONLY_APPROVAL_OR_FRESH_CAPTURE_MISSING",)
            ),
            0,
            0,
        ),
    )
    return tasks
