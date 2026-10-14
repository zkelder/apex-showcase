from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.adapters.alpaca_paper_broker import (
    AlpacaPaperBroker,
    AlpacaPaperCredentials,
    AlpacaPaperResponse,
)
from apex.adapters.external_paper_demo import (
    alpaca_paper_acceptance_demo,
    external_paper_reconciliation_demo,
)
from apex.adapters.external_paper_fixture import (
    AMBIGUOUS,
    FULL_FILL,
    PARTIAL_FILL,
    DeterministicPaperBrokerFixture,
)
from apex.application.external_paper import (
    CancelExternalPaperInstruction,
    CheckAlpacaPaperReadiness,
    ExternalPaperApplicationError,
    FlattenExternalPaperCampaign,
    InMemoryExternalPaperRepository,
    KillExternalPaper,
    PrepareAuthorizedExternalPaperExecution,
    ResolveAmbiguousExternalPaperSubmission,
    SubmitAuthorizedPaperInstruction,
    SyncExternalPaperInstruction,
    SynchronizeAlpacaPaperInstruction,
    SynchronizeExternalPaperCampaign,
    ValidateAlpacaPaperFill,
    ValidateExternalPaperFill,
    external_paper_flatten_confirmation,
    external_paper_kill_confirmation,
    ready_capability,
)
from apex.domain.external_paper import (
    ExternalPaperAuthorization,
    ExternalPaperBrokerError,
    ExternalPaperBrokerEvidence,
    ExternalPaperCapabilityState,
    ExternalPaperInstruction,
    ExternalPaperInstructionState,
    ExternalPaperInternalExecutionBinding,
    ExternalPaperReconciliationClassification,
    PaperBrokerCapabilitySnapshot,
    reconcile_external_paper,
)

NOW = datetime(2026, 7, 28, 20, 0, tzinfo=UTC)


def test_alpaca_acceptance_demo_retains_preflight_without_financial_or_broker_mutation() -> None:
    report = alpaca_paper_acceptance_demo()

    assert report["purpose"] == "ENGINEERING_ACCEPTANCE_ONLY"
    assert report["environment"] == "ALPACA_PAPER"
    assert report["preflight"].state == "READY_FOR_OPERATOR_CONFIRMATION"
    assert report["retained_preflight"].confirmation == report["preflight"].confirmation
    assert report["preflight"].confirmation.startswith("SUBMIT_ALPACA_PAPER:")
    assert report["network_calls"] == 0
    assert report["credential_use"] is False
    assert report["actual_paper_orders"] == 0
    assert report["live_authority"] == "NONE"


def test_fixture_reconciliation_demo_retains_each_terminal_lifecycle() -> None:
    report = external_paper_reconciliation_demo()

    assert report["authority"] == "FIXTURE_ONLY"
    assert report["actual_broker_network_calls"] == 0
    assert report["actual_paper_orders"] == 0
    assert report["credential_use"] is False
    assert report["live_authority"] == "NONE"
    assert report["genuine_shadow_mutated"] is False

    full = report["full_fill"]
    assert full["state"] == "FILLED"
    assert full["binding_state"] == "FULLY_APPLIED"
    assert full["quantity"] == "1"
    assert full["cash"] == "900"

    partial = report["partial_fill_canceled_remainder"]
    assert partial["state"] == "CANCELED"
    assert partial["binding_state"] == "CANCELED"
    assert partial["quantity"] == "0.5"
    assert partial["cash"] == "950.0"

    rejected = report["rejected"]
    assert rejected["state"] == "REJECTED"
    assert rejected["binding_state"] == "REJECTED"
    assert rejected["applied_fill_count"] == 0
    assert rejected["quantity"] == "0"
    assert rejected["cash"] == "1000"

    recovered = report["ambiguous_resolved"]
    assert recovered["submission_replayed"] is True
    assert recovered["state"] == "FILLED"
    assert recovered["binding_state"] == "FULLY_APPLIED"
    assert recovered["applied_fill_count"] == 1

    race = report["fill_cancel_race"]
    assert race["state"] == "CANCELED"
    assert race["binding_state"] == "CANCELED"
    assert race["quantity"] == "0.5"
    assert race["cash"] == "950.0"
    assert race["applied_fill_count"] == 1

    reduction = report["reduction_and_close"]
    assert reduction["partial_reduction"]["state"] == "FILLED"
    assert reduction["partial_reduction"]["position_quantity"] == "0.5"
    assert reduction["complete_close"]["state"] == "FILLED"
    assert reduction["complete_close"]["position_quantity"] == "0"
    assert reduction["final_position_state"] == "CLOSED"
    assert reduction["final_position_quantity"] == "0"
    assert reduction["flatten_completed"] is True
    assert reduction["flatten_blockers"] == ()
    assert reduction["journals_balanced"] is True

    controls = report["unknown_activity_kill_and_flatten"]
    assert controls["unknown_activity_block"] is True
    assert controls["blocked_instruction_state"] == "BLOCKED"
    assert controls["kill_replayed"] is False
    assert controls["flatten_completed"] is True
    assert controls["flatten_blockers"] == ()
    assert controls["fixture_network_calls"] == 0

    for lifecycle in (full, partial, rejected, recovered):
        assert lifecycle["fixture_network_calls"] == 0
        assert lifecycle["journals_balanced"] is True
        assert lifecycle["replay_journal_count_unchanged"] is True


