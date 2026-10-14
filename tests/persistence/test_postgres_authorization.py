from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apex import ENGINE_VERSION
from apex.adapters.fixtures import FixtureReader
from apex.adapters.simulator import DeterministicSimulator
from apex.application.authorize_order import (
    AuthorizationFaultPoint,
    AuthorizeAndReserveCommand,
    AuthorizedOrderResult,
    CampaignInitializationCommand,
    DurableAuthorizationService,
    IdempotencyConflictError,
    InjectedAuthorizationFault,
    PersistAuthorizationInputsCommand,
    PersistedRiskRejection,
)
from apex.application.interfaces import RunMetadata
from apex.application.run_simulation import GoldenSimulation
from apex.cli import _decision_inputs, _execution_script
from apex.domain.campaign import OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS
from apex.domain.decisions import StaleMarketRiskDecision
from apex.domain.execution import create_reserved_order
from apex.domain.values import ContentHash, Money

pytestmark = pytest.mark.postgres


def _authorize(artifacts, client_order_id: str, fault_at=None) -> AuthorizeAndReserveCommand:
    return AuthorizeAndReserveCommand(
        campaign_id=artifacts.inputs.campaign_id,
        proposal_id=artifacts.inputs.proposal.proposal_id,
        allocation_id=artifacts.inputs.allocation.allocation_id,
        client_order_id=client_order_id,
        authorization_at=artifacts.authorization_at,
        fault_at=fault_at,
    )


def test_campaign_initialization_is_exact_balanced_and_idempotent(
    service,
    policy,
    uow_factory,
) -> None:
    command = CampaignInitializationCommand(
        campaign_id="campaign-initialization",
        contribution=Money("1000.00000000"),
        authorized_loss=Money("600.12500000"),
        policy=policy,
        occurred_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
    )
    first = service.initialize_campaign(command)
    second = service.initialize_campaign(command)

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert first.campaign == second.campaign
    assert first.contribution_journal == second.contribution_journal
    assert first.contribution_journal.balanced
    assert first.contribution_journal.occurred_at == datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    with uow_factory() as unit_of_work:
        balances = unit_of_work.ledger.balances(command.campaign_id)
        assert balances.contributions.amount == Decimal("1000")
        assert balances.settled_cash.amount == Decimal("1000")
        assert balances.ledger_equity.amount == Decimal("1000")
        assert balances.trading_pnl == Decimal(0)
        assert unit_of_work.ledger.journal_count(command.campaign_id) == 1
        unit_of_work.commit()


def test_campaign_initialization_conflict_is_rejected(service, policy) -> None:
    original = CampaignInitializationCommand(
        campaign_id="campaign-conflict",
        contribution=Money("1000"),
        authorized_loss=Money("500"),
        policy=policy,
        occurred_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
    )
    service.initialize_campaign(original)
    with pytest.raises(IdempotencyConflictError):
        service.initialize_campaign(replace(original, authorized_loss=Money("501")))


def test_atomic_authorization_round_trips_exact_decimals_and_replays(
    service,
    initialize_campaign,
    make_artifacts,
    uow_factory,
) -> None:
    initialize_campaign()
    artifacts = make_artifacts("roundtrip", maximum_loss="100.125")
    service.persist_authorization_inputs(artifacts.inputs)
    command = _authorize(artifacts, "client-roundtrip")

    first = service.authorize_and_reserve(command)
    replay = service.authorize_and_reserve(command)

    assert isinstance(first, AuthorizedOrderResult)
    assert isinstance(replay, AuthorizedOrderResult)
    assert first.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert replay.order == first.order
    assert replay.reservation == first.reservation
    assert first.order.client_order_id == "client-roundtrip"
    assert first.order.quantity.value == Decimal("100.125")
    assert first.reservation.maximum_loss.amount == Decimal("100.125")
    assert first.capacity_before.available_risk_capacity.amount == Decimal("1000")
    assert first.capacity_before.active_position_risk.amount == Decimal(0)
    with uow_factory() as unit_of_work:
        assert unit_of_work.risk_decisions.count("campaign-persistence") == 1
        assert unit_of_work.reservations.count("campaign-persistence") == 1
        assert unit_of_work.orders.count("campaign-persistence") == 1
        assert unit_of_work.reservations.active_total("campaign-persistence").amount == Decimal(
            "100.125"
        )
        unit_of_work.commit()


