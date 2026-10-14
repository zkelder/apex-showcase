from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from apex.adapters.coinbase_provider_preview import (
    ApprovedCoinbasePreviewClient,
    build_coinbase_preview_jwt,
)
from apex.adapters.live_preflight_acceptance import (
    ACCEPTANCE_AT,
    DeterministicCoinbasePreviewFixture,
    _repository_with_intent,
    _run_internal,
    _sources,
    run_fixture_live_preflight_acceptance,
    run_genuine_zero_intent_acceptance,
)
from apex.application.coinbase_read_only import CoinbaseCredentialMaterial
from apex.application.live_preflight import (
    ApproveCoinbaseProviderPreview,
    ConstructLiveIntent,
    InvalidateLiveIntent,
    RunApprovedCoinbaseProviderPreview,
    live_preflight_workspace,
)
from apex.application.operator_console_v1 import build_operator_live_preflight
from apex.domain.live_preflight import (
    COINBASE_PREVIEW_HOST,
    COINBASE_PREVIEW_PATH,
    KALSHI_PREVIEW_LIMITATION,
    InternalPreflightStatus,
    LiveIntentState,
    LiveOrderType,
    LivePreflightError,
    PreflightRecommendation,
    ProviderPreviewApproval,
    ProviderPreviewStatus,
    coinbase_preview_body,
    validate_provider_preview_request,
)
from apex.domain.live_readiness import Provider, ReadinessRecommendation


def test_fixture_acceptance_covers_every_required_branch_without_authority() -> None:
    scenarios = run_fixture_live_preflight_acceptance()
    assert set(scenarios) == {
        "valid_coinbase_spot_intent",
        "valid_kalshi_defined_risk_intent",
        "insufficient_balance",
        "minimum_size_failure",
        "precision_failure",
        "stale_opportunity",
        "unavailable_liquidity",
        "failed_strategy_health",
        "unresolved_reconciliation",
        "unhealthy_operating_cycle",
        "emergency_stop",
        "dangerous_provider_permissions",
        "preview_mismatch",
        "preview_unavailable",
        "fully_passing",
    }
    assert (
        scenarios["fully_passing"].recommendation is PreflightRecommendation.READY_FOR_LIVE_REVIEW
    )
    assert (
        scenarios["preview_unavailable"].recommendation
        is PreflightRecommendation.READY_FOR_LIVE_REVIEW
    )
    assert scenarios["preview_mismatch"].recommendation is PreflightRecommendation.BLOCKED
    assert scenarios["precision_failure"].blockers == tuple(
        sorted(scenarios["precision_failure"].blockers)
    )
    assert all(
        value.authority == value.live_authority == "NONE"
        and value.provider_write_count == 0
        and not value.order_created
        and not value.reservation_created
        for value in scenarios.values()
    )


def test_constructed_intent_has_exact_lineage_and_is_not_order_or_reservation() -> None:
    repository, intent = _repository_with_intent()
    assert repository.intent(intent.intent_id) == intent
    assert intent.version == 1
    assert intent.allocation_amount > 0
    assert intent.authority == intent.capital_authority == intent.live_authority == "NONE"
    assert not intent.is_order
    assert intent.provider_order_id is None
    assert intent.reservation_id is None
    assert intent.status_at(ACCEPTANCE_AT) is LiveIntentState.ACTIVE
    assert len(intent.canonical_hash) == 64


