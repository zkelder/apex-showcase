from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

import pytest

from apex.application.authorize_order import (
    AuthorizeAndReserveCommand,
    AuthorizedOrderResult,
    PersistedRiskRejection,
)

pytestmark = pytest.mark.postgres


def _command(artifacts, client_order_id: str) -> AuthorizeAndReserveCommand:
    return AuthorizeAndReserveCommand(
        campaign_id=artifacts.inputs.campaign_id,
        proposal_id=artifacts.inputs.proposal.proposal_id,
        allocation_id=artifacts.inputs.allocation.allocation_id,
        client_order_id=client_order_id,
        authorization_at=artifacts.authorization_at,
    )


@pytest.mark.parametrize("repeat", range(3))
def test_concurrent_distinct_requests_cannot_over_reserve_capacity(
    service,
    initialize_campaign,
    make_artifacts,
    uow_factory,
    repeat: int,
) -> None:
    initialize_campaign(campaign_id=f"campaign-concurrent-{repeat}")
    campaign_id = f"campaign-concurrent-{repeat}"
    first = make_artifacts(
        f"concurrent-{repeat}-a",
        campaign_id=campaign_id,
        maximum_loss="600",
    )
    second = make_artifacts(
        f"concurrent-{repeat}-b",
        campaign_id=campaign_id,
        maximum_loss="600",
        authorization_at=first.authorization_at + timedelta(seconds=1),
    )
    service.persist_authorization_inputs(first.inputs)
    service.persist_authorization_inputs(second.inputs)
    barrier = Barrier(2)

    def authorize(command: AuthorizeAndReserveCommand):
        barrier.wait(timeout=10)
        return service.authorize_and_reserve(command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(authorize, _command(first, f"client-concurrent-{repeat}-a")),
            executor.submit(authorize, _command(second, f"client-concurrent-{repeat}-b")),
        )
        results = tuple(future.result(timeout=15) for future in futures)

    assert sum(isinstance(result, AuthorizedOrderResult) for result in results) == 1
    assert sum(isinstance(result, PersistedRiskRejection) for result in results) == 1
    rejection = next(result for result in results if isinstance(result, PersistedRiskRejection))
    assert "RISK_CAPACITY_EXCEEDED" in rejection.risk_decision.reason_codes
    with uow_factory() as unit_of_work:
        assert unit_of_work.reservations.count(campaign_id) == 1
        assert unit_of_work.orders.count(campaign_id) == 1
        assert unit_of_work.reservations.active_total(campaign_id).amount == Decimal("600")
        balances = unit_of_work.ledger.balances(campaign_id)
        assert balances.settled_cash.amount == Decimal("1000")
        assert balances.trading_pnl == Decimal(0)
        unit_of_work.commit()


def test_concurrent_same_request_returns_one_idempotent_bundle(
    service,
    initialize_campaign,
    make_artifacts,
    uow_factory,
) -> None:
    initialize_campaign(campaign_id="campaign-same-request")
    artifacts = make_artifacts(
        "same-request",
        campaign_id="campaign-same-request",
        maximum_loss="250",
    )
    service.persist_authorization_inputs(artifacts.inputs)
    command = _command(artifacts, "client-same-request")
    barrier = Barrier(2)

    def authorize():
        barrier.wait(timeout=10)
        return service.authorize_and_reserve(command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(authorize), executor.submit(authorize))
        results = tuple(future.result(timeout=15) for future in futures)

    assert all(isinstance(result, AuthorizedOrderResult) for result in results)
    assert {result.idempotent_replay for result in results} == {False, True}
    assert results[0].order == results[1].order
    assert results[0].reservation == results[1].reservation
    with uow_factory() as unit_of_work:
        assert unit_of_work.risk_decisions.count("campaign-same-request") == 1
        assert unit_of_work.reservations.count("campaign-same-request") == 1
        assert unit_of_work.orders.count("campaign-same-request") == 1
        unit_of_work.commit()
