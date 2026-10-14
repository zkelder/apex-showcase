from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from threading import Barrier

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
    position_settlement,
    postgres_uow_factory,
    settlement_fact,
)
from apex.adapters.postgres import (
    position as durable_position,
)
from apex.adapters.simulator import DeterministicSimulator
from apex.application.authorize_order import (
    AuthorizeAndReserveCommand,
    AuthorizedOrderResult,
    CampaignInitializationCommand,
    DurableAuthorizationService,
    IdempotencyConflictError,
    PersistAuthorizationInputsCommand,
)
from apex.application.execute_partial_fill_canceled_order import (
    ApplyPartialFillExecutionEventCommand,
    DurablePartialFillCanceledExecutionService,
    InjectedPartialFillExecutionFault,
    PartialFillExecutionFaultPoint,
)
from apex.application.interfaces import (
    PartialFillCanceledExecutionScript,
    PartialFillCanceledResult,
    RunMetadata,
)
from apex.application.run_simulation import GoldenSimulation, PartialExecutionCheckpoint
from apex.application.settle_partial_fill_canceled_no import (
    DurablePartialFillCanceledSettlementService,
    InjectedSettlementFault,
    SettlementFaultPoint,
    SettlePartialFillCanceledCommand,
)
from apex.cli import _decision_inputs, _execution_script, _settlement_inputs
from apex.domain.execution import (
    ClassifiedRuleBoundSettlementFact,
    ClassifiedSettledFilledPositionState,
    ExecutionEvent,
    OrderState,
    ReservationState,
    RuleBoundSettlementFact,
    SettlementState,
    create_reserved_order,
)
from apex.domain.ledger import AccountCode, JournalFactory, JournalType, PostingSide
from apex.domain.market import SettlementOutcome, VoidContractRuleVersion
from apex.domain.values import (
    ContentHash,
    Money,
    ValueValidationError,
    canonical_decimal,
    content_digest,
)

pytestmark = pytest.mark.postgres


@dataclass(frozen=True, slots=True)
class ExactPartialCase:
    oracle: PartialFillCanceledResult
    script: PartialFillCanceledExecutionScript
    initial_order: object
    initial_reservation: object


@dataclass(frozen=True, slots=True)
class ExactVoidPartialCase:
    checkpoint: PartialExecutionCheckpoint
    script: PartialFillCanceledExecutionScript
    initial_order: object
    initial_reservation: object

    @property
    def campaign_id(self) -> str:
        return self.checkpoint.decision.campaign.campaign_id

    @property
    def order(self):
        return self.checkpoint.decision.order

    @property
    def position(self):
        if self.checkpoint.position is None:
            raise AssertionError("VOID checkpoint is missing its filled position")
        return self.checkpoint.position

    @property
    def events(self) -> tuple[ExecutionEvent, ...]:
        return self.checkpoint.events

    @property
    def information_set(self):
        return self.checkpoint.decision.information_set


def _prepare_exact_partial(
    service: DurableAuthorizationService,
) -> ExactPartialCase:
    reader = FixtureReader()
    fixture = reader.load_decision("partial-fill-canceled")
    decision_inputs = _decision_inputs(fixture)
    script = _execution_script(fixture)
    assert isinstance(script, PartialFillCanceledExecutionScript)
    simulation = GoldenSimulation(
        decision_inputs=decision_inputs,
        execution_script=script,
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("partial-fill-canceled")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        capital=Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )
    oracle = simulation.run_decision_and_execution()
    assert isinstance(oracle, PartialFillCanceledResult)
    initial_reservation, initial_order = create_reserved_order(
        campaign_id=oracle.campaign.campaign_id,
        proposal=oracle.proposal,
        allocation=oracle.allocation,
        risk_decision=oracle.risk_decision,
    )
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
    return ExactPartialCase(oracle, script, initial_order, initial_reservation)