def _authorization() -> ExternalPaperAuthorization:
    return ExternalPaperAuthorization.create(
        campaign_id="campaign-paper-fixture",
        paper_trial_authorization_id="trial-paper-fixture",
        portfolio_run_id="portfolio-run-fixture",
        portfolio_instruction_id="portfolio-instruction-fixture",
        allowed_symbols=("SPY",),
        maximum_capital="100",
        maximum_position_quantity="10",
        maximum_order_count=1,
        starts_at=NOW,
        expires_at=NOW + timedelta(days=1),
        operator_action_id="operator-fixture-approval",
    )


def _bind(
    repository: InMemoryExternalPaperRepository,
    authorization: ExternalPaperAuthorization,
    instruction: ExternalPaperInstruction,
) -> None:
    repository.save_binding(
        ExternalPaperInternalExecutionBinding.pending(
            authorization=authorization,
            instruction=instruction,
            securities_paper_run_id=f"run-{instruction.instruction_id}",
            internal_order_id=f"order-{instruction.instruction_id}",
            risk_reservation_id=f"reservation-{instruction.instruction_id}",
            execution_policy_id="SECURITIES_NEXT_BAR_CROSS_V1",
            execution_policy_hash="a" * 64,
            instrument_id="instrument-spy",
            intended_session="2026-07-29",
            idempotency_key=f"binding-{instruction.instruction_id}",
        )
    )


def _submitted_instruction(
    repository: InMemoryExternalPaperRepository, *, key: str = "alpaca-sync"
) -> ExternalPaperInstruction:
    authorization = _authorization()
    repository.save_authorization(authorization)
    instruction = (
        ExternalPaperInstruction.authorized(
            authorization=authorization,
            symbol="SPY",
            quantity="1",
            maximum_notional="100",
            expires_at=NOW + timedelta(hours=1),
            idempotency_key=key,
        )
        .transition(state=ExternalPaperInstructionState.SUBMISSION_PENDING)
        .transition(
            state=ExternalPaperInstructionState.SUBMITTED,
            broker_order_id="alpaca-paper-order-1",
            reason_code="accepted",
        )
    )
    return repository.save_instruction(instruction)


def _sync_adapter(
    *, status: str, filled_qty: str = "0", request_id: str = "request-1"
) -> AlpacaPaperBroker:
    def request(
        _method: str, url: str, _headers: dict[str, str], _body: bytes | None
    ) -> AlpacaPaperResponse:
        if url.endswith("/v2/orders/alpaca-paper-order-1"):
            return AlpacaPaperResponse(
                200,
                {
                    "id": "alpaca-paper-order-1",
                    "symbol": "SPY",
                    "side": "buy",
                    "status": status,
                    "qty": "1",
                    "filled_qty": filled_qty,
                    "filled_avg_price": "500" if filled_qty != "0" else None,
                    "updated_at": "2026-07-28T20:00:00Z",
                },
                request_id,
            )
        if "/v2/account/activities?" in url:
            payload = (
                [
                    {
                        "id": "fill-1",
                        "order_id": "alpaca-paper-order-1",
                        "symbol": "SPY",
                        "side": "buy",
                        "qty": filled_qty,
                        "price": "500",
                        "transaction_time": "2026-07-28T20:00:00Z",
                    }
                ]
                if filled_qty != "0"
                else []
            )
            return AlpacaPaperResponse(200, payload, "activity-request")
        if url.endswith("/v2/positions/SPY"):
            return AlpacaPaperResponse(
                200,
                {
                    "symbol": "SPY",
                    "qty": filled_qty,
                    "avg_entry_price": "500" if filled_qty != "0" else None,
                    "updated_at": "2026-07-28T20:00:00Z",
                },
                "position-request",
            )
        if url.endswith("/v2/account"):
            return AlpacaPaperResponse(200, {"status": "ACTIVE", "cash": "1000"}, "account-request")
        raise AssertionError(url)

    return AlpacaPaperBroker(
        credentials=AlpacaPaperCredentials("TEST", "paper-key", "paper-secret"), request=request
    )


