from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from apex.adapters.coinbase_read_only import (
    DeterministicCoinbasePrivateReadFixture,
    EnvironmentReferenceResolver,
    LocalIgnoredFileReferenceResolver,
    OperatingSystemSecretReferenceResolver,
    StaticFixtureCredentialResolver,
    build_coinbase_rest_jwt,
)
from apex.application.coinbase_read_only import (
    ApproveCoinbaseReadOnlyInspection,
    CoinbaseCredentialMaterial,
    InMemoryCoinbaseReadOnlyRepository,
    InspectCoinbaseReadOnlyCapability,
    RefreshCoinbaseReadOnlyCapability,
    coinbase_read_only_attention,
    coinbase_read_only_workspace,
)
from apex.application.live_readiness import (
    InMemoryLiveReadinessRepository,
    RegisterCredentialReference,
)
from apex.domain.coinbase_read_only import (
    CoinbaseCapabilityClass,
    CoinbaseEndpointClass,
    CoinbaseInspectionStatus,
    CoinbaseReadOnlyError,
    CoinbaseRefreshDisposition,
    validate_coinbase_private_read_request,
)
from apex.domain.live_readiness import (
    AccountEnvironment,
    ExecutionControlEvent,
    ExecutionControlState,
    LiveReadinessInputs,
    LiveRiskPolicy,
    PermissionClass,
    Provider,
    SecretBackend,
    evaluate_live_risk,
)

NOW = datetime(2026, 8, 5, 14, tzinfo=UTC)
PURPOSE = "READ_ONLY_LIVE_CAPABILITY_INSPECTION"