def _prepare_exact_void_partial(service: DurableAuthorizationService) -> ExactVoidPartialCase:
    fixture_id = "partial-fill-canceled-settled-void"
    reader = FixtureReader()
    fixture = reader.load_decision(fixture_id)
    decision_inputs = _decision_inputs(fixture)
    script = _execution_script(fixture)
    assert isinstance(script, PartialFillCanceledExecutionScript)
    simulation = GoldenSimulation(
        decision_inputs=decision_inputs,
        execution_script=script,
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash(fixture_id)),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        capital=Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )
    checkpoint = simulation.run_decision_and_execution()
    assert isinstance(checkpoint, PartialExecutionCheckpoint)
    assert checkpoint.position is not None
    proposal = checkpoint.decision.strategy_evaluation.proposal
    assert proposal is not None
    initial_reservation, initial_order = create_reserved_order(
        campaign_id=checkpoint.decision.campaign.campaign_id,
        proposal=proposal,
        allocation=checkpoint.decision.allocation,
        risk_decision=checkpoint.decision.risk_decision,
    )
    service.initialize_campaign(
        CampaignInitializationCommand(
            campaign_id=checkpoint.decision.campaign.campaign_id,
            contribution=checkpoint.decision.campaign.contribution,
            authorized_loss=checkpoint.decision.campaign.authorized_loss,
            policy=checkpoint.decision.campaign.policy,
            occurred_at=fixture.logical_time,
        )
    )
    service.persist_authorization_inputs(
        PersistAuthorizationInputsCommand(
            campaign_id=checkpoint.decision.campaign.campaign_id,
            proposal=proposal,
            allocation=checkpoint.decision.allocation,
            information_set=checkpoint.decision.information_set,
            market_observation_source_id=decision_inputs.market_observation_source_id,
            recorded_at=fixture.logical_time,
        )
    )
    authorized = service.authorize_and_reserve(
        AuthorizeAndReserveCommand(
            campaign_id=checkpoint.decision.campaign.campaign_id,
            proposal_id=proposal.proposal_id,
            allocation_id=checkpoint.decision.allocation.allocation_id,
            client_order_id=initial_order.client_order_id,
            authorization_at=decision_inputs.risk_authorization_at,
        )
    )
    assert isinstance(authorized, AuthorizedOrderResult)
    assert authorized.risk_decision == checkpoint.decision.risk_decision
    assert authorized.reservation == initial_reservation
    assert authorized.order == initial_order
    return ExactVoidPartialCase(checkpoint, script, initial_order, initial_reservation)


def _execution_service(engine: Engine) -> DurablePartialFillCanceledExecutionService:
    return DurablePartialFillCanceledExecutionService(postgres_uow_factory(engine))


def _command(
    case: ExactPartialCase,
    event: ExecutionEvent,
    fault_at: PartialFillExecutionFaultPoint | None = None,
) -> ApplyPartialFillExecutionEventCommand:
    return ApplyPartialFillExecutionEventCommand(
        campaign_id=case.oracle.campaign.campaign_id,
        order_id=case.oracle.order.order_id,
        script=case.script,
        event=event,
        recorded_at=event.occurred_at + timedelta(seconds=1),
        fault_at=fault_at,
    )


def _snapshot(engine: Engine, order_id: str) -> tuple[object, ...]:
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
            ).mappings()
        )
        position_rows = tuple(
            dict(row)
            for row in connection.execute(
                sa.select(durable_position).where(
                    durable_position.c.originating_order_id == order_id
                )
            ).mappings()
        )
        journal_rows = tuple(
            dict(row)
            for row in connection.execute(
                sa.select(ledger_journal)
                .join(ledger_posting)
                .where(ledger_journal.c.campaign_id == order_row["campaign_id"])
                .group_by(*ledger_journal.c)
                .order_by(ledger_journal.c.journal_id)
            ).mappings()
        )
        posting_rows = tuple(
            dict(row)
            for row in connection.execute(
                sa.select(ledger_posting)
                .join(ledger_journal)
                .where(ledger_journal.c.campaign_id == order_row["campaign_id"])
                .order_by(ledger_posting.c.journal_id, ledger_posting.c.sequence)
            ).mappings()
        )
    return order_row, reservation_row, event_rows, position_rows, journal_rows, posting_rows


