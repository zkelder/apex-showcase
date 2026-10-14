from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.postgres import (
    allocation_decision,
    ledger_journal,
    ledger_posting,
    opportunity_proposal,
)
from apex.adapters.simulator import DeterministicSimulator
from apex.application.authorize_order import (
    AuthorizeAndReserveCommand,
    AuthorizedOrderResult,
    DurableAuthorizationService,
    PersistedRiskRejection,
)
from apex.application.execute_unfilled_order import (
    ApplyExecutionEventCommand,
    DurableExecutionResult,
    DurableUnfilledExecutionService,
)
from apex.application.interfaces import UnfilledExpiryExecutionScript
from apex.domain.decisions import RestingLimitProposal
from apex.domain.execution import ExpiringLimitOrder, OrderState, ReservationState
from apex.domain.values import Money, Price

pytestmark = pytest.mark.postgres


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    journals: tuple[dict[str, object], ...]
    postings: tuple[dict[str, object], ...]


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


def _decision_evidence_counts(engine: Engine, campaign_id: str) -> tuple[int, int]:
    with engine.connect() as connection:
        proposal_count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(opportunity_proposal)
            .where(opportunity_proposal.c.campaign_id == campaign_id)
        )
        allocation_count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(allocation_decision)
            .where(allocation_decision.c.campaign_id == campaign_id)
        )
    return int(proposal_count or 0), int(allocation_count or 0)


def _authorization_command(artifacts, client_order_id: str) -> AuthorizeAndReserveCommand:
    return AuthorizeAndReserveCommand(
        campaign_id=artifacts.inputs.campaign_id,
        proposal_id=artifacts.inputs.proposal.proposal_id,
        allocation_id=artifacts.inputs.allocation.allocation_id,
        client_order_id=client_order_id,
        authorization_at=artifacts.authorization_at,
    )


def _resting_artifacts(artifacts, order_expires_at: datetime):
    proposal = artifacts.inputs.proposal
    resting = RestingLimitProposal(
        proposal_id=proposal.proposal_id,
        instrument_id=proposal.instrument_id,
        rule_version_id=proposal.rule_version_id,
        information_set_id=proposal.information_set_id,
        strategy_version=proposal.strategy_version,
        side=proposal.side,
        executable_ask=proposal.executable_ask,
        fee_model_version=proposal.fee_model_version,
        fee_per_unit=proposal.fee_per_unit,
        expected_net_value_per_unit=proposal.expected_net_value_per_unit,
        maximum_loss_per_unit=proposal.maximum_loss_per_unit,
        requested_risk_fraction=proposal.requested_risk_fraction,
        expires_at=proposal.expires_at,
        fingerprint=proposal.fingerprint,
        limit_price=Price("0.39", proposal.executable_ask.tick),
        order_expires_at=order_expires_at,
    )
    return replace(artifacts, inputs=replace(artifacts.inputs, proposal=resting))


def _prepare_unfilled_order(
    authorization_service: DurableAuthorizationService,
    artifacts,
    *,
    client_order_id: str,
    order_expires_at: datetime,
):
    resting_artifacts = _resting_artifacts(artifacts, order_expires_at)
    authorization_service.persist_authorization_inputs(resting_artifacts.inputs)
    authorized = authorization_service.authorize_and_reserve(
        _authorization_command(resting_artifacts, client_order_id)
    )
    assert isinstance(authorized, AuthorizedOrderResult)
    assert isinstance(authorized.order, ExpiringLimitOrder)
    script = UnfilledExpiryExecutionScript(
        script_id=f"script-{client_order_id}",
        kind="NONMARKETABLE_EXPIRE_UNFILLED",
        side="YES",
        executable_yes_ask="0.40",
        executable_no_ask="0.60",
        visible_yes_quantity="1000",
        opened_at=resting_artifacts.authorization_at + timedelta(seconds=1),
        expires_at=order_expires_at,
        opened_sequence=1,
        expired_sequence=2,
    )
    events = DeterministicSimulator().simulate(
        authorized.order,
        script,
        resting_artifacts.inputs.information_set.market,
    )
    return resting_artifacts, authorized, script, events


def _execution_command(
    artifacts,
    authorized: AuthorizedOrderResult,
    script: UnfilledExpiryExecutionScript,
    event,
) -> ApplyExecutionEventCommand:
    return ApplyExecutionEventCommand(
        campaign_id=artifacts.inputs.campaign_id,
        order_id=authorized.order.order_id,
        script=script,
        event=event,
        recorded_at=event.occurred_at + timedelta(seconds=1),
    )