def test_zero_allocation_or_changed_lineage_is_rejected_before_intent() -> None:
    projection, eligibility, decision, line, proposal = _sources("CRYPTO")
    repository, _ = _repository_with_intent(suffix="source")
    service = ConstructLiveIntent(repository)
    for changed_line in (
        replace(line, amount=Decimal("0")),
        replace(line, source_projection_hash="0" * 64),
    ):
        with pytest.raises(LivePreflightError, match="SOURCE_LINEAGE"):
            service.execute(
                intent_id=f"bad-intent-{changed_line.source_projection_hash[:1]}",
                projection=projection,
                shared_eligibility=eligibility,
                allocation=decision,
                allocation_line=changed_line,
                paper_trial_proposal=proposal,
                provider=Provider.COINBASE,
                account_scope_id="fixture-live",
                side="BUY",
                order_type=LiveOrderType.LIMIT,
                requested_quantity="0.1",
                requested_notional=None,
                limit_price="10",
                maximum_risk="2",
                strategy_or_method_version="fixture-v1",
                capability_observed_at=ACCEPTANCE_AT,
                created_at=ACCEPTANCE_AT,
                expires_at=ACCEPTANCE_AT + timedelta(hours=1),
                actor="operator:test",
                confirmation=(
                    f"CONSTRUCT_LIVE_INTENT:bad-intent-{changed_line.source_projection_hash[:1]}"
                ),
            )


def test_internal_preflight_decimal_economics_and_risk_constraints() -> None:
    _, intent, preflight = _run_internal(suffix="economics")
    assert preflight.status is InternalPreflightStatus.READY_FOR_PROVIDER_PREVIEW
    assert preflight.normalized_quantity == Decimal("0.10")
    assert preflight.normalized_price == Decimal("10.00")
    assert preflight.estimated_notional == Decimal("1.0000")
    assert preflight.estimated_fees == Decimal("0.0010000")
    assert preflight.estimated_slippage == Decimal("0.0010000")
    assert preflight.maximum_loss == Decimal("1.0020000")
    assert preflight.maximum_loss <= intent.maximum_risk
    assert dict(preflight.constraint_checks)["BALANCE"] == "PASS"
    assert preflight.provider_request_count == preflight.provider_write_count == 0


def test_coinbase_preview_firewall_is_exact_and_rejects_mutations_before_io() -> None:
    _, intent = _repository_with_intent(suffix="firewall")
    approval = ProviderPreviewApproval.create_coinbase(
        approval_id="fixture-firewall-approval",
        credential_reference_id="fixture-view-reference",
        intent=intent,
        maximum_request_count=1,
        timeout_seconds=5,
        approved_at=ACCEPTANCE_AT,
        expires_at=ACCEPTANCE_AT + timedelta(minutes=5),
        actor="operator:test",
        reason="Exact preview-only firewall proof.",
        idempotency_key="fixture-firewall-approval",
    )
    body = coinbase_preview_body(intent)
    validate_provider_preview_request(
        approval=approval,
        intent=intent,
        method="POST",
        host=COINBASE_PREVIEW_HOST,
        path=COINBASE_PREVIEW_PATH,
        body=body,
        requested_at=ACCEPTANCE_AT + timedelta(minutes=1),
        prior_request_count=0,
    )
    for method, host, path, changed_body, requested_at, count in (
        ("POST", COINBASE_PREVIEW_HOST, "/api/v3/brokerage/orders", body, ACCEPTANCE_AT, 0),
        ("DELETE", COINBASE_PREVIEW_HOST, COINBASE_PREVIEW_PATH, body, ACCEPTANCE_AT, 0),
        ("POST", "evil.invalid", COINBASE_PREVIEW_PATH, body, ACCEPTANCE_AT, 0),
        (
            "POST",
            COINBASE_PREVIEW_HOST,
            COINBASE_PREVIEW_PATH,
            {**body, "product_id": "ETH-USD"},
            ACCEPTANCE_AT,
            0,
        ),
        (
            "POST",
            COINBASE_PREVIEW_HOST,
            COINBASE_PREVIEW_PATH,
            {**body, "client_order_id": "prohibited"},
            ACCEPTANCE_AT,
            0,
        ),
        (
            "POST",
            COINBASE_PREVIEW_HOST,
            COINBASE_PREVIEW_PATH,
            body,
            ACCEPTANCE_AT + timedelta(minutes=5),
            0,
        ),
        ("POST", COINBASE_PREVIEW_HOST, COINBASE_PREVIEW_PATH, body, ACCEPTANCE_AT, 1),
    ):
        with pytest.raises(LivePreflightError):
            validate_provider_preview_request(
                approval=approval,
                intent=intent,
                method=method,
                host=host,
                path=path,
                body=changed_body,
                requested_at=requested_at,
                prior_request_count=count,
            )