def _payload(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _responses(
    *,
    can_view: object = True,
    can_trade: object = False,
    can_transfer: object = False,
    restricted: bool = False,
    fees: bool = True,
    product: bool = True,
) -> dict[str, tuple[int, bytes]]:
    values = {
        "/api/v3/brokerage/accounts": (
            200,
            _payload(
                {
                    "accounts": [
                        {
                            "uuid": "fixture-account-identity",
                            "currency": "USD",
                            "available_balance": {"value": "123.45", "currency": "USD"},
                            "hold": {"value": "6.78", "currency": "USD"},
                            "active": not restricted,
                            "ready": not restricted,
                            "type": "ACCOUNT_TYPE_FIAT",
                        }
                    ],
                    "has_next": False,
                }
            ),
        ),
        "/api/v3/brokerage/key_permissions": (
            200,
            _payload(
                {
                    "can_view": can_view,
                    "can_trade": can_trade,
                    "can_transfer": can_transfer,
                    "can_receive": False,
                    "portfolio_uuid": "fixture-portfolio",
                    "portfolio_type": "DEFAULT",
                }
            ),
        ),
    }
    if fees:
        values["/api/v3/brokerage/transaction_summary"] = (
            200,
            _payload(
                {
                    "fee_tier": {
                        "maker_fee_rate": "0.004",
                        "taker_fee_rate": "0.006",
                        "pricing_tier": "ADVANCED_TRADE_TIER_1",
                    }
                }
            ),
        )
    if product:
        values["/api/v3/brokerage/products/BTC-USD"] = (
            200,
            _payload(
                {
                    "product_id": "BTC-USD",
                    "base_currency_id": "BTC",
                    "quote_currency_id": "USD",
                    "base_increment": "0.00000001",
                    "quote_increment": "0.01",
                    "base_min_size": "0.0001",
                    "quote_min_size": "1",
                    "trading_disabled": False,
                    "is_disabled": False,
                    "cancel_only": False,
                    "limit_only": False,
                    "post_only": False,
                    "view_only": False,
                }
            ),
        )
    return values


def _setup(
    *,
    endpoint_classes: tuple[CoinbaseEndpointClass, ...] = tuple(CoinbaseEndpointClass),
    maximum_requests: int = 4,
) -> tuple[InMemoryCoinbaseReadOnlyRepository, InMemoryLiveReadinessRepository]:
    repository = InMemoryCoinbaseReadOnlyRepository()
    live = InMemoryLiveReadinessRepository()
    RegisterCredentialReference(live).execute(
        reference_id="coinbase-readonly-reference-v1",
        version=1,
        provider=Provider.COINBASE,
        account_environment=AccountEnvironment.LIVE,
        account_purpose=PURPOSE,
        required_permissions=(PermissionClass.PRIVATE_VIEW,),
        secret_backend=SecretBackend.ENVIRONMENT_REFERENCE,
        backend_reference_id="coinbase-readonly-v1",
        actor="operator:fixture",
        reason="Fixture opaque reference only.",
        confirmation="REGISTER_CREDENTIAL_REFERENCE:coinbase-readonly-reference-v1:1",
        idempotency_key="coinbase-readonly-reference-v1",
        created_at=NOW,
    )
    ApproveCoinbaseReadOnlyInspection(repository, live).execute(
        approval_id="fixture-coinbase-read-approval-v1",
        credential_reference_id="coinbase-readonly-reference-v1",
        account_purpose=PURPOSE,
        allowed_endpoint_classes=endpoint_classes,
        maximum_request_count=maximum_requests,
        timeout_seconds=5,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
        actor="operator:fixture",
        reason="Deterministic read-only fixture proof.",
        idempotency_key="fixture-coinbase-read-approval-v1",
        confirmation=(
            "APPROVE_COINBASE_READ_ONLY:fixture-coinbase-read-approval-v1:"
            "coinbase-readonly-reference-v1"
        ),
    )
    return repository, live


def _inspect(
    repository: InMemoryCoinbaseReadOnlyRepository,
    live: InMemoryLiveReadinessRepository,
    responses: dict[str, tuple[int, bytes]],
    *,
    suffix: str = "valid",
    product_ids: tuple[str, ...] = ("BTC-USD",),
) -> tuple[object, DeterministicCoinbasePrivateReadFixture]:
    transport = DeterministicCoinbasePrivateReadFixture(
        responses=responses,
        completed_at=NOW + timedelta(seconds=1),
    )
    service = InspectCoinbaseReadOnlyCapability(
        repository,
        live,
        StaticFixtureCredentialResolver(
            CoinbaseCredentialMaterial(
                key_name="organizations/fixture/apiKeys/read-only",
                private_key="fixture-ephemeral-private-material",  # pragma: allowlist secret
            )
        ),
        transport,
    )
    result = service.execute(
        attempt_id=f"fixture-coinbase-attempt-{suffix}",
        capture_id=f"fixture-coinbase-capture-{suffix}",
        readiness_snapshot_id=f"fixture-coinbase-readiness-{suffix}",
        approval_id="fixture-coinbase-read-approval-v1",
        credential_reference_id="coinbase-readonly-reference-v1",
        account_purpose=PURPOSE,
        product_ids=product_ids,
        inspected_at=NOW,
        execution_control=ExecutionControlState.LIVE_BLOCKED,
    )
    return result, transport


def test_exact_allowlist_rejects_preview_orders_transfers_and_non_get_before_io() -> None:
    for method, path, endpoint_class in (
        ("POST", "/api/v3/brokerage/orders/preview", CoinbaseEndpointClass.PERMISSIONS_READ),
        ("POST", "/api/v3/brokerage/orders", CoinbaseEndpointClass.ACCOUNT_STATUS_READ),
        (
            "POST",
            "/api/v3/brokerage/orders/batch_cancel",
            CoinbaseEndpointClass.ACCOUNT_STATUS_READ,
        ),
        ("POST", "/v2/accounts/id/transactions", CoinbaseEndpointClass.BALANCES_READ),
        (
            "GET",
            "/api/v3/brokerage/orders/historical/batch",
            CoinbaseEndpointClass.ACCOUNT_STATUS_READ,
        ),
    ):
        with pytest.raises(CoinbaseReadOnlyError, match="NOT_ALLOWLISTED"):
            validate_coinbase_private_read_request(
                method=method,
                host="api.coinbase.com",
                path=path,
                endpoint_class=endpoint_class,
            )


def test_approval_expiration_scope_and_exact_confirmation_fail_closed() -> None:
    repository, live = _setup()
    approval = repository.approval("fixture-coinbase-read-approval-v1")
    assert approval is not None and approval.active_at(NOW)
    assert approval.read_only_only and approval.zero_provider_writes
    assert not any(
        (
            approval.preview_allowed,
            approval.trade_allowed,
            approval.transfer_allowed,
            approval.withdrawal_allowed,
        )
    )
    with pytest.raises(CoinbaseReadOnlyError, match="EXACT_CONFIRMATION"):
        ApproveCoinbaseReadOnlyInspection(repository, live).execute(
            approval_id="bad",
            credential_reference_id="coinbase-readonly-reference-v1",
            account_purpose=PURPOSE,
            allowed_endpoint_classes=(CoinbaseEndpointClass.PERMISSIONS_READ,),
            maximum_request_count=1,
            timeout_seconds=5,
            approved_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            actor="operator:fixture",
            reason="Must fail.",
            idempotency_key="bad",
            confirmation="wrong",
        )


def test_private_view_capture_retains_redacted_balance_fee_minimum_and_exact_replay() -> None:
    repository, live = _setup()
    first, transport = _inspect(repository, live, _responses())
    result = first  # narrow the intentionally opaque helper return for assertions
    assert result.attempt.status is CoinbaseInspectionStatus.SUCCEEDED  # type: ignore[attr-defined]
    capture = result.capture  # type: ignore[attr-defined]
    assert capture is not None
    assert capture.capability_classes == (CoinbaseCapabilityClass.PRIVATE_VIEW_AVAILABLE,)
    assert capture.provider_request_count == 4
    assert capture.provider_write_count == 0
    assert capture.balances[0].currency == "USD"
    assert str(capture.balances[0].available_amount) == "123.45"
    assert str(capture.balances[0].held_amount) == "6.78"
    assert str(capture.maker_fee_rate) == "0.004"
    assert capture.fee_tier == "ADVANCED_TRADE_TIER_1"
    assert str(capture.products[0].minimum_quote_size) == "1"
    assert capture.redacted_account_fingerprint.startswith("sha256:")
    rendered = str(capture)
    assert "fixture-account-identity" not in rendered
    assert "fixture-ephemeral-private-material" not in rendered
    assert len(transport.requests) == 4

    replay, _ = _inspect(repository, live, _responses())
    assert replay.replayed  # type: ignore[attr-defined]
    assert replay.capture == capture  # type: ignore[attr-defined]
    assert len(transport.requests) == 4


def test_readiness_reassessment_closes_only_evidence_supported_blockers() -> None:
    repository, live = _setup()
    result, _ = _inspect(repository, live, _responses(), suffix="readiness")
    capture = result.capture  # type: ignore[attr-defined]
    assert capture is not None
    snapshot = live.latest_capability(Provider.COINBASE, "coinbase-default")
    assert snapshot is not None
    policy = LiveRiskPolicy.v1(
        created_at=NOW,
        allowed_account_scopes=("coinbase-default",),
    )
    control = ExecutionControlEvent.create(
        event_id="coinbase-readiness-live-blocked",
        control_id="global",
        ordinal=1,
        state=ExecutionControlState.LIVE_BLOCKED,
        actor="operator:fixture",
        reason="Readiness remains blocked.",
        effective_at=NOW,
        expires_at=None,
        previous_state=None,
        idempotency_key="coinbase-readiness-live-blocked",
    )
    decision = evaluate_live_risk(
        decision_id="coinbase-readiness-decision",
        policy=policy,
        inputs=LiveReadinessInputs(
            venue=Provider.COINBASE,
            account_scope_id="coinbase-default",
            capability=snapshot,
            strategy_or_method_id=None,
            paper_sample_count=0,
            fill_count=0,
            settlement_count=0,
            total_live_capital=Decimal("0"),
            retained_cash_ratio=Decimal("1"),
            per_opportunity_exposure=Decimal("0"),
            per_strategy_exposure=Decimal("0"),
            per_instrument_exposure=Decimal("0"),
            per_venue_exposure=Decimal("0"),
            open_order_count=0,
            open_position_count=0,
            daily_realized_loss=Decimal("0"),
            daily_modeled_loss=Decimal("0"),
            drawdown_ratio=Decimal("0"),
            strategy_health_state="INSUFFICIENT_SAMPLE",
            reconciliation_quality="UNAVAILABLE",
            unresolved_reconciliation_severity=1,
            operating_cycle_health="HEALTHY",
            provider_health="HEALTHY",
            backup=None,
            control=control,
            qualifying_opportunity=False,
            explicit_operator_review=True,
            assessed_at=NOW,
        ),
    )
    assert "PRIVATE_ACCOUNT_VIEW_UNAVAILABLE" not in decision.blockers
    assert "FEE_EVIDENCE_MISSING" not in decision.blockers
    assert "MINIMUM_SIZE_EVIDENCE_MISSING" not in decision.blockers
    assert "PRODUCT_NOT_ALLOWED_OR_UNPROVEN" not in decision.blockers
    assert "TRADE_PERMISSION_NOT_EXPLICITLY_CONFIRMED" in decision.blockers
    assert "LIVE_PILOT_NOT_ARMED" in decision.blockers
    assert "INSUFFICIENT_GENUINE_PAPER_SAMPLE" in decision.blockers


def test_explicit_trade_and_dangerous_permissions_are_classified_but_grant_no_authority() -> None:
    repository, live = _setup()
    result, _ = _inspect(
        repository,
        live,
        _responses(can_trade=True, can_transfer=True),
        suffix="dangerous",
    )
    capture = result.capture  # type: ignore[attr-defined]
    assert capture is not None
    assert CoinbaseCapabilityClass.LIVE_TRADE_PERMISSION_AVAILABLE in capture.capability_classes
    assert CoinbaseCapabilityClass.TRANSFER_PERMISSION_AVAILABLE in capture.capability_classes
    assert CoinbaseCapabilityClass.WITHDRAWAL_PERMISSION_AVAILABLE in capture.capability_classes
    assert "DANGEROUS_TRANSFER_AND_WITHDRAWAL_PERMISSION_PRESENT" in capture.warnings
    assert capture.authority == capture.capital_authority == capture.live_authority == "NONE"
    assert "DANGEROUS_TRANSFER_OR_WITHDRAWAL_CAPABILITY" in coinbase_read_only_attention(
        capture=capture,
        attempt=result.attempt,  # type: ignore[attr-defined]
        as_of=NOW,
    )


def test_credentials_and_approval_absent_paths_make_zero_requests_and_retain_no_capture() -> None:
    repository = InMemoryCoinbaseReadOnlyRepository()
    live = InMemoryLiveReadinessRepository()
    transport = DeterministicCoinbasePrivateReadFixture(responses={}, completed_at=NOW)
    service = InspectCoinbaseReadOnlyCapability(
        repository,
        live,
        StaticFixtureCredentialResolver(
            CoinbaseCredentialMaterial(key_name="unused", private_key="unused")
        ),
        transport,
    )
    absent = service.execute(
        attempt_id="credentials-absent",
        capture_id="must-not-exist",
        readiness_snapshot_id="must-not-exist",
        approval_id=None,
        credential_reference_id=None,
        account_purpose=PURPOSE,
        product_ids=(),
        inspected_at=NOW,
        execution_control=ExecutionControlState.LIVE_BLOCKED,
    )
    assert absent.attempt.blocked_reason == "PRIVATE_CREDENTIALS_ABSENT"
    assert absent.attempt.request_count == 0
    assert absent.capture is None
    assert repository.captures() == ()
    assert transport.requests == []


def test_authentication_failure_is_fully_redacted_and_zero_write() -> None:
    repository, live = _setup()
    responses = _responses()
    responses["/api/v3/brokerage/accounts"] = (
        401,
        b'{"message":"rejected fixture-sensitive-token"}',  # pragma: allowlist secret
    )
    result, _ = _inspect(repository, live, responses, suffix="auth-failure")
    assert result.attempt.status is CoinbaseInspectionStatus.AUTHENTICATION_FAILED  # type: ignore[attr-defined]
    assert result.capture is None  # type: ignore[attr-defined]
    assert "fixture-sensitive-token" not in str(result.attempt)  # type: ignore[attr-defined]
    assert result.attempt.provider_write_count == 0  # type: ignore[attr-defined]


def test_restricted_missing_fee_insufficient_product_stale_and_refresh_states() -> None:
    endpoints = (
        CoinbaseEndpointClass.ACCOUNT_STATUS_READ,
        CoinbaseEndpointClass.PERMISSIONS_READ,
    )
    repository, live = _setup(endpoint_classes=endpoints, maximum_requests=2)
    result, _ = _inspect(
        repository,
        live,
        _responses(restricted=True, fees=False, product=False),
        suffix="restricted",
        product_ids=(),
    )
    capture = result.capture  # type: ignore[attr-defined]
    assert capture is not None
    assert "COINBASE_ACCOUNT_RESTRICTED" in capture.warnings
    assert "COINBASE_FEE_EVIDENCE_MISSING" in capture.warnings
    assert "COINBASE_PRODUCT_EVIDENCE_INSUFFICIENT" in capture.warnings
    attention = coinbase_read_only_attention(
        capture=capture,
        attempt=result.attempt,  # type: ignore[attr-defined]
        as_of=NOW + timedelta(hours=1),
    )
    assert {"ACCOUNT_RESTRICTION", "STALE_CAPABILITY_EVIDENCE"}.issubset(attention)

    inspector = InspectCoinbaseReadOnlyCapability(
        repository,
        live,
        StaticFixtureCredentialResolver(
            CoinbaseCredentialMaterial(key_name="unused", private_key="unused")
        ),
        DeterministicCoinbasePrivateReadFixture(responses={}, completed_at=NOW),
    )
    not_due = RefreshCoinbaseReadOnlyCapability(repository, inspector).execute(
        refresh_id="refresh-not-due",
        attempt_id="unused",
        capture_id="unused",
        readiness_snapshot_id="unused",
        approval_id="fixture-coinbase-read-approval-v1",
        credential_reference_id="coinbase-readonly-reference-v1",
        account_purpose=PURPOSE,
        product_ids=(),
        evaluated_at=NOW,
        due=False,
        execution_control=ExecutionControlState.LIVE_BLOCKED,
    )
    assert not_due.disposition is CoinbaseRefreshDisposition.NOT_DUE
    assert not_due.request_count == 0


def test_ambiguous_view_permission_fails_closed_without_inference() -> None:
    repository, live = _setup()
    result, _ = _inspect(repository, live, _responses(can_view=None), suffix="ambiguous")
    assert result.capture is None  # type: ignore[attr-defined]
    assert result.attempt.status is CoinbaseInspectionStatus.PROVIDER_FAILED  # type: ignore[attr-defined]
    assert result.attempt.blocked_reason == "CAPABILITY_EVIDENCE_INVALID_REDACTED"  # type: ignore[attr-defined]
    assert live.latest_capability(Provider.COINBASE, "coinbase-default") is None


def test_environment_and_operating_system_backends_resolve_ephemerally() -> None:
    _, live = _setup()
    reference = live.credential_reference("coinbase-readonly-reference-v1")
    assert reference is not None
    raw = json.dumps(
        {
            "key_name": "organizations/fixture/apiKeys/view",
            "private_key": "ephemeral-fixture-key",  # pragma: allowlist secret
        }
    )
    environment = EnvironmentReferenceResolver(
        reference_environment_names={"coinbase-readonly-v1": "APEX_FIXTURE_COINBASE"},
        environment={"APEX_FIXTURE_COINBASE": raw},
    ).resolve(reference)
    os_secret = OperatingSystemSecretReferenceResolver(loader=lambda identity: raw).resolve(
        reference
    )
    assert repr(environment) == repr(os_secret) == "CoinbaseCredentialMaterial([REDACTED])"
    assert "ephemeral-fixture-key" not in repr(environment)


def test_ignored_local_file_backend_enforces_private_mode(tmp_path: Path) -> None:
    root = tmp_path / "credentials"
    root.mkdir()
    path = root / "coinbase.json"
    path.write_text(
        json.dumps(
            {
                "key_name": "organizations/fixture/apiKeys/view",
                "private_key": "ephemeral-local-fixture-key",  # pragma: allowlist secret
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    _, live = _setup()
    reference = live.credential_reference("coinbase-readonly-reference-v1")
    assert reference is not None
    material = LocalIgnoredFileReferenceResolver(
        ignored_root=root,
        reference_file_names={"coinbase-readonly-v1": "coinbase.json"},
    ).resolve(reference)
    assert repr(material) == "CoinbaseCredentialMaterial([REDACTED])"
    path.chmod(0o644)
    with pytest.raises(CoinbaseReadOnlyError, match="REFERENCE_INVALID"):
        LocalIgnoredFileReferenceResolver(
            ignored_root=root,
            reference_file_names={"coinbase-readonly-v1": "coinbase.json"},
        ).resolve(reference)


def test_request_bound_es256_jwt_contains_no_persisted_state() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    token = build_coinbase_rest_jwt(
        credential=CoinbaseCredentialMaterial(
            key_name="organizations/fixture/apiKeys/view",
            private_key=pem,
        ),
        path="/api/v3/brokerage/accounts",
        issued_at=NOW,
    )
    assert token.count(".") == 2
    assert pem not in token


def test_workspace_exposes_no_mutation_capability() -> None:
    workspace = coinbase_read_only_workspace(InMemoryCoinbaseReadOnlyRepository())
    assert workspace["provider_request_count"] == 0
    assert workspace["provider_write_count"] == 0
    assert workspace["preview_capability"] == "NONE"
    assert workspace["live_authority"] == "NONE"
