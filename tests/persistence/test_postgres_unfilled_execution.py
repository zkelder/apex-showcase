from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex import ENGINE_VERSION
from apex.adapters.fixtures import FixtureReader
from apex.adapters.postgres import (
    PostgresSettings,
    create_postgres_engine,
    durable_order,
    durable_risk_reservation,
    execution_event,
    ledger_journal,
    ledger_posting,
    postgres_uow_factory,
)
from apex.adapters.simulator import DeterministicSimulator
from apex.application.authorize_order import (
    AuthorizeAndReserveCommand,
    AuthorizedOrderResult,
    CampaignInitializationCommand,
    DurableAuthorizationService,
    IdempotencyConflictError,
    PersistAuthorizationInputsCommand,
    PersistenceConflictError,
)
from apex.application.execute_unfilled_order import (
    ApplyExecutionEventCommand,
    DurableRiskReservationState,
    DurableUnfilledExecutionService,
    ExecutionFaultPoint,
    InjectedExecutionFault,
)
from apex.application.interfaces import (
    RunMetadata,
    UnfilledExpiredResult,
    UnfilledExpiryExecutionScript,
)
from apex.application.run_simulation import GoldenSimulation
from apex.cli import _decision_inputs, _execution_script
from apex.domain.campaign import CampaignSnapshot
from apex.domain.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExpiringLimitOrder,
    OrderState,
    ReservationState,
    RiskReservation,
    apply_unfilled_order_event,
    create_reserved_order,
)
from apex.domain.values import ContentHash, Money, ValueValidationError, content_digest

pytestmark = pytest.mark.postgres


@dataclass(frozen=True, slots=True)
class ExactUnfilledCase:
    oracle: UnfilledExpiredResult
    script: UnfilledExpiryExecutionScript
    initial_order: ExpiringLimitOrder
    initial_reservation: RiskReservation


@dataclass(frozen=True, slots=True)
class StoredLifecycle:
    order: dict[str, object]
    reservation: dict[str, object]
    events: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    journals: tuple[dict[str, object], ...]
    postings: tuple[dict[str, object], ...]


def _prepare_exact_unfilled(service: DurableAuthorizationService) -> ExactUnfilledCase:
    reader = FixtureReader()
    fixture = reader.load_decision("unfilled-expired")
    decision_inputs = _decision_inputs(fixture)
    script = _execution_script(fixture)
    assert isinstance(script, UnfilledExpiryExecutionScript)
    simulation = GoldenSimulation(
        decision_inputs=decision_inputs,
        execution_script=script,
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("unfilled-expired")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        capital=Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )
    oracle = simulation.run_decision_and_execution()
    assert isinstance(oracle, UnfilledExpiredResult)
    initial_reservation, initial_order = create_reserved_order(
        campaign_id=oracle.campaign.campaign_id,
        proposal=oracle.proposal,
        allocation=oracle.allocation,
        risk_decision=oracle.risk_decision,
    )
    assert isinstance(initial_order, ExpiringLimitOrder)

    service.initialize_campaign(
        CampaignInitializationCommand(
            campaign_id=oracle.campaign.campaign_id,
            contribution=oracle.campaign.contribution,
            authorized_loss=oracle.campaign.authorized_loss,
            policy=oracle.campaign.policy,
            occurred_at=fixture.logical_time,
        )
    )
    service.persist_authorization_inputs(
        PersistAuthorizationInputsCommand(
            campaign_id=oracle.campaign.campaign_id,
            proposal=oracle.proposal,
            allocation=oracle.allocation,
            information_set=oracle.information_set,
            market_observation_source_id=decision_inputs.market_observation_source_id,
            recorded_at=fixture.logical_time,
        )
    )
    authorized = service.authorize_and_reserve(
        AuthorizeAndReserveCommand(
            campaign_id=oracle.campaign.campaign_id,
            proposal_id=oracle.proposal.proposal_id,
            allocation_id=oracle.allocation.allocation_id,
            client_order_id=initial_order.client_order_id,
            authorization_at=decision_inputs.risk_authorization_at,
        )
    )
    assert isinstance(authorized, AuthorizedOrderResult)
    assert authorized.risk_decision == oracle.risk_decision
    assert authorized.reservation == initial_reservation
    assert authorized.order == initial_order
    return ExactUnfilledCase(oracle, script, initial_order, initial_reservation)