@pytest.mark.parametrize(
    ("status", "filled_qty", "expected_state", "next_action"),
    (
        ("accepted", "0", ExternalPaperInstructionState.SUBMITTED, "WAIT_AND_SYNCHRONIZE_AGAIN"),
        ("pending_new", "0", ExternalPaperInstructionState.SUBMITTED, "WAIT_AND_SYNCHRONIZE_AGAIN"),
        (
            "partially_filled",
            "0.5",
            ExternalPaperInstructionState.PARTIALLY_FILLED,
            "VALIDATE_RETAINED_FILL",
        ),
        ("filled", "1", ExternalPaperInstructionState.FILLED, "VALIDATE_RETAINED_FILL"),
        ("canceled", "0", ExternalPaperInstructionState.CANCELED, "WAIT_AND_SYNCHRONIZE_AGAIN"),
        ("rejected", "0", ExternalPaperInstructionState.REJECTED, "WAIT_AND_SYNCHRONIZE_AGAIN"),
    ),
)
def test_alpaca_paper_sync_reads_exact_order_and_never_applies_finance(
    status: str,
    filled_qty: str,
    expected_state: ExternalPaperInstructionState,
    next_action: str,
) -> None:
    repository = InMemoryExternalPaperRepository()
    instruction = _submitted_instruction(repository, key=f"alpaca-sync-{status}")
    adapter = _sync_adapter(status=status, filled_qty=filled_qty)

    result = SynchronizeAlpacaPaperInstruction(repository=repository, broker=adapter).execute(
        instruction_id=instruction.instruction_id
    )

    assert result.instruction.state is expected_state
    assert result.broker_order_state == status
    assert result.next_action == next_action
    assert result.order_observation_ids
    assert result.account_observation_ids
    assert result.network_reads == 4
    assert result.broker_writes is False
    assert result.financial_mutations == 0
    assert result.actual_paper_orders_submitted == 0
    assert result.live_authority == "NONE"
    assert repository.validated_fills == {}
    assert repository.fill_applications == {}
    assert len(result.fill_activity_observation_ids) == (1 if filled_qty != "0" else 0)


def test_alpaca_paper_sync_deduplicates_repeated_observations_and_retains_request_ids() -> None:
    repository = InMemoryExternalPaperRepository()
    instruction = _submitted_instruction(repository, key="alpaca-sync-deduplicated")
    adapter = _sync_adapter(status="accepted")
    service = SynchronizeAlpacaPaperInstruction(repository=repository, broker=adapter)

    first = service.execute(instruction_id=instruction.instruction_id)
    second = service.execute(instruction_id=instruction.instruction_id)

    assert first.order_observation_ids == second.order_observation_ids
    assert first.request_ids == ("account-request", "position-request", "request-1")
    assert len(repository.evidence_for_instruction(instruction.instruction_id)) == 3
    assert adapter.network_reads == 8


def test_alpaca_paper_sync_rejects_fixture_and_missing_credentials_without_requests() -> None:
    repository = InMemoryExternalPaperRepository()
    instruction = _submitted_instruction(repository, key="alpaca-sync-adapter-isolation")
    fixture = DeterministicPaperBrokerFixture(scenarios={}, now=NOW)
    with pytest.raises(ExternalPaperApplicationError, match="ALPACA_PAPER_ADAPTER_REQUIRED"):
        SynchronizeAlpacaPaperInstruction(repository=repository, broker=fixture).execute(  # type: ignore[arg-type]
            instruction_id=instruction.instruction_id
        )

    calls: list[str] = []
    adapter = AlpacaPaperBroker(
        credentials=AlpacaPaperCredentials("TEST", None, None),
        request=lambda _method, url, _headers, _body: calls.append(url),  # type: ignore[arg-type]
    )
    result = SynchronizeAlpacaPaperInstruction(repository=repository, broker=adapter).execute(
        instruction_id=instruction.instruction_id
    )
    assert result.next_action == "AUTHENTICATION_REJECTED"
    assert calls == []
    assert adapter.network_reads == 0


def test_alpaca_paper_sync_fails_closed_when_exact_broker_order_does_not_match() -> None:
    repository = InMemoryExternalPaperRepository()
    instruction = _submitted_instruction(repository, key="alpaca-sync-unmatched")
    adapter = _sync_adapter(status="accepted")
    original = adapter.order

    def mismatched(*, instruction_id: str, broker_order_id: str) -> ExternalPaperBrokerEvidence:
        evidence = original(instruction_id=instruction_id, broker_order_id=broker_order_id)
        return ExternalPaperBrokerEvidence.create(
            instruction_id=evidence.instruction_id,
            endpoint_identity=evidence.endpoint_identity,
            observation_type=evidence.observation_type,
            observed_at=evidence.observed_at,
            broker_reported_at=evidence.broker_reported_at,
            request_id=evidence.request_id,
            sanitized_payload={**evidence.sanitized_payload, "id": "different-order"},
        )

    adapter.order = mismatched  # type: ignore[method-assign]
    result = SynchronizeAlpacaPaperInstruction(repository=repository, broker=adapter).execute(
        instruction_id=instruction.instruction_id
    )

    assert result.instruction.state is ExternalPaperInstructionState.RECONCILIATION_REQUIRED
    assert result.next_action == "RECONCILIATION_REQUIRED"
    assert "RETRIEVE_EXACT_BROKER_ORDER_AND_REVIEW" in result.blockers