def test_concurrent_identical_opened_event_is_inserted_once(
    service,
    initialize_campaign,
    make_artifacts,
    uow_factory,
    postgres_engine: Engine,
) -> None:
    campaign_id = "campaign-concurrent-unfilled-opened"
    initialize_campaign(campaign_id=campaign_id)
    artifacts = make_artifacts(
        "concurrent-unfilled-opened",
        campaign_id=campaign_id,
        maximum_loss="100",
    )
    expires_at = artifacts.authorization_at + timedelta(minutes=10)
    artifacts, authorized, script, events = _prepare_unfilled_order(
        service,
        artifacts,
        client_order_id="client-concurrent-unfilled-opened",
        order_expires_at=expires_at,
    )
    execution_service = DurableUnfilledExecutionService(uow_factory)
    command = _execution_command(artifacts, authorized, script, events[0])
    ledger_before = _ledger_snapshot(postgres_engine, campaign_id)
    barrier = Barrier(2)

    def open_order() -> DurableExecutionResult:
        barrier.wait(timeout=10)
        return execution_service.apply_execution_event(command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(open_order), executor.submit(open_order))
        results = tuple(future.result(timeout=15) for future in futures)

    assert _ledger_snapshot(postgres_engine, campaign_id) == ledger_before
    assert {result.idempotent_replay for result in results} == {False, True}
    assert results[0].state == results[1].state
    assert all(result.state.order.state is OrderState.OPEN for result in results)
    assert all(result.state.reservation.state is ReservationState.OPEN for result in results)
    assert all(result.state.execution_events == (events[0],) for result in results)
    with uow_factory() as unit_of_work:
        assert unit_of_work.unfilled_executions.count(authorized.order.order_id) == 1
        assert unit_of_work.reservations.active_total(campaign_id).amount == Decimal("100")
        unit_of_work.commit()


def test_concurrent_identical_expired_event_is_inserted_once_and_releases_once(
    service,
    initialize_campaign,
    make_artifacts,
    uow_factory,
    postgres_engine: Engine,
) -> None:
    campaign_id = "campaign-concurrent-unfilled-expired"
    initialize_campaign(campaign_id=campaign_id)
    artifacts = make_artifacts(
        "concurrent-unfilled-expired",
        campaign_id=campaign_id,
        maximum_loss="100",
    )
    expires_at = artifacts.authorization_at + timedelta(minutes=10)
    artifacts, authorized, script, events = _prepare_unfilled_order(
        service,
        artifacts,
        client_order_id="client-concurrent-unfilled-expired",
        order_expires_at=expires_at,
    )
    execution_service = DurableUnfilledExecutionService(uow_factory)
    execution_service.apply_execution_event(
        _execution_command(artifacts, authorized, script, events[0])
    )
    command = _execution_command(artifacts, authorized, script, events[1])
    ledger_before = _ledger_snapshot(postgres_engine, campaign_id)
    barrier = Barrier(2)

    def expire_order() -> DurableExecutionResult:
        barrier.wait(timeout=10)
        return execution_service.apply_execution_event(command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(expire_order), executor.submit(expire_order))
        results = tuple(future.result(timeout=15) for future in futures)

    assert _ledger_snapshot(postgres_engine, campaign_id) == ledger_before
    assert {result.idempotent_replay for result in results} == {False, True}
    assert results[0].state == results[1].state
    assert all(result.state.order.state is OrderState.EXPIRED for result in results)
    assert all(result.state.reservation.state is ReservationState.RELEASED for result in results)
    assert all(result.state.reservation.current_amount == Money(0) for result in results)
    assert all(result.released_maximum_loss == Money("100") for result in results)
    assert all(result.state.execution_events == events for result in results)
    with uow_factory() as unit_of_work:
        assert unit_of_work.unfilled_executions.count(authorized.order.order_id) == 2
        assert unit_of_work.reservations.active_total(campaign_id).amount == Decimal(0)
        unit_of_work.commit()


