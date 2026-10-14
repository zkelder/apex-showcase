from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.postgres import execution_event, ledger_journal, position
from apex.adapters.simulator import DeterministicSimulator
from apex.application.authorize_order import (
    AuthorizeAndReserveCommand,
    AuthorizedOrderResult,
    DurableAuthorizationService,
    PersistedRiskRejection,
)
from apex.application.execute_partial_fill_canceled_order import (
    ApplyPartialFillExecutionEventCommand,
    DurablePartialFillCanceledExecutionService,
    DurablePartialFillExecutionResult,
)
from apex.application.interfaces import PartialFillCanceledExecutionScript
from apex.domain.decisions import RestingLimitProposal
from apex.domain.execution import OrderState, ReservationState
from apex.domain.values import Money, Price

pytestmark = pytest.mark.postgres


def _authorization_command(artifacts, client_order_id: str) -> AuthorizeAndReserveCommand:
    return AuthorizeAndReserveCommand(
        campaign_id=artifacts.inputs.campaign_id,
        proposal_id=artifacts.inputs.proposal.proposal_id,
        allocation_id=artifacts.inputs.allocation.allocation_id,
        client_order_id=client_order_id,
        authorization_at=artifacts.authorization_at,
    )


def _prepare_partial_order(
    service: DurableAuthorizationService,
    artifacts,
    *,
    client_order_id: str,
):
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
        order_expires_at=artifacts.authorization_at + timedelta(minutes=10),
    )
    artifacts = replace(
        artifacts,
        inputs=replace(artifacts.inputs, proposal=resting),
    )
    service.persist_authorization_inputs(artifacts.inputs)
    authorized = service.authorize_and_reserve(_authorization_command(artifacts, client_order_id))
    assert isinstance(authorized, AuthorizedOrderResult)
    script = PartialFillCanceledExecutionScript(
        script_id=f"script-{client_order_id}",
        kind="PARTIAL_FILL_THEN_CANCEL",
        side="YES",
        executable_yes_ask="0.40",
        executable_no_ask="0.60",
        visible_yes_quantity="1000",
        ordered_quantity="250",
        fill_quantity="100",
        remaining_quantity="150",
        opened_at=artifacts.authorization_at + timedelta(seconds=1),
        filled_at=artifacts.authorization_at + timedelta(seconds=2),
        canceled_at=artifacts.authorization_at + timedelta(seconds=3),
        opened_sequence=1,
        fill_sequence=2,
        canceled_sequence=3,
        fill_price="0.39",
        fee_per_contract="0.01",
    )
    events = DeterministicSimulator().simulate(
        authorized.order,
        script,
        artifacts.inputs.information_set.market,
    )
    return artifacts, authorized, script, events


def _command(artifacts, authorized, script, event):
    return ApplyPartialFillExecutionEventCommand(
        campaign_id=artifacts.inputs.campaign_id,
        order_id=authorized.order.order_id,
        script=script,
        event=event,
        recorded_at=event.occurred_at + timedelta(seconds=1),
    )


def _counts(engine: Engine, campaign_id: str, order_id: str) -> tuple[int, int, int]:
    with engine.connect() as connection:
        events = connection.scalar(
            sa.select(sa.func.count())
            .select_from(execution_event)
            .where(execution_event.c.order_id == order_id)
        )
        positions = connection.scalar(
            sa.select(sa.func.count())
            .select_from(position)
            .where(position.c.originating_order_id == order_id)
        )
        journals = connection.scalar(
            sa.select(sa.func.count())
            .select_from(ledger_journal)
            .where(ledger_journal.c.campaign_id == campaign_id)
        )
    return int(events or 0), int(positions or 0), int(journals or 0)