def _execution_service(engine: Engine) -> DurableUnfilledExecutionService:
    return DurableUnfilledExecutionService(postgres_uow_factory(engine))


def _command(
    case: ExactUnfilledCase,
    event: ExecutionEvent,
    fault_at: ExecutionFaultPoint | None = None,
) -> ApplyExecutionEventCommand:
    return ApplyExecutionEventCommand(
        campaign_id=case.oracle.campaign.campaign_id,
        order_id=case.initial_order.order_id,
        script=case.script,
        event=event,
        recorded_at=event.occurred_at + timedelta(seconds=1),
        fault_at=fault_at,
    )


def _stored(engine: Engine, order_id: str) -> StoredLifecycle:
    with engine.connect() as connection:
        order_row = dict(
            connection.execute(sa.select(durable_order).where(durable_order.c.order_id == order_id))
            .mappings()
            .one()
        )
        reservation_row = dict(
            connection.execute(
                sa.select(durable_risk_reservation).where(
                    durable_risk_reservation.c.order_id == order_id
                )
            )
            .mappings()
            .one()
        )
        event_rows = tuple(
            dict(row)
            for row in connection.execute(
                sa.select(execution_event)
                .where(execution_event.c.order_id == order_id)
                .order_by(execution_event.c.sequence)
            )
            .mappings()
            .all()
        )
    return StoredLifecycle(order_row, reservation_row, event_rows)


def _ledger_snapshot(engine: Engine, campaign_id: str) -> LedgerSnapshot:
    with engine.connect() as connection:
        journal_rows = tuple(
            dict(row)
            for row in connection.execute(
                sa.select(ledger_journal)
                .where(ledger_journal.c.campaign_id == campaign_id)
                .order_by(ledger_journal.c.journal_id)
            )
            .mappings()
            .all()
        )
        posting_rows = tuple(
            dict(row)
            for row in connection.execute(
                sa.select(ledger_posting)
                .join(
                    ledger_journal,
                    ledger_journal.c.journal_id == ledger_posting.c.journal_id,
                )
                .where(ledger_journal.c.campaign_id == campaign_id)
                .order_by(
                    ledger_posting.c.journal_id,
                    ledger_posting.c.sequence,
                    ledger_posting.c.posting_id,
                )
            )
            .mappings()
            .all()
        )
    return LedgerSnapshot(journal_rows, posting_rows)


def _available_risk_capacity(uow_factory, campaign_id: str) -> Money:
    with uow_factory() as unit_of_work:
        campaign = unit_of_work.campaigns.get(campaign_id)
        assert campaign is not None
        balances = unit_of_work.ledger.balances(campaign_id)
        active_reservations = unit_of_work.reservations.active_total(campaign_id)
        capacity = Money(
            CampaignSnapshot(
                campaign=campaign,
                ledger_equity=balances.ledger_equity,
                settled_cash=balances.settled_cash,
                active_reservations=active_reservations,
            ).risk_capacity
        )
        unit_of_work.commit()
    return capacity


def _expected_order_hash(
    campaign_id: str,
    order: ExpiringLimitOrder,
    idempotency_payload_hash: object,
) -> str:
    return content_digest(
        {
            "campaign_id": campaign_id,
            "order": order,
            "runtime_capability": "SIMULATION_ONLY",
            "execution_evidence": "SYNTHETIC",
            "idempotency_payload_hash": str(idempotency_payload_hash),
        }
    )


def _expected_reservation_hash(value: DurableRiskReservationState) -> str:
    return content_digest(
        RiskReservation(
            reservation_id=value.reservation_id,
            campaign_id=value.campaign_id,
            risk_decision_id=value.risk_decision_id,
            order_id=value.order_id,
            exact_order_fingerprint=value.exact_order_fingerprint,
            maximum_loss=value.maximum_loss,
            state=value.state,
        )
    )