def test_alpaca_validation_rejects_fixture_evidence_and_retains_genuine_validation() -> None:
    authorization = _authorization()
    repository = InMemoryExternalPaperRepository()
    repository.save_authorization(authorization)
    instruction = ExternalPaperInstruction.authorized(
        authorization=authorization,
        symbol="SPY",
        quantity="1",
        maximum_notional="100",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="alpaca-genuine-validation",
    )
    repository.save_instruction(instruction)
    _bind(repository, authorization, instruction)
    fixture = DeterministicPaperBrokerFixture(
        scenarios={"alpaca-genuine-validation": FULL_FILL}, now=NOW
    )
    submitted = SubmitAuthorizedPaperInstruction(repository=repository, broker=fixture).execute(
        instruction_id=instruction.instruction_id, capability=ready_capability(NOW), now=NOW
    )
    assert submitted.evidence is not None
    with pytest.raises(
        ExternalPaperApplicationError, match="ALPACA_PAPER_GENUINE_EVIDENCE_REQUIRED"
    ):
        ValidateAlpacaPaperFill(repository=repository).execute(
            instruction_id=instruction.instruction_id, evidence_id=submitted.evidence.evidence_id
        )

    evidence = ExternalPaperBrokerEvidence.create(
        instruction_id=instruction.instruction_id,
        endpoint_identity="PAPER:GET:/v2/orders/{order_id}",
        observation_type="ORDER_STATUS",
        observed_at=NOW,
        broker_reported_at=NOW,
        request_id="genuine-paper-request",
        sanitized_payload={
            "id": submitted.instruction.broker_order_id,
            "symbol": "SPY",
            "side": "buy",
            "status": "filled",
            "qty": "1",
            "filled_qty": "1",
            "filled_avg_price": "100",
            "fee": "0",
        },
    )
    repository.save_evidence(evidence)
    first = ValidateAlpacaPaperFill(repository=repository).execute(
        instruction_id=instruction.instruction_id, evidence_id=evidence.evidence_id
    )
    replay = ValidateAlpacaPaperFill(repository=repository).execute(
        instruction_id=instruction.instruction_id, evidence_id=evidence.evidence_id
    )

    assert first.validated_fill_id == replay.validated_fill_id
    assert first.state.value == "VALIDATED"


@pytest.mark.parametrize(
    ("error", "next_action"),
    (("HTTP_401", "AUTHENTICATION_REJECTED"), ("TRANSPORT_UNAVAILABLE", "PROVIDER_UNAVAILABLE")),
)
def test_alpaca_paper_sync_reports_provider_failures_without_a_write(
    error: str, next_action: str
) -> None:
    repository = InMemoryExternalPaperRepository()
    instruction = _submitted_instruction(repository, key=f"alpaca-sync-{error}")

    def unavailable(
        _method: str, _url: str, _headers: dict[str, str], _body: bytes | None
    ) -> AlpacaPaperResponse:
        raise ExternalPaperBrokerError(error)

    adapter = AlpacaPaperBroker(
        credentials=AlpacaPaperCredentials("TEST", "paper-key", "paper-secret"), request=unavailable
    )
    result = SynchronizeAlpacaPaperInstruction(repository=repository, broker=adapter).execute(
        instruction_id=instruction.instruction_id
    )

    assert result.next_action == next_action
    assert result.broker_writes is False
    assert result.financial_mutations == 0


def test_paper_capability_is_explicit_and_instruction_is_bounded() -> None:
    capability = PaperBrokerCapabilitySnapshot.create(
        state=ExternalPaperCapabilityState.CREDENTIALS_REQUIRED,
        credentials_available=False,
        authentication_state="NOT_ATTEMPTED",
        account_read=False,
        order_submit=False,
        order_read=False,
        cancel=False,
        position_read=False,
        activity_read=False,
        checked_at=NOW,
        reason_codes=("PAPER_CREDENTIALS_REQUIRED",),
    )
    instruction = ExternalPaperInstruction.authorized(
        authorization=_authorization(),
        symbol="SPY",
        quantity="1",
        maximum_notional="100",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="fixture-full-fill",
    )

    assert capability.state is ExternalPaperCapabilityState.CREDENTIALS_REQUIRED
    assert instruction.state is ExternalPaperInstructionState.AUTHORIZED
    assert instruction.side == "BUY"
    assert instruction.time_in_force == "DAY"
    with pytest.raises(ValueError, match="exceeds authorization"):
        ExternalPaperInstruction.authorized(
            authorization=_authorization(),
            symbol="QQQ",
            quantity="1",
            maximum_notional="100",
            expires_at=NOW + timedelta(hours=1),
            idempotency_key="wrong-symbol",
        )


def test_ambiguous_submission_fails_closed_and_broker_mismatch_is_material() -> None:
    instruction = (
        ExternalPaperInstruction.authorized(
            authorization=_authorization(),
            symbol="SPY",
            quantity="1",
            maximum_notional="100",
            expires_at=NOW + timedelta(hours=1),
            idempotency_key="fixture-ambiguous",
        )
        .transition(state=ExternalPaperInstructionState.SUBMISSION_PENDING)
        .transition(
            state=ExternalPaperInstructionState.RECONCILIATION_REQUIRED,
            reason_code="SUBMISSION_TIMEOUT",
        )
    )
    ambiguous = reconcile_external_paper(instruction=instruction, broker_order=None)
    mismatch = reconcile_external_paper(
        instruction=ExternalPaperInstruction.authorized(
            authorization=_authorization(),
            symbol="SPY",
            quantity="1",
            maximum_notional="100",
            expires_at=NOW + timedelta(hours=1),
            idempotency_key="fixture-mismatch",
        ),
        broker_order={"symbol": "QQQ", "side": "buy"},
    )

    assert (
        ambiguous.classification is ExternalPaperReconciliationClassification.SUBMISSION_AMBIGUOUS
    )
    assert mismatch.classification is ExternalPaperReconciliationClassification.MATERIAL_DISCREPANCY