def test_exact_partial_fill_fixture_persists_filled_only_financial_and_risk_state(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
    uow_factory,
) -> None:
    case = _prepare_exact_partial(service)
    execution = _execution_service(postgres_engine)
    opened_event, fill_event, canceled_event = case.oracle.execution_events
    campaign_id = case.oracle.campaign.campaign_id

    opened = execution.apply_execution_event(_command(case, opened_event))
    assert opened.state.order.state is OrderState.OPEN
    assert opened.state.reservation.current_amount == Money("100")
    assert opened.state.position is None
    assert len(_snapshot(postgres_engine, case.oracle.order.order_id)[4]) == 1

    filled = execution.apply_execution_event(_command(case, fill_event))
    assert filled.state.order.state is OrderState.PARTIALLY_FILLED
    assert filled.state.order.quantity.value == Decimal("250")
    assert filled.state.order.filled_quantity.value == Decimal("100")
    assert filled.state.order.remaining_quantity.value == Decimal("150")
    assert filled.state.order.reserved_maximum_loss == Money("60")
    assert filled.state.reservation.maximum_loss == Money("100")
    assert filled.state.reservation.current_amount == Money("60")
    assert filled.state.reservation.state is ReservationState.OPEN
    assert filled.state.position == case.oracle.position
    assert filled.state.position.maximum_remaining_loss == Money("40")
    after_fill = _snapshot(postgres_engine, case.oracle.order.order_id)
    assert len(after_fill[2]) == 2
    assert len(after_fill[3]) == 1
    assert len(after_fill[4]) == 3
    assert len(after_fill[5]) == 6
    fill_row = after_fill[2][1]
    assert fill_row["fill_quantity"] == Decimal("100")
    assert fill_row["fill_price"] == Decimal("0.39")
    assert fill_row["execution_fee"] == Decimal("1")

    canceled = execution.apply_execution_event(_command(case, canceled_event))
    assert canceled.released_maximum_loss == Money("60")
    assert canceled.state.order == case.oracle.order
    assert canceled.state.order.state is OrderState.CANCELED
    assert canceled.state.order.last_execution_sequence == 3
    assert canceled.state.order_row_version == 4
    assert canceled.state.reservation.maximum_loss == Money("100")
    assert canceled.state.reservation.current_amount == Money(0)
    assert canceled.state.reservation.state is ReservationState.RELEASED
    assert canceled.state.reservation.released_at == canceled_event.occurred_at
    assert canceled.state.position == case.oracle.position
    assert canceled.state.execution_events == case.oracle.execution_events
    terminal = _snapshot(postgres_engine, case.oracle.order.order_id)
    assert terminal[3] == after_fill[3]
    assert terminal[4] == after_fill[4]
    assert terminal[5] == after_fill[5]
    assert len(terminal[2]) == 3
    assert all(
        row["fill_quantity"] is None and row["fill_price"] is None and row["execution_fee"] is None
        for row in (terminal[2][0], terminal[2][2])
    )
    expected_purchase = JournalFactory.purchase(
        fill_event.event_id,
        Money("39"),
        fill_event.occurred_at,
    )
    expected_fee = JournalFactory.fee(fill_event.event_id, Money("1"), fill_event.occurred_at)
    with uow_factory() as unit_of_work:
        balances = unit_of_work.ledger.balances(campaign_id)
        assert balances.contributions == Money("1000")
        assert balances.settled_cash == Money("960")
        assert balances.ledger_equity == Money("999")
        assert balances.trading_pnl == Decimal("-1")
        assert unit_of_work.ledger.journal_count(campaign_id) == 3
        assert unit_of_work.reservations.active_total(campaign_id) == Money(0)
        assert unit_of_work.positions.active_total(campaign_id) == Money("40")
        unit_of_work.commit()
    journal_ids = {row["journal_id"] for row in terminal[4]}
    assert {expected_purchase.journal_id, expected_fee.journal_id} <= journal_ids
    assert not any(row["journal_type"] == "SETTLEMENT" for row in terminal[4])
    assert case.oracle.final_active_campaign_risk == Money("40")
    assert case.oracle.metrics.realized_trading_pnl_before_fees == Decimal(0)


def _settlement_service(engine: Engine) -> DurablePartialFillCanceledSettlementService:
    return DurablePartialFillCanceledSettlementService(postgres_uow_factory(engine))


def _no_settlement_fact() -> RuleBoundSettlementFact:
    value = _settlement_inputs(
        FixtureReader().load_settlement("partial-fill-canceled-settled-no")
    ).fact
    assert isinstance(value, RuleBoundSettlementFact)
    return value