def _assert_event_row(
    row: dict[str, object],
    event: ExecutionEvent,
    campaign_id: str,
) -> None:
    assert row["event_id"] == event.event_id
    assert row["campaign_id"] == campaign_id
    assert row["order_id"] == event.order_id
    assert row["sequence"] == event.sequence
    assert row["event_type"] == event.event_type.value
    assert row["occurred_at"] == event.occurred_at
    assert row["canonical_payload_hash"] == event.payload_hash.value
    assert len(str(row["canonical_payload_hash"])) == 64
    assert row["created_at"] == event.occurred_at + timedelta(seconds=1)


def test_exact_unfilled_fixture_persists_open_and_expiration_without_financial_state(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
    uow_factory,
) -> None:
    case = _prepare_exact_unfilled(service)
    execution = _execution_service(postgres_engine)
    opened_event, expired_event = case.oracle.execution_events
    campaign_id = case.oracle.campaign.campaign_id
    initial = _stored(postgres_engine, case.initial_order.order_id)
    financial_before_open = _ledger_snapshot(postgres_engine, campaign_id)
    expected_open = apply_unfilled_order_event(
        case.initial_order,
        case.initial_reservation,
        opened_event,
    )
    expected_final = apply_unfilled_order_event(
        expected_open.order,
        expected_open.reservation,
        expired_event,
    )

    assert initial.order["state"] == OrderState.READY.value
    assert initial.order["last_execution_sequence"] == 0
    assert initial.order["row_version"] == 1
    assert initial.reservation["maximum_loss"] == case.initial_reservation.maximum_loss.amount
    assert initial.reservation["current_amount"] == case.initial_reservation.maximum_loss.amount
    assert initial.reservation["state"] == ReservationState.HELD.value
    assert initial.reservation["released_at"] is None
    assert initial.events == ()
    assert len(financial_before_open.journals) == 1
    assert len(financial_before_open.postings) == 2

    opened = execution.apply_execution_event(_command(case, opened_event))
    stored_open = _stored(postgres_engine, case.initial_order.order_id)
    assert opened.idempotent_replay is False
    assert opened.released_maximum_loss == Money(0)
    assert opened.state.order == expected_open.order
    assert opened.state.order.state is OrderState.OPEN
    assert opened.state.order.last_execution_sequence == 1
    assert opened.state.order_row_version == 2
    assert opened.state.reservation.maximum_loss == case.initial_reservation.maximum_loss
    assert opened.state.reservation.current_amount == case.initial_reservation.maximum_loss
    assert opened.state.reservation.state is ReservationState.OPEN
    assert opened.state.reservation.released_at is None
    assert opened.state.execution_events == (opened_event,)
    assert stored_open.order["state"] == OrderState.OPEN.value
    assert stored_open.order["last_execution_sequence"] == 1
    assert stored_open.order["row_version"] == 2
    assert (
        stored_open.order["reserved_maximum_loss"] == case.initial_reservation.maximum_loss.amount
    )
    assert stored_open.order["canonical_payload_hash"] == _expected_order_hash(
        case.oracle.campaign.campaign_id,
        expected_open.order,
        initial.order["idempotency_payload_hash"],
    )
    assert stored_open.reservation["maximum_loss"] == case.initial_reservation.maximum_loss.amount
    assert stored_open.reservation["current_amount"] == case.initial_reservation.maximum_loss.amount
    assert stored_open.reservation["state"] == ReservationState.OPEN.value
    assert stored_open.reservation["released_at"] is None
    assert stored_open.reservation["canonical_payload_hash"] == _expected_reservation_hash(
        opened.state.reservation
    )
    assert stored_open.order["created_at"] == initial.order["created_at"]
    assert stored_open.reservation["created_at"] == initial.reservation["created_at"]
    assert stored_open.order["expires_at"] == case.initial_order.expires_at
    assert stored_open.order["executable_ask"] == case.initial_order.executable_ask.value
    assert len(stored_open.events) == 1
    _assert_event_row(stored_open.events[0], opened_event, campaign_id)
    assert _ledger_snapshot(postgres_engine, campaign_id) == financial_before_open
    assert (
        _available_risk_capacity(uow_factory, campaign_id) == case.oracle.risk_capacity_while_open
    )
    financial_before_expiration = _ledger_snapshot(postgres_engine, campaign_id)

    expired = execution.apply_execution_event(_command(case, expired_event))
    stored_final = _stored(postgres_engine, case.initial_order.order_id)
    assert expired.idempotent_replay is False
    assert expired.released_maximum_loss == case.oracle.released_reservation_amount
    assert expired.state.order == expected_final.order == case.oracle.order
    assert expired.state.order.state is OrderState.EXPIRED
    assert expired.state.order.filled_quantity.value == Decimal(0)
    assert expired.state.order.reserved_maximum_loss == Money(0)
    assert expired.state.order.last_execution_sequence == 2
    assert expired.state.order_row_version == 3
    assert expired.state.reservation.maximum_loss == case.initial_reservation.maximum_loss
    assert expired.state.reservation.current_amount == case.oracle.final_reservation.maximum_loss
    assert expired.state.reservation.state is ReservationState.RELEASED
    assert expired.state.reservation.released_at == expired_event.occurred_at
    assert expired.state.execution_events == case.oracle.execution_events
    assert stored_final.order["state"] == OrderState.EXPIRED.value
    assert stored_final.order["last_execution_sequence"] == 2
    assert stored_final.order["row_version"] == 3
    assert stored_final.order["filled_quantity"] == Decimal(0)
    assert stored_final.order["reserved_maximum_loss"] == Decimal(0)
    assert stored_final.order["canonical_payload_hash"] == _expected_order_hash(
        case.oracle.campaign.campaign_id,
        case.oracle.order,
        initial.order["idempotency_payload_hash"],
    )
    assert stored_final.reservation["maximum_loss"] == case.initial_reservation.maximum_loss.amount
    assert stored_final.reservation["current_amount"] == Decimal(0)
    assert stored_final.reservation["state"] == ReservationState.RELEASED.value
    assert stored_final.reservation["released_at"] == expired_event.occurred_at
    assert stored_final.reservation["canonical_payload_hash"] == _expected_reservation_hash(
        expired.state.reservation
    )
    assert stored_final.order["created_at"] == initial.order["created_at"]
    assert stored_final.reservation["created_at"] == initial.reservation["created_at"]
    assert len(stored_final.events) == 2
    _assert_event_row(stored_final.events[0], opened_event, campaign_id)
    _assert_event_row(stored_final.events[1], expired_event, campaign_id)
    assert _ledger_snapshot(postgres_engine, campaign_id) == financial_before_expiration
    assert financial_before_expiration == financial_before_open
    assert (
        _available_risk_capacity(uow_factory, campaign_id)
        == case.oracle.risk_capacity_after_expiration
    )

    with uow_factory() as unit_of_work:
        assert unit_of_work.unfilled_executions.count(case.initial_order.order_id) == 2
        assert unit_of_work.reservations.active_total(case.oracle.campaign.campaign_id) == Money(0)
        assert unit_of_work.ledger.journal_count(case.oracle.campaign.campaign_id) == 1
        balances = unit_of_work.ledger.balances(case.oracle.campaign.campaign_id)
        assert balances.contributions == case.oracle.campaign.contribution
        assert balances.settled_cash == case.oracle.campaign.contribution
        assert balances.ledger_equity == case.oracle.campaign.contribution
        assert balances.trading_pnl == Decimal(0)
        unit_of_work.commit()
    assert case.oracle.journals == (case.oracle.journals[0],)
    assert case.oracle.metrics.position_cost == Money(0)
    assert case.oracle.metrics.fees == Money(0)
    assert case.oracle.metrics.net_trading_pnl == Decimal(0)