def test_reconciliation_projection_is_retained_by_instruction() -> None:
    authorization = _authorization()
    repository = InMemoryExternalPaperRepository()
    repository.save_authorization(authorization)
    instruction = ExternalPaperInstruction.authorized(
        authorization=authorization,
        symbol="SPY",
        quantity="1",
        maximum_notional="100",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="reconciliation-projection",
    )
    repository.save_instruction(instruction)
    report = reconcile_external_paper(
        instruction=instruction,
        broker_order={"id": "different-broker-order", "status": "accepted"},
    )

    retained = repository.save_reconciliation(report)

    assert repository.reconciliations_for_instruction(instruction.instruction_id) == (retained,)


def test_paper_adapter_uses_only_paper_host_and_retains_sanitized_acknowledgment() -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []

    def request(
        method: str, url: str, headers: dict[str, str], _body: bytes | None
    ) -> AlpacaPaperResponse:
        calls.append((method, url, headers))
        return AlpacaPaperResponse(
            200,
            {
                "id": "broker-order-1",
                "symbol": "SPY",
                "side": "buy",
                "status": "accepted",
                "qty": "1",
                "submitted_at": "2026-07-28T20:00:00Z",
            },
            "request-1",
        )

    adapter = AlpacaPaperBroker(
        credentials=AlpacaPaperCredentials("TEST", "paper-key", "paper-secret"),
        request=request,
        allow_submission=True,
    )
    evidence = adapter.submit(
        ExternalPaperInstruction.authorized(
            authorization=_authorization(),
            symbol="SPY",
            quantity="1",
            maximum_notional="100",
            expires_at=NOW + timedelta(hours=1),
            idempotency_key="adapter-submit",
        )
    )

    assert calls[0][0] == "POST"
    assert calls[0][1].startswith("https://paper-api.alpaca.markets/")
    assert evidence.endpoint_identity == "PAPER:POST:/v2/orders"
    assert "paper-secret" not in str(evidence)
    assert evidence.sanitized_payload["id"] == "broker-order-1"


def test_submission_is_idempotent_and_ambiguous_timeout_requires_reconciliation() -> None:
    authorization = _authorization()
    repository = InMemoryExternalPaperRepository()
    repository.authorizations[authorization.authorization_id] = authorization
    instruction = ExternalPaperInstruction.authorized(
        authorization=authorization,
        symbol="SPY",
        quantity="1",
        maximum_notional="100",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="submission-success",
    )
    repository.save_instruction(instruction)
    _bind(repository, authorization, instruction)

    class Broker:
        calls = 0

        def submit(self, submitted: ExternalPaperInstruction) -> ExternalPaperBrokerEvidence:
            self.calls += 1
            return ExternalPaperBrokerEvidence.create(
                instruction_id=submitted.instruction_id,
                endpoint_identity="PAPER:POST:/v2/orders",
                observation_type="ORDER_ACKNOWLEDGMENT",
                observed_at=NOW,
                broker_reported_at=NOW,
                request_id="fixture-request",
                sanitized_payload={"id": "fixture-order", "status": "accepted", "symbol": "SPY"},
            )

    broker = Broker()
    service = SubmitAuthorizedPaperInstruction(repository=repository, broker=broker)
    first = service.execute(
        instruction_id=instruction.instruction_id, capability=ready_capability(NOW), now=NOW
    )
    replay = service.execute(
        instruction_id=instruction.instruction_id, capability=ready_capability(NOW), now=NOW
    )
    assert first.instruction.state is ExternalPaperInstructionState.SUBMITTED
    assert replay.replayed
    assert broker.calls == 1

    ambiguous_instruction = ExternalPaperInstruction.authorized(
        authorization=authorization,
        symbol="SPY",
        quantity="1",
        maximum_notional="100",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="submission-ambiguous",
    )
    repository.save_instruction(ambiguous_instruction)
    _bind(repository, authorization, ambiguous_instruction)

    ambiguous_broker = DeterministicPaperBrokerFixture(
        scenarios={"submission-ambiguous": AMBIGUOUS}, now=NOW
    )
    ambiguous = SubmitAuthorizedPaperInstruction(
        repository=repository, broker=ambiguous_broker
    ).execute(
        instruction_id=ambiguous_instruction.instruction_id,
        capability=ready_capability(NOW),
        now=NOW,
    )
    assert ambiguous.instruction.state is ExternalPaperInstructionState.RECONCILIATION_REQUIRED
    recovered = ResolveAmbiguousExternalPaperSubmission(
        repository=repository, broker=ambiguous_broker
    ).execute(instruction_id=ambiguous_instruction.instruction_id)
    assert recovered.state is ExternalPaperInstructionState.SUBMITTED
    assert recovered.broker_order_id is not None