@pytest.mark.parametrize("repeat", range(3))
def test_expiration_and_new_authorization_serialize_capacity_without_overspend(
    service,
    initialize_campaign,
    make_artifacts,
    uow_factory,
    postgres_engine: Engine,
    repeat: int,
) -> None:
    campaign_id = f"campaign-expire-authorize-{repeat}"
    initialize_campaign(campaign_id=campaign_id)

    expiring_artifacts = make_artifacts(
        f"expire-authorize-{repeat}-expiring",
        campaign_id=campaign_id,
        maximum_loss="600",
    )
    expires_at = expiring_artifacts.authorization_at + timedelta(minutes=10)
    expiring_artifacts, expiring_authorized, script, events = _prepare_unfilled_order(
        service,
        expiring_artifacts,
        client_order_id=f"client-expire-authorize-{repeat}-expiring",
        order_expires_at=expires_at,
    )

    execution_service = DurableUnfilledExecutionService(uow_factory)
    execution_service.apply_execution_event(
        _execution_command(expiring_artifacts, expiring_authorized, script, events[0])
    )
    with uow_factory() as unit_of_work:
        assert unit_of_work.reservations.active_total(campaign_id).amount == Decimal("600")
        assert unit_of_work.reservations.count(campaign_id) == 1
        assert unit_of_work.orders.count(campaign_id) == 1
        unit_of_work.commit()

    contender_artifacts = make_artifacts(
        f"expire-authorize-{repeat}-contender",
        campaign_id=campaign_id,
        maximum_loss="600",
        authorization_at=expires_at + timedelta(seconds=1),
    )
    service.persist_authorization_inputs(contender_artifacts.inputs)
    ledger_before = _ledger_snapshot(postgres_engine, campaign_id)
    expire_command = _execution_command(
        expiring_artifacts,
        expiring_authorized,
        script,
        events[1],
    )
    authorize_command = _authorization_command(
        contender_artifacts,
        f"client-expire-authorize-{repeat}-contender",
    )
    barrier = Barrier(2)

    def expire_order() -> DurableExecutionResult:
        barrier.wait(timeout=10)
        return execution_service.apply_execution_event(expire_command)

    def authorize_contender() -> AuthorizedOrderResult | PersistedRiskRejection:
        barrier.wait(timeout=10)
        return service.authorize_and_reserve(authorize_command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        expiration_future = executor.submit(expire_order)
        authorization_future = executor.submit(authorize_contender)
        expiration_result = expiration_future.result(timeout=15)
        authorization_result = authorization_future.result(timeout=15)

    assert _ledger_snapshot(postgres_engine, campaign_id) == ledger_before
    assert expiration_result.idempotent_replay is False
    assert expiration_result.released_maximum_loss == Money("600")
    recovered = execution_service.recover(campaign_id, expiring_authorized.order.order_id)
    assert recovered.order.state is OrderState.EXPIRED
    assert recovered.reservation.state is ReservationState.RELEASED
    assert recovered.reservation.maximum_loss == Money("600")
    assert recovered.reservation.current_amount == Money(0)
    assert recovered.execution_events == events
    assert _decision_evidence_counts(postgres_engine, campaign_id) == (2, 2)

    with uow_factory() as unit_of_work:
        active = unit_of_work.reservations.active_total(campaign_id).amount
        assert active <= Decimal("1000")
        assert unit_of_work.unfilled_executions.count(expiring_authorized.order.order_id) == 2
        assert unit_of_work.risk_decisions.count(campaign_id) == 2
        assert unit_of_work.ledger.journal_count(campaign_id) == 1
        balances = unit_of_work.ledger.balances(campaign_id)
        assert balances.contributions.amount == Decimal("1000")
        assert balances.settled_cash.amount == Decimal("1000")
        assert balances.trading_pnl == Decimal(0)
        if isinstance(authorization_result, AuthorizedOrderResult):
            assert authorization_result.idempotent_replay is False
            assert authorization_result.capacity_before.active_order_reservations.amount == Decimal(
                0
            )
            assert authorization_result.capacity_before.available_risk_capacity.amount == Decimal(
                "1000"
            )
            assert authorization_result.order.state is OrderState.READY
            assert authorization_result.reservation.state is ReservationState.HELD
            assert active == Decimal("600")
            assert unit_of_work.reservations.count(campaign_id) == 2
            assert unit_of_work.orders.count(campaign_id) == 2
        else:
            assert isinstance(authorization_result, PersistedRiskRejection)
            assert authorization_result.idempotent_replay is False
            assert "RISK_CAPACITY_EXCEEDED" in authorization_result.risk_decision.reason_codes
            assert authorization_result.capacity_before.active_order_reservations.amount == Decimal(
                "600"
            )
            assert authorization_result.capacity_before.available_risk_capacity.amount == Decimal(
                "400"
            )
            assert active == Decimal(0)
            assert unit_of_work.reservations.count(campaign_id) == 1
            assert unit_of_work.orders.count(campaign_id) == 1
        unit_of_work.commit()