def test_sequential_replays_are_idempotent_and_conflicting_sequence_is_rejected(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
) -> None:
    case = _prepare_exact_unfilled(service)
    execution = _execution_service(postgres_engine)
    campaign_id = case.oracle.campaign.campaign_id
    opened_event, expired_event = case.oracle.execution_events
    alternate_script = replace(case.script, script_id=f"{case.script.script_id}-conflict")
    alternate_events = DeterministicSimulator().simulate(
        case.initial_order,
        alternate_script,
        case.oracle.information_set.market,
    )
    financial_baseline = _ledger_snapshot(postgres_engine, campaign_id)

    first_open = execution.apply_execution_event(_command(case, opened_event))
    assert first_open.idempotent_replay is False
    assert _ledger_snapshot(postgres_engine, campaign_id) == financial_baseline

    stored_before_open_replay = _stored(postgres_engine, case.initial_order.order_id)
    financial_before_open_replay = _ledger_snapshot(postgres_engine, campaign_id)
    replay_open = execution.apply_execution_event(_command(case, opened_event))
    assert replay_open.idempotent_replay is True
    assert replay_open.state == first_open.state
    assert _stored(postgres_engine, case.initial_order.order_id) == stored_before_open_replay
    assert (
        _ledger_snapshot(postgres_engine, campaign_id)
        == financial_before_open_replay
        == financial_baseline
    )

    same_id_conflict = replace(opened_event, fee=Money("0.01"))
    stored_before_same_id_conflict = _stored(postgres_engine, case.initial_order.order_id)
    financial_before_same_id_conflict = _ledger_snapshot(postgres_engine, campaign_id)
    with pytest.raises(IdempotencyConflictError):
        execution.apply_execution_event(_command(case, same_id_conflict))
    assert _stored(postgres_engine, case.initial_order.order_id) == stored_before_same_id_conflict
    assert (
        _ledger_snapshot(postgres_engine, campaign_id)
        == financial_before_same_id_conflict
        == financial_baseline
    )

    stored_before_open_sequence_conflict = _stored(postgres_engine, case.initial_order.order_id)
    financial_before_open_sequence_conflict = _ledger_snapshot(postgres_engine, campaign_id)
    with pytest.raises(IdempotencyConflictError):
        execution.apply_execution_event(
            replace(
                _command(case, alternate_events[0]),
                script=alternate_script,
            )
        )
    assert (
        _stored(postgres_engine, case.initial_order.order_id)
        == stored_before_open_sequence_conflict
    )
    assert (
        _ledger_snapshot(postgres_engine, campaign_id)
        == financial_before_open_sequence_conflict
        == financial_baseline
    )

    first_expired = execution.apply_execution_event(_command(case, expired_event))
    assert first_expired.idempotent_replay is False
    assert _ledger_snapshot(postgres_engine, campaign_id) == financial_baseline

    stored_before_expired_replay = _stored(postgres_engine, case.initial_order.order_id)
    financial_before_expired_replay = _ledger_snapshot(postgres_engine, campaign_id)
    replay_expired = execution.apply_execution_event(_command(case, expired_event))
    assert replay_expired.idempotent_replay is True
    assert replay_expired.released_maximum_loss == case.initial_reservation.maximum_loss
    assert replay_expired.state == first_expired.state
    assert _stored(postgres_engine, case.initial_order.order_id) == stored_before_expired_replay
    assert (
        _ledger_snapshot(postgres_engine, campaign_id)
        == financial_before_expired_replay
        == financial_baseline
    )

    stored_before_late_open_replay = _stored(postgres_engine, case.initial_order.order_id)
    financial_before_late_open_replay = _ledger_snapshot(postgres_engine, campaign_id)
    late_open_replay = execution.apply_execution_event(_command(case, opened_event))
    assert late_open_replay.idempotent_replay is True
    assert late_open_replay.released_maximum_loss == Money(0)
    assert late_open_replay.state == first_expired.state
    assert _stored(postgres_engine, case.initial_order.order_id) == stored_before_late_open_replay
    assert (
        _ledger_snapshot(postgres_engine, campaign_id)
        == financial_before_late_open_replay
        == financial_baseline
    )

    stored_before_expired_sequence_conflict = _stored(postgres_engine, case.initial_order.order_id)
    financial_before_expired_sequence_conflict = _ledger_snapshot(postgres_engine, campaign_id)
    with pytest.raises(IdempotencyConflictError):
        execution.apply_execution_event(
            replace(
                _command(case, alternate_events[1]),
                script=alternate_script,
            )
        )
    assert (
        _stored(postgres_engine, case.initial_order.order_id)
        == stored_before_expired_sequence_conflict
    )
    assert (
        _ledger_snapshot(postgres_engine, campaign_id)
        == financial_before_expired_sequence_conflict
        == financial_baseline
    )
    assert len(_stored(postgres_engine, case.initial_order.order_id).events) == 2


