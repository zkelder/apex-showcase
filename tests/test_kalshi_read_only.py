from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from apex.adapters.kalshi_read_only import (
    DeterministicKalshiPrivateReadFixture,
    StaticFixtureCredentialResolver,
    build_kalshi_request_signature,
)
from apex.application.kalshi_read_only import (
    ApproveKalshiReadOnlyInspection,
    InMemoryKalshiReadOnlyRepository,
    InspectKalshiReadOnlyCapability,
    KalshiCredentialMaterial,
    RefreshKalshiReadOnlyCapability,
    kalshi_read_only_attention,
    kalshi_read_only_workspace,
)
from apex.application.live_readiness import (
    InMemoryLiveReadinessRepository,
    RegisterCredentialReference,
)
from apex.domain.kalshi_read_only import (
    KALSHI_PRIVATE_HOST,
    KalshiCapabilityClass,
    KalshiEndpointClass,
    KalshiInspectionStatus,
    KalshiReadOnlyError,
    KalshiRefreshDisposition,
    validate_kalshi_private_read_request,
)
from apex.domain.live_readiness import (
    AccountEnvironment,
    ExecutionControlState,
    PermissionClass,
    Provider,
    ProviderAccountStatus,
    SecretBackend,
)

NOW = datetime(2026, 8, 3, 14, tzinfo=UTC)
PURPOSE = "READ_ONLY_LIVE_CAPABILITY_INSPECTION"
REFERENCE_ID = "kalshi-readonly-reference-v1"
APPROVAL_ID = "fixture-kalshi-read-approval-v1"
KEY_ID = "fixture-current-key-id"