def test_fixture_retains_partial_cancel_and_ambiguous_observations_without_network() -> None:
    authorization = _authorization()
    repository = InMemoryExternalPaperRepository()
    repository.save_authorization(authorization)
    partial = ExternalPaperInstruction.authorized(
        authorization=authorization,
        symbol="SPY",
        quantity="1",
        maximum_notional="100",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="partial",
    )
    ambiguous = ExternalPaperInstruction.authorized(
        authorization=authorization,
        symbol="SPY",
        quantity="1",
        maximum_notional="100",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="ambiguous",
    )
    repository.save_instruction(partial)
    repository.save_instruction(ambiguous)
    _bind(repository, authorization, partial)
    _bind(repository, authorization, ambiguous)
    fixture = DeterministicPaperBrokerFixture(
        scenarios={"partial": PARTIAL_FILL, "ambiguous": AMBIGUOUS}, now=NOW
    )
    submitted = SubmitAuthorizedPaperInstruction(repository=repository, broker=fixture).execute(
        instruction_id=partial.instruction_id, capability=ready_capability(NOW), now=NOW
    )
    partial_state, report = SyncExternalPaperInstruction(
        repository=repository, broker=fixture
    ).execute(instruction_id=partial.instruction_id)
    cancellation = CancelExternalPaperInstruction(repository=repository, broker=fixture).execute(
        instruction_id=partial.instruction_id
    )
    cancellation_replay = CancelExternalPaperInstruction(
        repository=repository, broker=fixture
    ).execute(instruction_id=partial.instruction_id)
    ambiguous_result = SubmitAuthorizedPaperInstruction(
        repository=repository, broker=fixture
    ).execute(instruction_id=ambiguous.instruction_id, capability=ready_capability(NOW), now=NOW)

    assert submitted.instruction.broker_order_id is not None
    assert partial_state.state is ExternalPaperInstructionState.PARTIALLY_FILLED
    assert report.classification is ExternalPaperReconciliationClassification.RECONCILED
    assert cancellation.sanitized_payload["status"] == "canceled"
    assert cancellation_replay.evidence_id == cancellation.evidence_id
    assert (
        ambiguous_result.instruction.state is ExternalPaperInstructionState.RECONCILIATION_REQUIRED
    )
    assert fixture.network_calls == 0
    assert len(fixture.evidence) >= 3


def test_validated_external_fill_is_exactly_idempotent() -> None:
    authorization = _authorization()
    repository = InMemoryExternalPaperRepository()
    repository.save_authorization(authorization)
    instruction = ExternalPaperInstruction.authorized(
        authorization=authorization,
        symbol="SPY",
        quantity="1",
        maximum_notional="100",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="validated-full",
    )
    repository.save_instruction(instruction)
    _bind(repository, authorization, instruction)
    fixture = DeterministicPaperBrokerFixture(scenarios={"validated-full": FULL_FILL}, now=NOW)
    submission = SubmitAuthorizedPaperInstruction(repository=repository, broker=fixture).execute(
        instruction_id=instruction.instruction_id, capability=ready_capability(NOW), now=NOW
    )
    assert submission.evidence is not None
    validator = ValidateExternalPaperFill(repository=repository)
    first = validator.execute(evidence=submission.evidence)
    replay = validator.execute(evidence=submission.evidence)

    assert first.state.value == "VALIDATED"
    assert first.validated_fill_id == replay.validated_fill_id
    assert first.newly_observed_quantity == Decimal("1")


def test_pending_internal_execution_binding_is_exactly_idempotent() -> None:
    authorization = _authorization()
    repository = InMemoryExternalPaperRepository()
    repository.save_authorization(authorization)
    instruction = ExternalPaperInstruction.authorized(
        authorization=authorization,
        symbol="SPY",
        quantity="1",
        maximum_notional="100",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="binding",
    )
    repository.save_instruction(instruction)
    binding = ExternalPaperInternalExecutionBinding.pending(
        authorization=authorization,
        instruction=instruction,
        securities_paper_run_id="internal-run",
        internal_order_id="internal-order",
        risk_reservation_id="reservation",
        execution_policy_id="SECURITIES_NEXT_BAR_CROSS_V1",
        execution_policy_hash="a" * 64,
        instrument_id="instrument-spy",
        intended_session="2026-07-29",
        idempotency_key="binding",
    )
    service = PrepareAuthorizedExternalPaperExecution(repository=repository)
    first = service.execute(binding=binding, now=NOW)
    replay = service.execute(binding=binding, now=NOW)

    assert first.binding_id == replay.binding_id
    assert first.internal_order_id == "internal-order"