def test_invalid_or_out_of_sequence_evidence_leaves_ready_state_unchanged(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
) -> None:
    case = _prepare_exact_unfilled(service)
    execution = _execution_service(postgres_engine)
    campaign_id = case.oracle.campaign.campaign_id
    opened_event, expired_event = case.oracle.execution_events
    before = _stored(postgres_engine, case.initial_order.order_id)
    financial_before = _ledger_snapshot(postgres_engine, campaign_id)

    def assert_unchanged() -> None:
        assert _stored(postgres_engine, case.initial_order.order_id) == before
        assert _ledger_snapshot(postgres_engine, campaign_id) == financial_before

    with pytest.raises(ValueValidationError, match="out of sequence"):
        execution.apply_execution_event(_command(case, expired_event))
    assert_unchanged()

    with pytest.raises(ValueValidationError, match="only OPENED or EXPIRED"):
        execution.apply_execution_event(
            _command(case, replace(opened_event, event_type=ExecutionEventType.FILL))
        )
    assert_unchanged()

    with pytest.raises(ValueValidationError, match="payload hash is not canonical"):
        execution.apply_execution_event(
            _command(case, replace(opened_event, payload_hash=ContentHash("0" * 64)))
        )
    assert_unchanged()

    with pytest.raises(ValueValidationError, match="another order"):
        execution.apply_execution_event(
            _command(case, replace(opened_event, order_id="order-not-authorized"))
        )
    assert_unchanged()

    with pytest.raises(ValueValidationError, match="OPENED event does not match"):
        execution.apply_execution_event(
            _command(
                case,
                replace(
                    opened_event,
                    occurred_at=opened_event.occurred_at + timedelta(seconds=1),
                ),
            )
        )
    assert_unchanged()

    with pytest.raises(ValueValidationError, match="OPENED event does not match"):
        execution.apply_execution_event(
            _command(case, replace(opened_event, sequence=opened_event.sequence + 1))
        )
    assert_unchanged()

    with pytest.raises(ValueValidationError, match="EXPIRED event does not match"):
        execution.apply_execution_event(
            _command(
                case,
                replace(
                    expired_event,
                    occurred_at=expired_event.occurred_at - timedelta(seconds=1),
                ),
            )
        )
    assert_unchanged()

    with pytest.raises(ValueError, match="recorded before"):
        execution.apply_execution_event(
            replace(
                _command(case, opened_event),
                recorded_at=opened_event.occurred_at - timedelta(seconds=1),
            )
        )
    assert_unchanged()

    with pytest.raises(PersistenceConflictError, match="another campaign"):
        execution.apply_execution_event(
            replace(_command(case, opened_event), campaign_id="campaign-not-authorized")
        )
    assert_unchanged()

    recovered = execution.recover(
        campaign_id,
        case.initial_order.order_id,
    )
    assert recovered.order == case.initial_order
    assert recovered.reservation.state is ReservationState.HELD
    assert recovered.execution_events == ()
    assert_unchanged()