def _no_settlement_command(
    case: ExactPartialCase,
    fault_at: SettlementFaultPoint | None = None,
) -> SettlePartialFillCanceledCommand:
    fact = _no_settlement_fact()
    return SettlePartialFillCanceledCommand(
        campaign_id=case.oracle.campaign.campaign_id,
        order_id=case.oracle.order.order_id,
        fact=fact,
        recorded_at=fact.available_at + timedelta(seconds=1),
        fault_at=fault_at,
    )


def _cancel_exact_partial(
    case: ExactPartialCase,
    engine: Engine,
) -> None:
    execution = _execution_service(engine)
    for event in case.oracle.execution_events:
        execution.apply_execution_event(_command(case, event))


def _void_execution_command(
    case: ExactVoidPartialCase,
    event: ExecutionEvent,
) -> ApplyPartialFillExecutionEventCommand:
    return ApplyPartialFillExecutionEventCommand(
        campaign_id=case.campaign_id,
        order_id=case.order.order_id,
        script=case.script,
        event=event,
        recorded_at=event.occurred_at + timedelta(seconds=1),
    )


def _cancel_exact_void(case: ExactVoidPartialCase, engine: Engine) -> None:
    execution = _execution_service(engine)
    for event in case.events:
        execution.apply_execution_event(_void_execution_command(case, event))


def test_exact_partial_fill_canceled_no_settlement_persists_only_executed_position(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
    uow_factory,
) -> None:
    case = _prepare_exact_partial(service)
    _cancel_exact_partial(case, postgres_engine)
    before = _snapshot(postgres_engine, case.oracle.order.order_id)

    settled = _settlement_service(postgres_engine).settle(_no_settlement_command(case))

    assert not settled.idempotent_replay
    assert settled.state.order == case.oracle.order
    assert settled.state.reservation.state is ReservationState.RELEASED
    assert settled.state.reservation.current_amount == Money(0)
    assert settled.state.position.settlement_state is SettlementState.SETTLED
    assert settled.state.position.open_quantity.value == 0
    assert settled.state.position.maximum_remaining_loss == Money(0)
    assert settled.settlement_journal.journal_type is JournalType.SETTLEMENT
    assert len(settled.settlement_journal.postings) == 2

    after = _snapshot(postgres_engine, case.oracle.order.order_id)
    assert after[0] == before[0]
    assert after[1] == before[1]
    assert after[2] == before[2]
    assert len(after[3]) == 1
    assert len(after[4]) == len(before[4]) + 1
    assert len(after[5]) == len(before[5]) + 2
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(settlement_fact)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(position_settlement)) == 1
    with uow_factory() as unit_of_work:
        balances = unit_of_work.ledger.balances(case.oracle.campaign.campaign_id)
        assert balances.settled_cash == Money("960")
        assert balances.ledger_equity == Money("960")
        assert balances.trading_pnl == Decimal("-40")
        assert unit_of_work.positions.active_total(case.oracle.campaign.campaign_id) == Money(0)
        assert unit_of_work.ledger.journal_count(case.oracle.campaign.campaign_id) == 4
        unit_of_work.commit()

    fresh_engine = create_postgres_engine(PostgresSettings.from_env().for_test())
    try:
        replay = _settlement_service(fresh_engine).settle(_no_settlement_command(case))
    finally:
        fresh_engine.dispose()
    assert replay.idempotent_replay
    assert replay.state == settled.state


def test_concurrent_identical_no_settlement_creates_one_durable_settlement(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
) -> None:
    case = _prepare_exact_partial(service)
    _cancel_exact_partial(case, postgres_engine)
    command = _no_settlement_command(case)
    barrier = Barrier(2)

    def settle():
        barrier.wait(timeout=10)
        return _settlement_service(postgres_engine).settle(command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(settle), executor.submit(settle))
        results = tuple(future.result(timeout=15) for future in futures)

    assert {result.idempotent_replay for result in results} == {False, True}
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(settlement_fact)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(position_settlement)) == 1


@pytest.mark.parametrize("fault_at", list(SettlementFaultPoint))
def test_no_settlement_faults_roll_back_without_mutating_canceled_order(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
    fault_at: SettlementFaultPoint,
) -> None:
    case = _prepare_exact_partial(service)
    _cancel_exact_partial(case, postgres_engine)
    before = _snapshot(postgres_engine, case.oracle.order.order_id)

    with pytest.raises(InjectedSettlementFault):
        _settlement_service(postgres_engine).settle(_no_settlement_command(case, fault_at))

    assert _snapshot(postgres_engine, case.oracle.order.order_id) == before
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(settlement_fact)) == 0
        assert connection.scalar(sa.select(sa.func.count()).select_from(position_settlement)) == 0