def test_kill_blocks_new_openings_idempotently_without_broker_access() -> None:
    repository = InMemoryExternalPaperRepository()
    service = KillExternalPaper(repository=repository)
    confirmation = external_paper_kill_confirmation(campaign_id="campaign-paper-fixture")
    assert not service.execute(
        campaign_id="campaign-paper-fixture",
        reason="operator stop",
        confirmation_text=confirmation,
        operator_action_id="fixture-kill",
    )
    assert repository.external_paper_killed("campaign-paper-fixture")
    assert service.execute(
        campaign_id="campaign-paper-fixture",
        reason="operator stop",
        confirmation_text=confirmation,
        operator_action_id="fixture-kill",
    )


def test_unknown_broker_order_state_requires_reconciliation() -> None:
    authorization = _authorization()
    repository = InMemoryExternalPaperRepository()
    repository.save_authorization(authorization)
    instruction = (
        ExternalPaperInstruction.authorized(
            authorization=authorization,
            symbol="SPY",
            quantity="1",
            maximum_notional="100",
            expires_at=NOW + timedelta(hours=1),
            idempotency_key="unknown-state",
        )
        .transition(state=ExternalPaperInstructionState.SUBMISSION_PENDING)
        .transition(
            state=ExternalPaperInstructionState.SUBMITTED,
            broker_order_id="broker-unknown",
        )
    )
    repository.save_instruction(instruction)
    _bind(repository, authorization, instruction)

    class UnknownStateBroker:
        def order(
            self, *, instruction_id: str, broker_order_id: str
        ) -> ExternalPaperBrokerEvidence:
            return ExternalPaperBrokerEvidence.create(
                instruction_id=instruction_id,
                endpoint_identity="PAPER:GET:/v2/orders/{order_id}",
                observation_type="ORDER_STATUS",
                observed_at=NOW,
                broker_reported_at=NOW,
                request_id="unknown-state",
                sanitized_payload={
                    "id": broker_order_id,
                    "symbol": "SPY",
                    "side": "buy",
                    "qty": "1",
                    "status": "unexpected",
                },
            )

    synced, report = SyncExternalPaperInstruction(
        repository=repository, broker=UnknownStateBroker()
    ).execute(instruction_id=instruction.instruction_id)
    assert synced.state is ExternalPaperInstructionState.RECONCILIATION_REQUIRED
    assert report.classification is ExternalPaperReconciliationClassification.SUBMISSION_AMBIGUOUS


def test_campaign_sync_retains_unknown_fill_activity_and_blocks_an_opening() -> None:
    authorization = _authorization()
    repository = InMemoryExternalPaperRepository()
    repository.save_authorization(authorization)
    instruction = ExternalPaperInstruction.authorized(
        authorization=authorization,
        symbol="SPY",
        quantity="1",
        maximum_notional="100",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="unknown-activity",
    )
    repository.save_instruction(instruction)
    _bind(repository, authorization, instruction)

    class Broker:
        def account(self) -> ExternalPaperBrokerEvidence:
            return ExternalPaperBrokerEvidence.create(
                instruction_id=None,
                endpoint_identity="PAPER:GET:/v2/account",
                observation_type="ACCOUNT_READINESS",
                observed_at=NOW,
                broker_reported_at=NOW,
                request_id="account",
                sanitized_payload={"cash": "100", "buying_power": "100"},
            )

        def orders(self) -> tuple[ExternalPaperBrokerEvidence, ...]:
            return ()

        def positions(self) -> tuple[ExternalPaperBrokerEvidence, ...]:
            return ()

        def activities(self) -> tuple[ExternalPaperBrokerEvidence, ...]:
            return (
                ExternalPaperBrokerEvidence.create(
                    instruction_id=None,
                    endpoint_identity="PAPER:GET:/v2/account/activities",
                    observation_type="FILL_ACTIVITY_ITEM",
                    observed_at=NOW,
                    broker_reported_at=NOW,
                    request_id="activity",
                    sanitized_payload={"order_id": "unrecognized-order", "symbol": "SPY"},
                ),
            )

        def order(
            self, *, instruction_id: str, broker_order_id: str
        ) -> ExternalPaperBrokerEvidence:
            raise AssertionError("unbound instruction has no broker order")

    SynchronizeExternalPaperCampaign(repository=repository, broker=Broker()).execute(
        campaign_id=authorization.campaign_id
    )
    assert repository.has_unknown_broker_activity(authorization.campaign_id)
    blocked = SubmitAuthorizedPaperInstruction(repository=repository, broker=Broker()).execute(
        instruction_id=instruction.instruction_id,
        capability=ready_capability(NOW),
        now=NOW,
    )
    assert blocked.instruction.state is ExternalPaperInstructionState.BLOCKED
    assert blocked.instruction.reason_code == "UNKNOWN_BROKER_ACTIVITY_BLOCK"