def test_client_order_reuse_with_different_request_conflicts(
    service,
    initialize_campaign,
    make_artifacts,
) -> None:
    initialize_campaign()
    artifacts = make_artifacts("idempotency-conflict")
    service.persist_authorization_inputs(artifacts.inputs)
    command = _authorize(artifacts, "client-conflict")
    service.authorize_and_reserve(command)

    with pytest.raises(IdempotencyConflictError):
        service.authorize_and_reserve(
            replace(command, authorization_at=command.authorization_at + timedelta(seconds=1))
        )


@pytest.mark.parametrize("fault_at", tuple(AuthorizationFaultPoint))
def test_each_fault_point_rolls_back_the_complete_accepted_bundle(
    service: DurableAuthorizationService,
    initialize_campaign,
    make_artifacts,
    uow_factory,
    fault_at: AuthorizationFaultPoint,
) -> None:
    initialize_campaign()
    artifacts = make_artifacts(f"fault-{fault_at.value.lower()}")
    service.persist_authorization_inputs(artifacts.inputs)
    command = _authorize(artifacts, f"client-{fault_at.value.lower()}", fault_at)

    with pytest.raises(InjectedAuthorizationFault):
        service.authorize_and_reserve(command)
    with uow_factory() as unit_of_work:
        assert unit_of_work.risk_decisions.count("campaign-persistence") == 0
        assert unit_of_work.reservations.count("campaign-persistence") == 0
        assert unit_of_work.orders.count("campaign-persistence") == 0
        unit_of_work.commit()

    retry = service.authorize_and_reserve(replace(command, fault_at=None))
    assert isinstance(retry, AuthorizedOrderResult)


def test_rejected_risk_is_persisted_without_reservation_or_order(
    service,
    initialize_campaign,
    make_artifacts,
    uow_factory,
) -> None:
    initialize_campaign()
    first = make_artifacts("capacity-first", maximum_loss="600")
    second = make_artifacts(
        "capacity-second",
        maximum_loss="600",
        authorization_at=first.authorization_at + timedelta(seconds=1),
    )
    service.persist_authorization_inputs(first.inputs)
    service.persist_authorization_inputs(second.inputs)
    accepted = service.authorize_and_reserve(_authorize(first, "client-capacity-first"))
    rejected = service.authorize_and_reserve(_authorize(second, "client-capacity-second"))

    assert isinstance(accepted, AuthorizedOrderResult)
    assert isinstance(rejected, PersistedRiskRejection)
    assert "RISK_CAPACITY_EXCEEDED" in rejected.risk_decision.reason_codes
    assert rejected.capacity_before.active_order_reservations.amount == Decimal("600")
    assert rejected.capacity_before.available_risk_capacity.amount == Decimal("400")
    with uow_factory() as unit_of_work:
        assert unit_of_work.risk_decisions.count("campaign-persistence") == 2
        assert unit_of_work.reservations.count("campaign-persistence") == 1
        assert unit_of_work.orders.count("campaign-persistence") == 1
        assert unit_of_work.ledger.balances("campaign-persistence").settled_cash.amount == Decimal(
            "1000"
        )
        unit_of_work.commit()


def test_rejection_fault_rolls_back_and_clean_retry_persists_once(
    service,
    initialize_campaign,
    make_artifacts,
    uow_factory,
) -> None:
    initialize_campaign()
    accepted_artifacts = make_artifacts("rejection-base", maximum_loss="600")
    rejected_artifacts = make_artifacts(
        "rejection-fault",
        maximum_loss="600",
        authorization_at=accepted_artifacts.authorization_at + timedelta(seconds=1),
    )
    service.persist_authorization_inputs(accepted_artifacts.inputs)
    service.persist_authorization_inputs(rejected_artifacts.inputs)
    service.authorize_and_reserve(_authorize(accepted_artifacts, "client-rejection-base"))
    faulted = _authorize(
        rejected_artifacts,
        "client-rejection-fault",
        AuthorizationFaultPoint.AFTER_RISK_DECISION,
    )
    with pytest.raises(InjectedAuthorizationFault):
        service.authorize_and_reserve(faulted)
    with uow_factory() as unit_of_work:
        assert unit_of_work.risk_decisions.count("campaign-persistence") == 1
        unit_of_work.commit()
    retry = service.authorize_and_reserve(replace(faulted, fault_at=None))
    assert isinstance(retry, PersistedRiskRejection)
    with uow_factory() as unit_of_work:
        assert unit_of_work.risk_decisions.count("campaign-persistence") == 2
        unit_of_work.commit()