def _void_settlement_fact() -> ClassifiedRuleBoundSettlementFact:
    value = _settlement_inputs(
        FixtureReader().load_settlement("partial-fill-canceled-settled-void")
    ).fact
    assert isinstance(value, ClassifiedRuleBoundSettlementFact)
    return value


def _void_settlement_command(
    case: ExactVoidPartialCase,
    fault_at: SettlementFaultPoint | None = None,
) -> SettlePartialFillCanceledCommand:
    fact = _void_settlement_fact()
    rule = case.information_set.rule
    assert isinstance(rule, VoidContractRuleVersion)
    return SettlePartialFillCanceledCommand(
        campaign_id=case.campaign_id,
        order_id=case.order.order_id,
        fact=fact,
        recorded_at=fact.available_at + timedelta(seconds=1),
        void_term_content_hash=rule.void_settlement_term.content_hash,
        fault_at=fault_at,
    )


def _rehashed_void_fact(
    baseline: ClassifiedRuleBoundSettlementFact,
    *,
    outcome_classification: SettlementOutcome | None = None,
    settlement_value: Decimal | None = None,
    source_id: str | None = None,
    rule_content_hash: ContentHash | None = None,
    resolution_source: str | None = None,
    fixture_id: str | None = None,
) -> ClassifiedRuleBoundSettlementFact:
    classification = outcome_classification or baseline.outcome_classification
    resolved_value = settlement_value if settlement_value is not None else baseline.settlement_value
    settlement_value_text = canonical_decimal(resolved_value)
    resolved_source_id = source_id or baseline.source_id
    resolved_rule_content_hash = rule_content_hash or baseline.rule_content_hash
    resolved_resolution_source = resolution_source or baseline.resolution_source
    resolved_fixture_id = fixture_id or baseline.fixture_id
    raw_payload = (
        f"resolution:{classification.value};"
        f"settlement_value={settlement_value_text};"
        f"instrument={baseline.instrument_id};"
        f"rule={baseline.rule_version_id};"
        f"rule_hash={resolved_rule_content_hash.value};"
        f"source={resolved_resolution_source}"
    )
    raw_payload_hash = ContentHash(sha256(raw_payload.encode("utf-8")).hexdigest())
    evidence = {
        "fixture_id": resolved_fixture_id,
        "schema_version": baseline.schema_version,
        "source_id": resolved_source_id,
        "instrument_id": baseline.instrument_id,
        "rule_version_id": baseline.rule_version_id,
        "rule_content_hash": resolved_rule_content_hash.value,
        "resolution_source": resolved_resolution_source,
        "outcome_classification": classification,
        "source_event_at": baseline.source_event_at,
        "available_at": baseline.available_at,
        "settlement_value": settlement_value_text,
        "raw_payload": raw_payload,
        "raw_payload_hash": raw_payload_hash.value,
    }
    return ClassifiedRuleBoundSettlementFact(
        source_id=resolved_source_id,
        instrument_id=baseline.instrument_id,
        rule_version_id=baseline.rule_version_id,
        source_event_at=baseline.source_event_at,
        available_at=baseline.available_at,
        settlement_value=resolved_value,
        content_hash=ContentHash(content_digest(evidence)),
        fixture_id=resolved_fixture_id,
        schema_version=baseline.schema_version,
        settlement_value_text=settlement_value_text,
        resolution_source=resolved_resolution_source,
        rule_content_hash=resolved_rule_content_hash,
        outcome_classification=classification,
        raw_payload=raw_payload,
        raw_payload_hash=raw_payload_hash,
    )