def test_paper_adapter_sanitizes_bounded_collection_reads() -> None:
    calls: list[str] = []

    def request(
        _method: str, url: str, _headers: dict[str, str], _body: bytes | None
    ) -> AlpacaPaperResponse:
        calls.append(url)
        return AlpacaPaperResponse(
            200,
            [{"id": "order-1", "symbol": "SPY", "status": "filled", "token": "never"}],
            "request-list",
        )

    adapter = AlpacaPaperBroker(
        credentials=AlpacaPaperCredentials("TEST", "paper-key", "paper-secret"), request=request
    )
    evidence = adapter.orders()

    assert calls == [
        "https://paper-api.alpaca.markets/v2/orders?status=all&limit=500&direction=desc"
    ]
    assert evidence[0].endpoint_identity == "PAPER:GET:/v2/orders"
    assert evidence[0].sanitized_payload == {
        "id": "order-1",
        "symbol": "SPY",
        "status": "filled",
    }


def test_alpaca_readiness_requires_both_isolated_credentials_without_requests() -> None:
    calls: list[str] = []

    def request(
        _method: str, url: str, _headers: dict[str, str], _body: bytes | None
    ) -> AlpacaPaperResponse:
        calls.append(url)
        raise AssertionError("absent credentials must not call PAPER")

    readiness = AlpacaPaperBroker(
        credentials=AlpacaPaperCredentials("TEST", None, None), request=request
    ).check_readiness(checked_at=NOW)

    assert readiness.capability.state is ExternalPaperCapabilityState.CREDENTIALS_REQUIRED
    assert readiness.evidence == ()
    assert calls == []


def test_alpaca_readiness_retains_sanitized_independent_reads() -> None:
    calls: list[str] = []

    def request(
        _method: str, url: str, _headers: dict[str, str], _body: bytes | None
    ) -> AlpacaPaperResponse:
        calls.append(url)
        if url.endswith("/account"):
            return AlpacaPaperResponse(200, {"status": "ACTIVE", "cash": "1000"}, "account-id")
        if "/orders?" in url:
            return AlpacaPaperResponse(200, [], "orders-id")
        if url.endswith("/positions"):
            return AlpacaPaperResponse(200, [], "positions-id")
        if "/account/activities?" in url:
            return AlpacaPaperResponse(200, [], "activities-id")
        raise AssertionError(url)

    repository = InMemoryExternalPaperRepository()
    readiness = CheckAlpacaPaperReadiness(
        repository=repository,
        broker=AlpacaPaperBroker(
            credentials=AlpacaPaperCredentials("TEST", "paper-key", "paper-secret"),
            request=request,
        ),
    ).execute(now=NOW)

    assert readiness.capability.state is ExternalPaperCapabilityState.READY
    assert readiness.capability.order_read
    assert readiness.capability.position_read
    assert readiness.capability.activity_read
    assert readiness.endpoint_states == (
        ("account", "READY"),
        ("orders", "READY"),
        ("positions", "READY"),
        ("activities", "READY"),
    )
    assert len(readiness.evidence_ids) == 1
    assert len(repository.capabilities) == 1
    assert all(url.startswith("https://paper-api.alpaca.markets/") for url in calls)


def test_flatten_requires_exact_confirmation_and_kills_new_openings_without_network() -> None:
    repository = InMemoryExternalPaperRepository()
    fixture = DeterministicPaperBrokerFixture(scenarios={}, now=NOW)
    from apex.application.securities_paper import InMemorySecuritiesPaperRepository

    service = FlattenExternalPaperCampaign(
        repository=repository,
        broker=fixture,
        securities_repository=InMemorySecuritiesPaperRepository(),
    )
    with pytest.raises(Exception, match="EXACT_OPERATOR_CONFIRMATION_REQUIRED"):
        service.execute(
            campaign_id="campaign-flatten",
            reason="fixture drain",
            confirmation_text="wrong",
            operator_action_id="fixture-flatten",
        )
    result = service.execute(
        campaign_id="campaign-flatten",
        reason="fixture drain",
        confirmation_text=external_paper_flatten_confirmation(campaign_id="campaign-flatten"),
        operator_action_id="fixture-flatten",
    )
    assert result.completed
    assert repository.external_paper_killed("campaign-flatten")
    assert fixture.network_calls == 0


def test_flatten_remains_incomplete_while_an_external_paper_position_is_open() -> None:
    repository = InMemoryExternalPaperRepository()
    fixture = DeterministicPaperBrokerFixture(scenarios={}, now=NOW)

    class SecuritiesRepository:
        def positions(self, campaign_id: str) -> tuple[SimpleNamespace, ...]:
            assert campaign_id == "campaign-open-position"
            return (
                SimpleNamespace(
                    position_id="external-paper-open-position",
                    open_quantity=SimpleNamespace(value=Decimal("1")),
                ),
            )

    result = FlattenExternalPaperCampaign(
        repository=repository,
        broker=fixture,
        securities_repository=SecuritiesRepository(),  # type: ignore[arg-type]
    ).execute(
        campaign_id="campaign-open-position",
        reason="fixture drain",
        confirmation_text=external_paper_flatten_confirmation(campaign_id="campaign-open-position"),
        operator_action_id="fixture-flatten-open-position",
    )

    assert not result.completed
    assert result.blockers == ("OPEN_POSITION:external-paper-open-position",)
    assert repository.external_paper_killed("campaign-open-position")
    assert fixture.network_calls == 0
