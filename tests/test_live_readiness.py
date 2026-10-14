from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from apex.adapters.live_readiness_acceptance import run_fixture_live_readiness_acceptance
from apex.application.live_readiness import (
    CredentialsAbsentCapabilityInspector,
    InMemoryLiveReadinessRepository,
    InspectProviderCapability,
    RegisterCredentialReference,
    SetExecutionControl,
    live_readiness_workspace,
    safe_json_payload,
)
from apex.domain.live_readiness import (
    AccountEnvironment,
    ExecutionControlState,
    LiveReadinessError,
    LiveRiskPolicy,
    PermissionClass,
    Provider,
    ReadinessExport,
    ReadinessRecommendation,
    SecretBackend,
    assert_secret_free,
    redact_payload,
)

NOW = datetime(2026, 8, 3, 19, tzinfo=UTC)


def _reference(repository: InMemoryLiveReadinessRepository) -> object:
    return RegisterCredentialReference(repository).execute(
        reference_id="coinbase-live-reference-v1",
        version=1,
        provider=Provider.COINBASE,
        account_environment=AccountEnvironment.LIVE,
        account_purpose="READ_ONLY_LIVE_CAPABILITY_INSPECTION",
        required_permissions=(PermissionClass.PRIVATE_VIEW,),
        secret_backend=SecretBackend.ENVIRONMENT_REFERENCE,
        backend_reference_id="coinbase-live-readonly-v1",
        actor="operator:test",
        reason="Test reference metadata only.",
        confirmation="REGISTER_CREDENTIAL_REFERENCE:coinbase-live-reference-v1:1",
        idempotency_key="register-coinbase-live-reference-v1",
        created_at=NOW,
    )


def test_credential_reference_retains_no_secret_and_absent_inspection_makes_no_request() -> None:
    repository = InMemoryLiveReadinessRepository()
    reference = _reference(repository)
    payload = asdict(reference)  # type: ignore[arg-type]
    assert payload["secret_values_retained"] is False
    assert "api_key" not in str(payload).casefold()
    assert "private_key" not in str(payload).casefold()
    safe = safe_json_payload(reference)
    assert isinstance(safe, dict)
    assert safe["secret_backend"] == "ENVIRONMENT_REFERENCE"  # pragma: allowlist secret
    assert safe["backend_reference_id"] == "[REDACTED]"
    assert safe["secret_values_retained"] is False

    absent = InspectProviderCapability(
        repository, CredentialsAbsentCapabilityInspector(Provider.KALSHI)
    ).execute(
        snapshot_id="kalshi-absent-v1",
        account_scope_id="kalshi-default",
        account_environment=AccountEnvironment.LIVE,
        credential_reference_id=None,
        inspected_at=NOW,
        request_limit=0,
        actor="operator:test",
        reason="Credentials-absent proof.",
        confirmation="INSPECT_PROVIDER_CAPABILITY_READ_ONLY:kalshi-absent-v1",
        idempotency_key="kalshi-absent-v1",
    )
    assert absent.provider_request_count == 0
    assert absent.permissions == ()
    assert not absent.trade_available
    assert absent.live_authority == "NONE"


def test_reference_rejects_paths_and_sensitive_material() -> None:
    repository = InMemoryLiveReadinessRepository()
    with pytest.raises(LiveReadinessError, match="MUST_BE_OPAQUE"):
        RegisterCredentialReference(repository).execute(
            reference_id="unsafe-reference",
            version=1,
            provider=Provider.COINBASE,
            account_environment=AccountEnvironment.LIVE,
            account_purpose="READ_ONLY",
            required_permissions=(PermissionClass.PRIVATE_VIEW,),
            secret_backend=SecretBackend.LOCAL_IGNORED_FILE_REFERENCE,
            backend_reference_id="/home/operator/.secrets/key.pem",
            actor="operator:test",
            reason="Unsafe path proof.",
            confirmation="REGISTER_CREDENTIAL_REFERENCE:unsafe-reference:1",
            idempotency_key="unsafe-reference",
            created_at=NOW,
        )
    with pytest.raises(LiveReadinessError, match="SECRET_RETENTION"):
        assert_secret_free({"api_key": "must-never-persist"})  # pragma: allowlist secret