def test_exact_partial_fill_canceled_void_settlement_matches_the_in_memory_oracle(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
    uow_factory,
) -> None:
    case = _prepare_exact_void_partial(service)
    _cancel_exact_void(case, postgres_engine)
    before = _snapshot(postgres_engine, case.order.order_id)
    command = _void_settlement_command(case)

    settled = _settlement_service(postgres_engine).settle(command)
    replay = _settlement_service(postgres_engine).settle(command)

    assert not settled.idempotent_replay
    assert replay.idempotent_replay
    assert replay.state == settled.state
    assert settled.state.order == case.order
    assert settled.state.reservation.state is ReservationState.RELEASED
    assert settled.state.reservation.current_amount == Money(0)
    assert isinstance(settled.state.position, ClassifiedSettledFilledPositionState)
    assert settled.state.position.outcome_classification is SettlementOutcome.VOID
    assert settled.state.position.settled_quantity.value == Decimal("100")
    assert settled.state.position.settlement_value == Decimal("0.40")
    assert settled.state.position.settlement_cash == Money("40")
    assert settled.state.position.realized_contract_pnl == Decimal("1")
    assert settled.state.position.open_quantity.value == 0
    assert settled.state.position.maximum_remaining_loss == Money(0)
    assert settled.settlement_journal.journal_type is JournalType.SETTLEMENT
    assert tuple(
        (posting.account_code, posting.side, posting.amount)
        for posting in settled.settlement_journal.postings
    ) == (
        (AccountCode.CASH, PostingSide.DEBIT, Money("40")),
        (AccountCode.POSITION_COST, PostingSide.CREDIT, Money("39")),
        (AccountCode.REALIZED_PNL, PostingSide.CREDIT, Money("1")),
    )

    after = _snapshot(postgres_engine, case.order.order_id)
    assert after[0] == before[0]
    assert after[1] == before[1]
    assert after[2] == before[2]
    assert len(after[3]) == 1
    assert len(after[4]) == len(before[4]) + 1
    assert len(after[5]) == len(before[5]) + 3
    with postgres_engine.connect() as connection:
        fact_row = connection.execute(sa.select(settlement_fact)).mappings().one()
        settlement_row = connection.execute(sa.select(position_settlement)).mappings().one()
    rule = case.information_set.rule
    assert isinstance(rule, VoidContractRuleVersion)
    assert fact_row["classification"] == SettlementOutcome.VOID.value
    assert fact_row["settlement_value"] == Decimal("0.40")
    assert fact_row["void_term_content_hash"] == rule.void_settlement_term.content_hash.value
    assert settlement_row["settled_quantity"] == Decimal("100")
    assert settlement_row["settlement_cash"] == Decimal("40")
    assert settlement_row["realized_contract_pnl"] == Decimal("1")
    with uow_factory() as unit_of_work:
        balances = unit_of_work.ledger.balances(case.campaign_id)
        assert balances.contributions == Money("1000")
        assert balances.settled_cash == Money("1000")
        assert balances.ledger_equity == Money("1000")
        assert balances.trading_pnl == Decimal(0)
        assert unit_of_work.ledger.journal_count(case.campaign_id) == 4
        assert unit_of_work.reservations.active_total(case.campaign_id) == Money(0)
        assert unit_of_work.positions.active_total(case.campaign_id) == Money(0)
        unit_of_work.commit()

    fresh_engine = create_postgres_engine(PostgresSettings.from_env().for_test())
    try:
        recovered = _settlement_service(fresh_engine).settle(command)
    finally:
        fresh_engine.dispose()
    assert recovered.idempotent_replay
    assert recovered.state == settled.state


def test_concurrent_identical_void_settlement_creates_one_durable_result(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
) -> None:
    case = _prepare_exact_void_partial(service)
    _cancel_exact_void(case, postgres_engine)
    command = _void_settlement_command(case)
    barrier = Barrier(2)

    def settle():
        barrier.wait(timeout=10)
        return _settlement_service(postgres_engine).settle(command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(settle), executor.submit(settle))
        results = tuple(future.result(timeout=15) for future in futures)

    assert {result.idempotent_replay for result in results} == {False, True}
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(settlement_fact)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(position_settlement)) == 1