@pytest.mark.parametrize("fault_at", tuple(ExecutionFaultPoint))
@pytest.mark.parametrize("phase", (ExecutionEventType.OPENED, ExecutionEventType.EXPIRED))
def test_each_execution_fault_rolls_back_and_clean_retry_succeeds(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
    fault_at: ExecutionFaultPoint,
    phase: ExecutionEventType,
) -> None:
    case = _prepare_exact_unfilled(service)
    execution = _execution_service(postgres_engine)
    campaign_id = case.oracle.campaign.campaign_id
    opened_event, expired_event = case.oracle.execution_events
    event = opened_event
    expected_state = OrderState.READY
    expected_reservation_state = ReservationState.HELD
    expected_count = 0
    if phase is ExecutionEventType.EXPIRED:
        execution.apply_execution_event(_command(case, opened_event))
        event = expired_event
        expected_state = OrderState.OPEN
        expected_reservation_state = ReservationState.OPEN
        expected_count = 1
    before = _stored(postgres_engine, case.initial_order.order_id)
    financial_before_fault = _ledger_snapshot(postgres_engine, campaign_id)

    with pytest.raises(InjectedExecutionFault):
        execution.apply_execution_event(_command(case, event, fault_at))

    after_fault = _stored(postgres_engine, case.initial_order.order_id)
    assert after_fault == before
    assert _ledger_snapshot(postgres_engine, campaign_id) == financial_before_fault
    recovered = execution.recover(
        case.oracle.campaign.campaign_id,
        case.initial_order.order_id,
    )
    assert recovered.order.state is expected_state
    assert recovered.reservation.state is expected_reservation_state
    assert len(recovered.execution_events) == expected_count
    assert _ledger_snapshot(postgres_engine, campaign_id) == financial_before_fault

    retried = execution.apply_execution_event(_command(case, event))
    assert retried.idempotent_replay is False
    assert len(retried.state.execution_events) == expected_count + 1
    assert retried.state.order.state is (
        OrderState.OPEN if phase is ExecutionEventType.OPENED else OrderState.EXPIRED
    )
    assert _ledger_snapshot(postgres_engine, campaign_id) == financial_before_fault