def _payload(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _responses(
    *,
    scopes: object = ("read",),
    account_status: str | None = "ACTIVE",
    include_tier: bool = True,
    permission_status: int = 200,
) -> dict[str, tuple[int, bytes]]:
    identity: dict[str, object] = {"communications_id": "fixture-account-identity"}
    if account_status is not None:
        identity["account_status"] = account_status
    values = {
        "/trade-api/v2/communications/id": (200, _payload(identity)),
        "/trade-api/v2/portfolio/balance?subaccount=0": (
            200,
            _payload({"balance": 12345, "portfolio_value": 13023}),
        ),
        "/trade-api/v2/api_keys": (
            permission_status,
            _payload(
                {
                    "api_keys": [
                        {
                            "api_key_id": KEY_ID,
                            "name": "never-retained-key-name",
                            "scopes": scopes,
                        }
                    ]
                }
            ),
        ),
    }
    if include_tier:
        values["/trade-api/v2/account/limits"] = (
            200,
            _payload(
                {
                    "usage_tier": "FIXTURE_TIER_1",
                    "read_limit": 20,
                    "write_limit": 10,
                }
            ),
        )
    return values


def _setup(
    endpoint_classes: tuple[KalshiEndpointClass, ...] = tuple(KalshiEndpointClass),
) -> tuple[InMemoryKalshiReadOnlyRepository, InMemoryLiveReadinessRepository]:
    repository = InMemoryKalshiReadOnlyRepository()
    live = InMemoryLiveReadinessRepository()
    RegisterCredentialReference(live).execute(
        reference_id=REFERENCE_ID,
        version=1,
        provider=Provider.KALSHI,
        account_environment=AccountEnvironment.LIVE,
        account_purpose=PURPOSE,
        required_permissions=(PermissionClass.PRIVATE_VIEW,),
        secret_backend=SecretBackend.ENVIRONMENT_REFERENCE,
        backend_reference_id="kalshi-readonly-v1",
        actor="operator:fixture",
        reason="Opaque fixture reference only.",
        confirmation=f"REGISTER_CREDENTIAL_REFERENCE:{REFERENCE_ID}:1",
        idempotency_key=REFERENCE_ID,
        created_at=NOW,
    )
    ApproveKalshiReadOnlyInspection(repository, live).execute(
        approval_id=APPROVAL_ID,
        credential_reference_id=REFERENCE_ID,
        account_purpose=PURPOSE,
        allowed_endpoint_classes=endpoint_classes,
        maximum_request_count=len(endpoint_classes),
        timeout_seconds=5,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
        actor="operator:fixture",
        reason="Deterministic read-only fixture proof.",
        idempotency_key=APPROVAL_ID,
        confirmation=f"APPROVE_KALSHI_READ_ONLY:{APPROVAL_ID}:{REFERENCE_ID}",
    )
    return repository, live


def _inspect(
    repository: InMemoryKalshiReadOnlyRepository,
    live: InMemoryLiveReadinessRepository,
    responses: dict[str, tuple[int, bytes]],
    *,
    suffix: str = "valid",
) -> tuple[object, DeterministicKalshiPrivateReadFixture]:
    transport = DeterministicKalshiPrivateReadFixture(
        responses=responses, completed_at=NOW + timedelta(seconds=1)
    )
    service = InspectKalshiReadOnlyCapability(
        repository,
        live,
        StaticFixtureCredentialResolver(
            KalshiCredentialMaterial(
                key_id=KEY_ID,
                private_key="fixture-ephemeral-private-material",  # pragma: allowlist secret
            )
        ),
        transport,
    )
    result = service.execute(
        attempt_id=f"fixture-kalshi-attempt-{suffix}",
        capture_id=f"fixture-kalshi-capture-{suffix}",
        readiness_snapshot_id=f"fixture-kalshi-readiness-{suffix}",
        approval_id=APPROVAL_ID,
        credential_reference_id=REFERENCE_ID,
        account_purpose=PURPOSE,
        inspected_at=NOW,
        execution_control=ExecutionControlState.LIVE_BLOCKED,
    )
    return result, transport


def test_exact_get_allowlist_rejects_mutations_hosts_and_query_variants_before_io() -> None:
    allowed = {
        KalshiEndpointClass.ACCOUNT_IDENTITY_READ: "/trade-api/v2/communications/id",
        KalshiEndpointClass.BALANCE_READ: "/trade-api/v2/portfolio/balance?subaccount=0",
        KalshiEndpointClass.PERMISSIONS_READ: "/trade-api/v2/api_keys",
        KalshiEndpointClass.ACCOUNT_TIER_READ: "/trade-api/v2/account/limits",
    }
    for endpoint_class, path in allowed.items():
        validate_kalshi_private_read_request(
            method="GET", host=KALSHI_PRIVATE_HOST, path=path, endpoint_class=endpoint_class
        )
    for method, host, path, endpoint_class in (
        (
            "POST",
            KALSHI_PRIVATE_HOST,
            "/trade-api/v2/portfolio/orders",
            KalshiEndpointClass.BALANCE_READ,
        ),
        (
            "DELETE",
            KALSHI_PRIVATE_HOST,
            "/trade-api/v2/portfolio/orders/x",
            KalshiEndpointClass.BALANCE_READ,
        ),
        (
            "GET",
            KALSHI_PRIVATE_HOST,
            "/trade-api/v2/portfolio/balance",
            KalshiEndpointClass.BALANCE_READ,
        ),
        (
            "GET",
            KALSHI_PRIVATE_HOST,
            "/trade-api/v2/portfolio/balance?subaccount=1",
            KalshiEndpointClass.BALANCE_READ,
        ),
        (
            "GET",
            "demo-api.kalshi.co",
            "/trade-api/v2/api_keys",
            KalshiEndpointClass.PERMISSIONS_READ,
        ),
        (
            "GET",
            KALSHI_PRIVATE_HOST,
            "/trade-api/v2/portfolio/settlements",
            KalshiEndpointClass.BALANCE_READ,
        ),
    ):
        with pytest.raises(KalshiReadOnlyError, match="NOT_ALLOWLISTED"):
            validate_kalshi_private_read_request(
                method=method, host=host, path=path, endpoint_class=endpoint_class
            )


def test_approval_is_exact_immutable_read_only_and_authority_free() -> None:
    repository, _ = _setup()
    approval = repository.approval(APPROVAL_ID)
    assert approval is not None
    assert approval.provider is Provider.KALSHI
    assert approval.read_only_only and approval.zero_provider_writes
    assert not any(
        (
            approval.preview_allowed,
            approval.trade_allowed,
            approval.cancellation_allowed,
            approval.fund_movement_allowed,
        )
    )
    assert approval.authority == "NONE"
    assert len(approval.canonical_hash) == 64


def test_credentials_and_approval_absence_are_non_failing_zero_request_and_replay_exact() -> None:
    repository = InMemoryKalshiReadOnlyRepository()
    live = InMemoryLiveReadinessRepository()
    transport = DeterministicKalshiPrivateReadFixture(responses={}, completed_at=NOW)
    inspector = InspectKalshiReadOnlyCapability(
        repository,
        live,
        StaticFixtureCredentialResolver(
            KalshiCredentialMaterial(key_id="unused", private_key="unused")
        ),
        transport,
    )
    kwargs = dict(
        attempt_id="credentials-absent",
        capture_id="must-not-exist",
        readiness_snapshot_id="must-not-exist",
        approval_id=None,
        credential_reference_id=None,
        account_purpose=PURPOSE,
        inspected_at=NOW,
        execution_control=ExecutionControlState.LIVE_BLOCKED,
    )
    first = inspector.execute(**kwargs)
    second = inspector.execute(**kwargs)
    assert first.capture is None and second.replayed
    assert first.attempt.blocked_reason == "PRIVATE_CREDENTIALS_ABSENT"
    assert first.attempt.request_count == first.attempt.provider_write_count == 0
    assert transport.requests == []
    refresh = RefreshKalshiReadOnlyCapability(repository, inspector).execute(
        refresh_id="not-due",
        attempt_id="not-due-attempt",
        capture_id="not-due-capture",
        readiness_snapshot_id="not-due-snapshot",
        approval_id=None,
        credential_reference_id=None,
        account_purpose=PURPOSE,
        evaluated_at=NOW,
        due=False,
        execution_control=ExecutionControlState.LIVE_BLOCKED,
    )
    assert refresh.disposition is KalshiRefreshDisposition.NOT_DUE
    assert refresh.request_count == refresh.provider_write_count == 0


def test_private_view_capture_retains_only_redacted_supported_evidence_and_readiness_fact() -> None:
    repository, live = _setup()
    result, transport = _inspect(repository, live, _responses())
    capture = result.capture
    assert capture is not None
    assert result.attempt.status is KalshiInspectionStatus.SUCCEEDED
    assert len(transport.requests) == capture.provider_request_count == 4
    assert capture.provider_write_count == 0
    assert capture.account_status is ProviderAccountStatus.ACTIVE
    assert capture.redacted_account_fingerprint.startswith("sha256:")
    assert str(capture.balances[0].available_amount) == "123.45"
    assert str(capture.balances[0].portfolio_value) == "130.23"
    assert capture.capability_classes == (KalshiCapabilityClass.PRIVATE_VIEW_AVAILABLE,)
    assert capture.account_tier == "FIXTURE_TIER_1"
    assert "KALSHI_FEE_EVIDENCE_MISSING" in capture.warnings
    assert "API_KEY_IDENTITY_NOT_RETAINED_OR_HASHED" in capture.redaction_proof
    rendered = repr(capture)
    assert KEY_ID not in rendered and "never-retained-key-name" not in rendered
    snapshot = live.capability("fixture-kalshi-readiness-valid")
    assert snapshot is not None
    assert snapshot.permissions == (PermissionClass.PRIVATE_VIEW,)
    assert snapshot.fee_evidence == ()
    assert snapshot.live_authority == "NONE"


@pytest.mark.parametrize(
    ("scopes", "warning", "attention"),
    (
        (("read", "write"), "UNEXPECTED_TRADE_PERMISSION_PRESENT", "UNEXPECTED_PERMISSION_SCOPE"),
        (
            ("read", "transfer"),
            "DANGEROUS_TRANSFER_AND_WITHDRAWAL_PERMISSION_PRESENT",
            "DANGEROUS_TRANSFER_OR_WITHDRAWAL_CAPABILITY",
        ),
        (
            ("read", "withdrawal"),
            "DANGEROUS_TRANSFER_AND_WITHDRAWAL_PERMISSION_PRESENT",
            "DANGEROUS_TRANSFER_OR_WITHDRAWAL_CAPABILITY",
        ),
    ),
)
def test_explicit_dangerous_scopes_are_facts_and_alerts_never_authority(
    scopes: tuple[str, ...], warning: str, attention: str
) -> None:
    repository, live = _setup()
    result, _ = _inspect(repository, live, _responses(scopes=scopes), suffix=scopes[-1])
    assert result.capture is not None
    assert warning in result.capture.warnings
    assert attention in kalshi_read_only_attention(
        capture=result.capture, attempt=result.attempt, as_of=NOW
    )
    assert result.capture.live_authority == result.capture.capital_authority == "NONE"


@pytest.mark.parametrize("scopes", ((), ("read", "future_scope"), None, True))
def test_ambiguous_permission_evidence_fails_closed(scopes: object) -> None:
    repository, live = _setup()
    result, _ = _inspect(repository, live, _responses(scopes=scopes), suffix="ambiguous")
    assert result.capture is None
    assert result.attempt.status is KalshiInspectionStatus.PROVIDER_FAILED
    assert "AMBIGUOUS_OR_INVALID_PERMISSION_OR_ACCOUNT_EVIDENCE" in result.attempt.warnings
    assert live.capabilities == {}


def test_restricted_stale_missing_fee_and_authentication_failure_alerts_are_deterministic() -> None:
    repository, live = _setup(
        (
            KalshiEndpointClass.ACCOUNT_IDENTITY_READ,
            KalshiEndpointClass.BALANCE_READ,
            KalshiEndpointClass.PERMISSIONS_READ,
        )
    )
    restricted, _ = _inspect(
        repository,
        live,
        _responses(account_status="RESTRICTED", include_tier=False),
        suffix="restricted",
    )
    assert restricted.capture is not None
    assert "ACCOUNT_RESTRICTION" in kalshi_read_only_attention(
        capture=restricted.capture,
        attempt=restricted.attempt,
        as_of=NOW,
    )
    assert "STALE_CAPABILITY_EVIDENCE" in kalshi_read_only_attention(
        capture=restricted.capture,
        attempt=restricted.attempt,
        as_of=NOW + timedelta(minutes=16),
    )
    assert "KALSHI_FEE_EVIDENCE_MISSING" in restricted.capture.warnings

    auth_repository, auth_live = _setup()
    failed, _ = _inspect(
        auth_repository,
        auth_live,
        _responses(permission_status=401),
        suffix="auth-failure",
    )
    assert failed.capture is None
    assert failed.attempt.status is KalshiInspectionStatus.AUTHENTICATION_FAILED
    assert "AUTHENTICATION_FAILURE" in kalshi_read_only_attention(
        capture=None, attempt=failed.attempt, as_of=NOW
    )
    rendered = repr(failed)
    assert KEY_ID not in rendered and "never-retained-key-name" not in rendered


def test_response_containing_secret_material_is_discarded_without_hash_retention() -> None:
    repository, live = _setup()
    responses = _responses()
    responses["/trade-api/v2/communications/id"] = (
        200,
        _payload({"communications_id": "x", "private_key": "leak"}),
    )
    result, _ = _inspect(repository, live, responses, suffix="secret-response")
    assert result.capture is None
    assert result.attempt.blocked_reason == "PROVIDER_RESPONSE_SECRET_SAFETY_UNPROVEN"
    assert result.attempt.request_count == 1


def test_rsa_pss_authentication_is_request_bound_and_excludes_query_from_signature() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    material = KalshiCredentialMaterial(key_id=KEY_ID, private_key=pem)
    timestamp, signature = build_kalshi_request_signature(
        credential=material,
        path="/trade-api/v2/portfolio/balance?subaccount=0",
        issued_at=NOW,
    )
    assert timestamp == str(int(NOW.timestamp() * 1000))
    private_key.public_key().verify(
        base64.b64decode(signature),
        f"{timestamp}GET/trade-api/v2/portfolio/balance".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    assert KEY_ID not in signature and pem not in signature


def test_workspace_and_readiness_regression_attention_are_authority_free() -> None:
    repository = InMemoryKalshiReadOnlyRepository()
    workspace = kalshi_read_only_workspace(repository)
    assert workspace["provider"] == "KALSHI"
    assert workspace["provider_request_count"] == workspace["provider_write_count"] == 0
    assert workspace["authority"] == workspace["live_authority"] == "NONE"
    assert "READINESS_REGRESSION" in kalshi_read_only_attention(
        capture=None,
        attempt=None,
        as_of=NOW,
        previous_readiness="LIVE_REVIEW_REQUIRED",
        current_readiness="LIVE_NOT_READY",
    )