def test_void_settlement_and_authorization_serialize_without_overcommitting(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
    uow_factory,
    make_artifacts,
) -> None:
    case = _prepare_exact_void_partial(service)
    _cancel_exact_void(case, postgres_engine)
    contender = make_artifacts(
        "void-settlement-contender",
        campaign_id=case.campaign_id,
        maximum_loss="1000",
        authorization_at=case.events[-1].occurred_at + timedelta(seconds=20),
    )
    service.persist_authorization_inputs(contender.inputs)
    authorization_command = AuthorizeAndReserveCommand(
        campaign_id=case.campaign_id,
        proposal_id=contender.inputs.proposal.proposal_id,
        allocation_id=contender.inputs.allocation.allocation_id,
        client_order_id="client-void-settlement-contender",
        authorization_at=contender.authorization_at,
    )
    settlement_command = _void_settlement_command(case)
    barrier = Barrier(2)

    def settle():
        barrier.wait(timeout=10)
        return _settlement_service(postgres_engine).settle(settlement_command)

    def authorize():
        barrier.wait(timeout=10)
        return service.authorize_and_reserve(authorization_command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        settlement_future = executor.submit(settle)
        authorization_future = executor.submit(authorize)
        settlement = settlement_future.result(timeout=15)
        authorization = authorization_future.result(timeout=15)

    assert settlement.state.position.settlement_state is SettlementState.SETTLED
    with uow_factory() as unit_of_work:
        balances = unit_of_work.ledger.balances(case.campaign_id)
        active_order_risk = unit_of_work.reservations.active_total(case.campaign_id)
        active_position_risk = unit_of_work.positions.active_total(case.campaign_id)
        assert active_position_risk == Money(0)
        assert (
            active_order_risk.amount + active_position_risk.amount <= balances.ledger_equity.amount
        )
        if isinstance(authorization, AuthorizedOrderResult):
            assert authorization.capacity_before.active_position_risk in (Money(0), Money("40"))
        else:
            assert "RISK_CAPACITY_EXCEEDED" in authorization.risk_decision.reason_codes
        unit_of_work.commit()


@pytest.mark.parametrize("fault_at", tuple(SettlementFaultPoint))
def test_void_settlement_faults_roll_back_without_mutating_canceled_order(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
    fault_at: SettlementFaultPoint,
) -> None:
    case = _prepare_exact_void_partial(service)
    _cancel_exact_void(case, postgres_engine)
    before = _snapshot(postgres_engine, case.order.order_id)

    with pytest.raises(InjectedSettlementFault):
        _settlement_service(postgres_engine).settle(_void_settlement_command(case, fault_at))

    assert _snapshot(postgres_engine, case.order.order_id) == before
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(settlement_fact)) == 0
        assert connection.scalar(sa.select(sa.func.count()).select_from(position_settlement)) == 0


@pytest.mark.parametrize(
    "kind",
    ("classification", "value", "term", "rule", "source"),
)
def test_void_conflicts_fail_before_mutating_the_canceled_position(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
    kind: str,
) -> None:
    case = _prepare_exact_void_partial(service)
    _cancel_exact_void(case, postgres_engine)
    before = _snapshot(postgres_engine, case.order.order_id)
    command = _void_settlement_command(case)
    if kind == "classification":
        command = replace(
            command,
            fact=_rehashed_void_fact(
                command.fact,
                outcome_classification=SettlementOutcome.NO,
            ),
        )
    elif kind == "value":
        command = replace(
            command, fact=_rehashed_void_fact(command.fact, settlement_value=Decimal("0.39"))
        )
    elif kind == "term":
        command = replace(command, void_term_content_hash=ContentHash("0" * 64))
    elif kind == "rule":
        command = replace(
            command,
            fact=_rehashed_void_fact(command.fact, rule_content_hash=ContentHash("0" * 64)),
        )
    else:
        command = replace(
            command,
            fact=_rehashed_void_fact(command.fact, resolution_source="conflicting source"),
        )

    with pytest.raises(ValueValidationError):
        _settlement_service(postgres_engine).settle(command)
    assert _snapshot(postgres_engine, case.order.order_id) == before


def test_void_evidence_conflict_after_settlement_is_idempotent_and_atomic(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
) -> None:
    case = _prepare_exact_void_partial(service)
    _cancel_exact_void(case, postgres_engine)
    command = _void_settlement_command(case)
    _settlement_service(postgres_engine).settle(command)
    before = _snapshot(postgres_engine, case.order.order_id)
    conflict = replace(
        command,
        fact=_rehashed_void_fact(command.fact, fixture_id="conflicting-void-evidence"),
    )

    with pytest.raises(IdempotencyConflictError):
        _settlement_service(postgres_engine).settle(conflict)
    assert _snapshot(postgres_engine, case.order.order_id) == before