def test_approved_preview_is_redacted_zero_write_and_exact_replay() -> None:
    repository, intent, preflight = _run_internal(suffix="redaction-replay")
    approval = ApproveCoinbaseProviderPreview(repository).execute(
        approval_id="fixture-redaction-preview-approval",
        credential_reference_id="fixture-view-reference",
        intent_id=intent.intent_id,
        maximum_request_count=1,
        timeout_seconds=5,
        approved_at=ACCEPTANCE_AT + timedelta(minutes=2),
        expires_at=ACCEPTANCE_AT + timedelta(minutes=5),
        actor="operator:test",
        reason="Redaction and replay proof.",
        idempotency_key="fixture-redaction-preview-approval",
        confirmation=(
            f"APPROVE_COINBASE_PROVIDER_PREVIEW:fixture-redaction-preview-approval:"
            f"{intent.intent_id}"
        ),
    )
    secret = "fixture-never-retain"  # pragma: allowlist secret
    transport = DeterministicCoinbasePreviewFixture(
        {
            "commission_total": "0.001",
            "errs": [],
            "warning": [],
            "base_size": "0.10",
            "authorization": f"Bearer {secret}",
        }
    )
    service = RunApprovedCoinbaseProviderPreview(repository, transport)
    first = service.execute(
        capture_id="fixture-redacted-preview-capture",
        approval_id=approval.approval_id,
        preflight_id=preflight.preflight_id,
        requested_at=ACCEPTANCE_AT + timedelta(minutes=3),
    )
    replay = service.execute(
        capture_id="fixture-redacted-preview-capture",
        approval_id=approval.approval_id,
        preflight_id=preflight.preflight_id,
        requested_at=ACCEPTANCE_AT + timedelta(minutes=3),
    )
    assert first.capture.status is ProviderPreviewStatus.SUCCEEDED
    assert first.capture.response_redacted
    assert first.capture.provider_request_count == 1
    assert first.capture.provider_write_count == 0
    assert first.capture.provider_order_id is None
    assert secret not in repr(first.capture)
    assert replay.replayed and replay.capture == first.capture
    assert len(transport.requests) == 1


def test_coinbase_preview_jwt_is_bound_only_to_exact_preview_path() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    credential = CoinbaseCredentialMaterial(
        key_name="organizations/fixture/apiKeys/preview-view",
        private_key=pem,
    )
    token = build_coinbase_preview_jwt(credential=credential, issued_at=ACCEPTANCE_AT)
    payload_segment = token.split(".")[1]
    payload_segment += "=" * (-len(payload_segment) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_segment))
    assert payload["uri"] == ("POST api.coinbase.com/api/v3/brokerage/orders/preview")
    client = ApprovedCoinbasePreviewClient(
        credential_material={"fixture-reference": credential},
        issued_at=ACCEPTANCE_AT,
    )
    with pytest.raises(LivePreflightError, match="NOT_ALLOWLISTED"):
        client.preview(
            method="POST",
            host=COINBASE_PREVIEW_HOST,
            path="/api/v3/brokerage/orders",
            body={"product_id": "BTC-USD", "side": "BUY", "order_configuration": {}},
            credential_reference_id="fixture-reference",
            timeout_seconds=5,
        )


def test_kalshi_retains_preview_unavailable_without_simulated_response() -> None:
    scenarios = run_fixture_live_preflight_acceptance()
    value = scenarios["preview_unavailable"]
    assert KALSHI_PREVIEW_LIMITATION in value.warnings
    assert value.provider_write_count == 0
    assert value.recommendation is PreflightRecommendation.READY_FOR_LIVE_REVIEW


def test_preflight_cannot_promote_live_readiness_or_create_authority() -> None:
    passing = run_fixture_live_preflight_acceptance()["fully_passing"]
    assert passing.readiness_before is ReadinessRecommendation.LIVE_REVIEW_REQUIRED
    assert passing.readiness_after is ReadinessRecommendation.LIVE_REVIEW_REQUIRED
    assert passing.closed_readiness_blockers == ()
    assert passing.recommendation is PreflightRecommendation.READY_FOR_LIVE_REVIEW
    assert passing.live_authority == "NONE"