def test_backend_redaction_covers_logs_exceptions_cli_api_and_exports() -> None:
    secret = "fixture-super-secret-value"  # pragma: allowlist secret
    source = {
        "authorization": f"Bearer {secret}",
        "nested": {"api-key": secret, "message": f"provider rejected {secret}"},
        "safe": "retained",
    }
    redacted = redact_payload(source, secret_values=(secret,))
    rendered = str(redacted)
    assert secret not in rendered
    assert "Bearer " not in rendered
    assert "[REDACTED]" in rendered
    assert secret not in str(safe_json_payload(source, secret_values=(secret,)))


def test_execution_control_is_append_only_and_live_arming_is_unavailable() -> None:
    repository = InMemoryLiveReadinessRepository()
    service = SetExecutionControl(repository)
    blocked = service.execute(
        event_id="control-1",
        control_id="global",
        state=ExecutionControlState.LIVE_BLOCKED,
        actor="operator:test",
        reason="Default block.",
        confirmation="SET_EXECUTION_CONTROL:global:LIVE_BLOCKED",
        idempotency_key="control-1",
        effective_at=NOW,
    )
    stopped = service.execute(
        event_id="control-2",
        control_id="global",
        state=ExecutionControlState.EMERGENCY_STOP,
        actor="operator:test",
        reason="Stop all new submissions.",
        confirmation="SET_EXECUTION_CONTROL:global:EMERGENCY_STOP",
        idempotency_key="control-2",
        effective_at=NOW,
    )
    assert blocked.blocks_new_submission
    assert blocked.blocks_new_live_submission
    assert not blocked.blocks_new_paper_submission
    assert stopped.blocks_new_submission
    assert stopped.blocks_new_paper_submission
    assert stopped.previous_state is ExecutionControlState.LIVE_BLOCKED
    with pytest.raises(LiveReadinessError, match="STATE_UNAVAILABLE"):
        service.execute(
            event_id="control-3",
            control_id="global",
            state=ExecutionControlState.LIVE_PILOT_ARMED,
            actor="operator:test",
            reason="Must fail.",
            confirmation="SET_EXECUTION_CONTROL:global:LIVE_PILOT_ARMED",
            idempotency_key="control-3",
            effective_at=NOW,
        )
    with pytest.raises(LiveReadinessError, match="EXACT_CONFIRMATION"):
        service.execute(
            event_id="control-4",
            control_id="global",
            state=ExecutionControlState.LIVE_BLOCKED,
            actor="operator:test",
            reason="Clear requires exact confirmation.",
            confirmation="SET_EXECUTION_CONTROL:global:LIVE_BLOCKED",
            idempotency_key="control-4",
            effective_at=NOW,
        )
    cleared = service.execute(
        event_id="control-4",
        control_id="global",
        state=ExecutionControlState.LIVE_BLOCKED,
        actor="operator:test",
        reason="Reviewed exact clear.",
        confirmation="CLEAR_EMERGENCY_STOP:global:LIVE_BLOCKED",
        idempotency_key="control-4",
        effective_at=NOW,
    )
    assert cleared.ordinal == 3