def test_concurrent_identical_fill_creates_one_event_position_and_journal_pair(
    service,
    initialize_campaign,
    make_artifacts,
    uow_factory,
    postgres_engine: Engine,
) -> None:
    campaign_id = "campaign-concurrent-partial-fill"
    initialize_campaign(campaign_id=campaign_id)
    artifacts = make_artifacts(
        "concurrent-partial-fill",
        campaign_id=campaign_id,
        maximum_loss="600",
    )
    artifacts, authorized, script, events = _prepare_partial_order(
        service,
        artifacts,
        client_order_id="client-concurrent-partial-fill",
    )
    execution = DurablePartialFillCanceledExecutionService(uow_factory)
    execution.apply_execution_event(_command(artifacts, authorized, script, events[0]))
    fill_command = _command(artifacts, authorized, script, events[1])
    barrier = Barrier(2)

    def fill() -> DurablePartialFillExecutionResult:
        barrier.wait(timeout=10)
        return execution.apply_execution_event(fill_command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(fill), executor.submit(fill))
        results = tuple(future.result(timeout=15) for future in futures)

    assert {result.idempotent_replay for result in results} == {False, True}
    assert results[0].state == results[1].state
    assert all(result.state.order.state is OrderState.PARTIALLY_FILLED for result in results)
    assert all(result.state.position is not None for result in results)
    assert _counts(postgres_engine, campaign_id, authorized.order.order_id) == (2, 1, 3)
    with uow_factory() as unit_of_work:
        assert unit_of_work.reservations.active_total(campaign_id) == Money("360")
        assert unit_of_work.positions.active_total(campaign_id) == Money("240")
        unit_of_work.commit()


@pytest.mark.parametrize("repeat", range(3))
def test_cancellation_and_authorization_serialize_without_overspending_position_risk(
    service,
    initialize_campaign,
    make_artifacts,
    uow_factory,
    postgres_engine: Engine,
    repeat: int,
) -> None:
    campaign_id = f"campaign-cancel-authorize-{repeat}"
    initialize_campaign(campaign_id=campaign_id)
    artifacts = make_artifacts(
        f"cancel-authorize-open-{repeat}",
        campaign_id=campaign_id,
        maximum_loss="600",
    )
    artifacts, authorized, script, events = _prepare_partial_order(
        service,
        artifacts,
        client_order_id=f"client-cancel-authorize-open-{repeat}",
    )
    execution = DurablePartialFillCanceledExecutionService(uow_factory)
    execution.apply_execution_event(_command(artifacts, authorized, script, events[0]))
    execution.apply_execution_event(_command(artifacts, authorized, script, events[1]))

    contender = make_artifacts(
        f"cancel-authorize-contender-{repeat}",
        campaign_id=campaign_id,
        maximum_loss="700",
        authorization_at=artifacts.authorization_at + timedelta(seconds=20),
    )
    service.persist_authorization_inputs(contender.inputs)
    cancel_command = _command(artifacts, authorized, script, events[2])
    authorize_command = _authorization_command(
        contender,
        f"client-cancel-authorize-contender-{repeat}",
    )
    barrier = Barrier(2)

    def cancel() -> DurablePartialFillExecutionResult:
        barrier.wait(timeout=10)
        return execution.apply_execution_event(cancel_command)

    def authorize() -> AuthorizedOrderResult | PersistedRiskRejection:
        barrier.wait(timeout=10)
        return service.authorize_and_reserve(authorize_command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancel_future = executor.submit(cancel)
        authorize_future = executor.submit(authorize)
        canceled = cancel_future.result(timeout=15)
        authorization = authorize_future.result(timeout=15)

    assert canceled.state.order.state is OrderState.CANCELED
    assert canceled.state.reservation.state is ReservationState.RELEASED
    assert canceled.state.position is not None
    assert canceled.state.position.maximum_remaining_loss == Money("240")
    with uow_factory() as unit_of_work:
        order_risk = unit_of_work.reservations.active_total(campaign_id)
        position_risk = unit_of_work.positions.active_total(campaign_id)
        balances = unit_of_work.ledger.balances(campaign_id)
        assert position_risk == Money("240")
        assert order_risk.amount + position_risk.amount <= balances.ledger_equity.amount
        assert unit_of_work.ledger.journal_count(campaign_id) == 3
        if isinstance(authorization, AuthorizedOrderResult):
            assert authorization.capacity_before.active_order_reservations == Money(0)
            assert authorization.capacity_before.active_position_risk == Money("240")
            assert order_risk == Money("700")
        else:
            assert isinstance(authorization, PersistedRiskRejection)
            assert authorization.capacity_before.active_order_reservations == Money("360")
            assert authorization.capacity_before.active_position_risk == Money("240")
            assert "RISK_CAPACITY_EXCEEDED" in authorization.risk_decision.reason_codes
            assert order_risk == Money(0)
        assert balances.trading_pnl == Decimal("-2.4")
        unit_of_work.commit()
    assert _counts(postgres_engine, campaign_id, authorized.order.order_id) == (3, 1, 3)