def test_invalidation_is_append_only_and_blocks_later_internal_preflight() -> None:
    repository, intent = _repository_with_intent(suffix="invalidate")
    invalidation = InvalidateLiveIntent(repository).execute(
        intent_id=intent.intent_id,
        invalidated_at=ACCEPTANCE_AT + timedelta(minutes=1),
        reason="Operator withdrew the proposal before any preflight.",
        confirmation=f"INVALIDATE_LIVE_INTENT:{intent.intent_id}",
    )
    assert invalidation.intent_hash == intent.canonical_hash
    assert repository.intent(intent.intent_id) == intent
    assert repository.invalidations(intent.intent_id) == (invalidation,)
    with pytest.raises(LivePreflightError, match="LIVE_INTENT_INVALIDATED"):
        ApproveCoinbaseProviderPreview(repository).execute(
            approval_id="invalidated-preview-approval",
            credential_reference_id="fixture-view-reference",
            intent_id=intent.intent_id,
            maximum_request_count=1,
            timeout_seconds=5,
            approved_at=ACCEPTANCE_AT + timedelta(minutes=2),
            expires_at=ACCEPTANCE_AT + timedelta(minutes=5),
            actor="operator:test",
            reason="Must be rejected because the intent was invalidated.",
            idempotency_key="invalidated-preview-approval",
            confirmation=(
                f"APPROVE_COINBASE_PROVIDER_PREVIEW:invalidated-preview-approval:{intent.intent_id}"
            ),
        )
    workspace = live_preflight_workspace(repository, as_of=ACCEPTANCE_AT + timedelta(minutes=2))
    assert workspace["operating_cycle_tasks"][0].disposition == "ZERO_INTENT"  # type: ignore[index,union-attr]


def test_operating_cycle_and_genuine_acceptance_default_to_zero_private_requests() -> None:
    genuine = run_genuine_zero_intent_acceptance()
    assert genuine["result"] == "ZERO_INTENT_BLOCKED"
    assert genuine["readiness"] == "LIVE_NOT_READY"
    assert genuine["execution_control"] == "LIVE_BLOCKED"
    assert genuine["provider_request_count"] == genuine["provider_write_count"] == 0
    tasks = genuine["operating_cycle_tasks"]
    assert len(tasks) == 5  # type: ignore[arg-type]
    assert all(item.provider_request_count == item.provider_write_count == 0 for item in tasks)  # type: ignore[union-attr]
    workspace = live_preflight_workspace(
        _repository_with_intent(suffix="cycle")[0],
        as_of=ACCEPTANCE_AT + timedelta(minutes=1),
    )
    assert "SUBMIT" in workspace["prohibited_actions"]
    assert workspace["provider_write_count"] == 0


def test_operator_console_live_preflight_area_is_compact_and_has_no_submission_action() -> None:
    empty = build_operator_live_preflight({})
    assert empty["empty_state"] == "NO_ELIGIBLE_LIVE_INTENT"
    assert empty["provider_write_count"] == 0
    assert empty["live_authority"] == "NONE"
    value = build_operator_live_preflight(
        {
            "intents": (
                {
                    "intent_id": "fixture-console-intent",
                    "provider": "COINBASE",
                    "authority": "NONE",
                },
            ),
            "preflights": ({"preflight_id": "fixture-console-preflight"},),
            "provider_preview_approvals": (),
            "provider_preview_captures": (),
            "assessments": (),
            "safe_actions": ("RUN_INTERNAL_PREFLIGHT",),
            "prohibited_actions": ("SUBMIT", "CANCEL", "TRANSFER", "WITHDRAW"),
        }
    )
    assert value["intent_count"] == 1
    assert value["empty_state"] is None
    assert "SUBMIT" in value["prohibited_actions"]  # type: ignore[operator]