def test_restart_recovery_uses_fresh_engines_for_each_lifecycle_step(
    service: DurableAuthorizationService,
    postgres_settings: PostgresSettings,
) -> None:
    case = _prepare_exact_unfilled(service)
    campaign_id = case.oracle.campaign.campaign_id
    opened_event, expired_event = case.oracle.execution_events

    first_engine = create_postgres_engine(postgres_settings)
    first_service = _execution_service(first_engine)
    financial_baseline = _ledger_snapshot(first_engine, campaign_id)
    try:
        opened = first_service.apply_execution_event(_command(case, opened_event))
        assert opened.state.order.state is OrderState.OPEN
        assert opened.state.order_row_version == 2
        assert _ledger_snapshot(first_engine, campaign_id) == financial_baseline
    finally:
        first_engine.dispose()
        del first_service

    second_engine = create_postgres_engine(postgres_settings)
    second_service = _execution_service(second_engine)
    try:
        assert second_engine is not first_engine
        recovered_open = second_service.recover(
            campaign_id,
            case.initial_order.order_id,
        )
        assert recovered_open == opened.state
        assert _ledger_snapshot(second_engine, campaign_id) == financial_baseline
        expired = second_service.apply_execution_event(_command(case, expired_event))
        assert expired.state.order == case.oracle.order
        assert expired.state.order_row_version == 3
        assert _ledger_snapshot(second_engine, campaign_id) == financial_baseline
    finally:
        second_engine.dispose()
        del second_service

    third_engine = create_postgres_engine(postgres_settings)
    third_service = _execution_service(third_engine)
    try:
        assert third_engine is not second_engine
        recovered_final = third_service.recover(
            campaign_id,
            case.initial_order.order_id,
        )
        assert recovered_final == expired.state
        assert _ledger_snapshot(third_engine, campaign_id) == financial_baseline

        stored_before_open_replay = _stored(third_engine, case.initial_order.order_id)
        financial_before_open_replay = _ledger_snapshot(third_engine, campaign_id)
        open_replay = third_service.apply_execution_event(_command(case, opened_event))
        assert open_replay.idempotent_replay is True
        assert open_replay.released_maximum_loss == Money(0)
        assert open_replay.state == recovered_final
        assert _stored(third_engine, case.initial_order.order_id) == stored_before_open_replay
        assert (
            _ledger_snapshot(third_engine, campaign_id)
            == financial_before_open_replay
            == financial_baseline
        )

        stored_before_expired_replay = _stored(third_engine, case.initial_order.order_id)
        financial_before_expired_replay = _ledger_snapshot(third_engine, campaign_id)
        expired_replay = third_service.apply_execution_event(_command(case, expired_event))
        assert expired_replay.idempotent_replay is True
        assert expired_replay.released_maximum_loss == case.initial_reservation.maximum_loss
        assert expired_replay.state == recovered_final
        assert _stored(third_engine, case.initial_order.order_id) == stored_before_expired_replay
        assert (
            _ledger_snapshot(third_engine, campaign_id)
            == financial_before_expired_replay
            == financial_baseline
        )
        final_stored = _stored(third_engine, case.initial_order.order_id)
    finally:
        third_engine.dispose()
        del third_service

    inspection_engine = create_postgres_engine(postgres_settings)
    try:
        assert inspection_engine is not third_engine
        inspected = _stored(inspection_engine, case.initial_order.order_id)
        assert inspected == final_stored
        assert len(inspected.events) == 2
        assert _ledger_snapshot(inspection_engine, campaign_id) == financial_baseline
    finally:
        inspection_engine.dispose()