def test_stale_market_rejection_replays_with_complete_typed_evidence(
    service,
    initialize_campaign,
    make_artifacts,
) -> None:
    initialize_campaign(campaign_id="campaign-stale-replay")
    artifacts = make_artifacts(
        "stale-replay",
        campaign_id="campaign-stale-replay",
        market_age_seconds=OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS + 1,
    )
    service.persist_authorization_inputs(artifacts.inputs)
    command = _authorize(artifacts, "client-stale-replay")
    first = service.authorize_and_reserve(command)
    replay = service.authorize_and_reserve(command)

    assert isinstance(first, PersistedRiskRejection)
    assert isinstance(first.risk_decision, StaleMarketRiskDecision)
    assert isinstance(replay, PersistedRiskRejection)
    assert isinstance(replay.risk_decision, StaleMarketRiskDecision)
    assert replay.risk_decision == first.risk_decision
    assert replay.idempotent_replay is True


@settings(
    max_examples=8,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@given(units=st.integers(min_value=1, max_value=10))
def test_authoritative_decimal_round_trip_property(
    service,
    initialize_campaign,
    make_artifacts,
    units: int,
) -> None:
    initialize_campaign(campaign_id="campaign-decimal-property")
    amount = Decimal(units) / Decimal("100000000")
    rendered = format(amount, "f")
    artifacts = make_artifacts(
        f"decimal-property-{units}",
        campaign_id="campaign-decimal-property",
        maximum_loss=rendered,
    )
    service.persist_authorization_inputs(artifacts.inputs)
    result = service.authorize_and_reserve(
        _authorize(artifacts, f"client-decimal-property-{units}")
    )
    assert isinstance(result, AuthorizedOrderResult)
    assert result.order.quantity.value == amount
    assert result.reservation.maximum_loss.amount == amount


def test_golden_in_memory_and_postgres_authorization_are_domain_identical(service) -> None:
    reader = FixtureReader()
    fixture = reader.load_decision("golden")
    decision_inputs = _decision_inputs(fixture)
    simulation = GoldenSimulation(
        decision_inputs=decision_inputs,
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("golden")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        capital=Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )
    checkpoint = simulation.run_decision_and_execution()
    in_memory = checkpoint.decision
    proposal = in_memory.strategy_evaluation.proposal
    assert proposal is not None
    expected_reservation, expected_order = create_reserved_order(
        campaign_id=in_memory.campaign.campaign_id,
        proposal=proposal,
        allocation=in_memory.allocation,
        risk_decision=in_memory.risk_decision,
    )

    service.initialize_campaign(
        CampaignInitializationCommand(
            campaign_id=in_memory.campaign.campaign_id,
            contribution=in_memory.campaign.contribution,
            authorized_loss=in_memory.campaign.authorized_loss,
            policy=in_memory.campaign.policy,
            occurred_at=fixture.logical_time,
        )
    )
    service.persist_authorization_inputs(
        PersistAuthorizationInputsCommand(
            campaign_id=in_memory.campaign.campaign_id,
            proposal=proposal,
            allocation=in_memory.allocation,
            information_set=in_memory.information_set,
            market_observation_source_id=decision_inputs.market_observation_source_id,
            recorded_at=fixture.logical_time,
        )
    )
    persisted = service.authorize_and_reserve(
        AuthorizeAndReserveCommand(
            campaign_id=in_memory.campaign.campaign_id,
            proposal_id=proposal.proposal_id,
            allocation_id=in_memory.allocation.allocation_id,
            client_order_id=expected_order.client_order_id,
            authorization_at=decision_inputs.risk_authorization_at,
        )
    )
    assert isinstance(persisted, AuthorizedOrderResult)
    assert persisted.risk_decision == in_memory.risk_decision
    assert persisted.reservation == expected_reservation
    assert persisted.order == expected_order