def test_fixture_acceptance_covers_every_required_readiness_branch() -> None:
    scenarios = run_fixture_live_readiness_acceptance()
    assert set(scenarios) == {
        "credentials_absent",
        "private_view_only",
        "preview_only",
        "trade_capable",
        "dangerous_permissions",
        "stale_capability",
        "insufficient_paper",
        "reconciliation_failure",
        "degraded_health",
        "unhealthy_operating_cycle",
        "missing_backup",
        "risk_limit_exceeded",
        "emergency_stop",
        "fully_passing",
    }
    assert scenarios["fully_passing"].recommendation is ReadinessRecommendation.LIVE_PILOT_ELIGIBLE
    assert scenarios["emergency_stop"].recommendation is ReadinessRecommendation.LIVE_SUSPENDED
    assert scenarios["dangerous_permissions"].unresolved_blockers == tuple(
        sorted(scenarios["dangerous_permissions"].unresolved_blockers)
    )
    assert "TRANSFER_PERMISSION_PRESENT" in scenarios["dangerous_permissions"].unresolved_blockers
    assert "WITHDRAWAL_PERMISSION_PRESENT" in scenarios["dangerous_permissions"].unresolved_blockers
    assert (
        "TOTAL_LIVE_CAPITAL_LIMIT_EXCEEDED" in scenarios["risk_limit_exceeded"].unresolved_blockers
    )
    assert all(
        value.authority == "NONE" and value.live_authority == "NONE" for value in scenarios.values()
    )


def test_trade_permission_is_never_inferred_from_authentication_or_preview() -> None:
    scenarios = run_fixture_live_readiness_acceptance()
    assert (
        "TRADE_PERMISSION_NOT_EXPLICITLY_CONFIRMED"
        in scenarios["private_view_only"].unresolved_blockers
    )
    assert (
        "TRADE_PERMISSION_NOT_EXPLICITLY_CONFIRMED" in scenarios["preview_only"].unresolved_blockers
    )
    assert (
        "TRADE_PERMISSION_NOT_EXPLICITLY_CONFIRMED"
        not in scenarios["trade_capable"].unresolved_blockers
    )


def test_live_risk_policy_v1_is_restrictive_and_grants_no_authority() -> None:
    policy = LiveRiskPolicy.v1(created_at=NOW)
    assert policy.policy_id == "LIVE_RISK_POLICY_V1"
    assert policy.allowed_products == ("SPOT", "FULLY_DEFINED_RISK")
    assert not policy.leverage_allowed
    assert not policy.shorting_allowed
    assert str(policy.maximum_total_live_capital) == "100"
    assert str(policy.minimum_retained_cash_ratio) == "0.90"
    assert policy.maximum_open_orders == 1
    assert policy.maximum_open_positions == 1
    assert policy.minimum_paper_sample_count == 30
    assert policy.minimum_clean_fill_count == 20
    assert policy.minimum_settlement_count == 20
    assert policy.capital_authority == "NONE"
    assert policy.live_authority == "NONE"


def test_credentials_absent_operating_cycle_tasks_are_safe_and_non_failing() -> None:
    workspace = live_readiness_workspace(InMemoryLiveReadinessRepository())
    tasks = workspace["operating_cycle_tasks"]
    assert isinstance(tasks, tuple)
    assert len(tasks) == 8
    assert sum(task.provider_request_count for task in tasks) == 0
    assert sum(task.provider_write_count for task in tasks) == 0
    assert tasks[0].disposition.value == "CREDENTIALS_ABSENT"
    assert workspace["live_authority"] == "NONE"


def test_readiness_export_is_redacted_paper_only_evidence() -> None:
    assessment = run_fixture_live_readiness_acceptance()["fully_passing"]
    value = ReadinessExport.create(
        export_id="fixture-redacted-export",
        assessment=assessment,
        categories=(
            ("PROVIDER_CAPABILITIES", 1),
            ("READINESS_ASSESSMENTS", 1),
            ("ORDERS_AND_FILLS", 0),
        ),
        evidence_ids=(assessment.assessment_id,),
        evidence_hashes=(assessment.canonical_hash,),
        paper_records=0,
        generated_at=NOW,
    )
    assert value.live_records == 0
    assert value.secrets_included is False
    assert "SECRET_FILE_PATHS" in value.redactions_applied
    assert value.financial_mutation_capability == "NONE"