def test_durable_position_is_immutable_and_cannot_be_duplicated(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
) -> None:
    case = _prepare_exact_partial(service)
    execution = _execution_service(postgres_engine)
    opened_event, fill_event, _ = case.oracle.execution_events
    execution.apply_execution_event(_command(case, opened_event))
    execution.apply_execution_event(_command(case, fill_event))
    order_id = case.oracle.order.order_id

    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError):
            connection.execute(
                durable_position.update()
                .where(durable_position.c.originating_order_id == order_id)
                .values(maximum_remaining_loss=Decimal("1"))
            )
        transaction.rollback()

    with postgres_engine.connect() as connection:
        original = dict(
            connection.execute(
                sa.select(durable_position).where(
                    durable_position.c.originating_order_id == order_id
                )
            )
            .mappings()
            .one()
        )
    original["position_id"] = "duplicate-position"
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError):
            connection.execute(durable_position.insert().values(**original))
        transaction.rollback()

    assert len(_snapshot(postgres_engine, order_id)[3]) == 1


def test_replay_is_idempotent_and_conflicting_fill_identity_is_rejected(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
) -> None:
    case = _prepare_exact_partial(service)
    execution = _execution_service(postgres_engine)
    for event in case.oracle.execution_events:
        first = execution.apply_execution_event(_command(case, event))
        before_replay = _snapshot(postgres_engine, case.oracle.order.order_id)
        replay = execution.apply_execution_event(_command(case, event))
        assert replay.idempotent_replay is True
        assert replay.state == first.state
        assert _snapshot(postgres_engine, case.oracle.order.order_id) == before_replay

    fill_event = case.oracle.execution_events[1]
    before_conflict = _snapshot(postgres_engine, case.oracle.order.order_id)
    with pytest.raises(IdempotencyConflictError):
        execution.apply_execution_event(_command(case, replace(fill_event, fee=Money("2"))))
    assert _snapshot(postgres_engine, case.oracle.order.order_id) == before_conflict


@pytest.mark.parametrize("fault_at", tuple(PartialFillExecutionFaultPoint))
def test_each_fill_fault_rolls_back_every_row_and_clean_retry_succeeds(
    service: DurableAuthorizationService,
    postgres_engine: Engine,
    fault_at: PartialFillExecutionFaultPoint,
) -> None:
    case = _prepare_exact_partial(service)
    execution = _execution_service(postgres_engine)
    opened_event, fill_event, _ = case.oracle.execution_events
    execution.apply_execution_event(_command(case, opened_event))
    before = _snapshot(postgres_engine, case.oracle.order.order_id)

    with pytest.raises(InjectedPartialFillExecutionFault):
        execution.apply_execution_event(_command(case, fill_event, fault_at))

    assert _snapshot(postgres_engine, case.oracle.order.order_id) == before
    recovered = execution.recover(case.oracle.campaign.campaign_id, case.oracle.order.order_id)
    assert recovered.order.state is OrderState.OPEN
    assert recovered.position is None
    retried = execution.apply_execution_event(_command(case, fill_event))
    assert retried.idempotent_replay is False
    assert retried.state.order.state is OrderState.PARTIALLY_FILLED
    assert retried.state.position == case.oracle.position


def test_restart_recovery_preserves_position_journals_and_terminal_state(
    service: DurableAuthorizationService,
    postgres_settings: PostgresSettings,
) -> None:
    case = _prepare_exact_partial(service)
    campaign_id = case.oracle.campaign.campaign_id
    states = []
    for event in case.oracle.execution_events:
        engine = create_postgres_engine(postgres_settings)
        try:
            execution = _execution_service(engine)
            if states:
                assert execution.recover(campaign_id, case.oracle.order.order_id) == states[-1]
            result = execution.apply_execution_event(_command(case, event))
            states.append(result.state)
        finally:
            engine.dispose()
    final_engine = create_postgres_engine(postgres_settings)
    try:
        recovered = _execution_service(final_engine).recover(
            campaign_id, case.oracle.order.order_id
        )
        assert recovered == states[-1]
        assert recovered.order == case.oracle.order
        assert recovered.position == case.oracle.position
        assert len(_snapshot(final_engine, case.oracle.order.order_id)[4]) == 3
    finally:
        final_engine.dispose()
