from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.adapters.external_paper_fixture import (
    FULL_FILL,
    DeterministicPaperBrokerFixture,
    FixturePaperScenario,
)
from apex.application.autopilot import (
    StrategyAuthorityState,
    StrategyDescriptor,
    StrategyEvidenceState,
    StrategyFamily,
    StrategyImplementationState,
)
from apex.application.evidence_review import (
    PaperTrialAuthorization,
    PaperTrialAuthorizationState,
    PaperTrialLimits,
    PaperTrialUsage,
)
from apex.application.external_paper import (
    InMemoryExternalPaperRepository,
    PrepareAuthorizedExternalPaperExecution,
    PrepareAuthorizedExternalPaperReduction,
    ProcessExternalPaperFills,
    SubmitAuthorizedPaperInstruction,
    SyncExternalPaperInstruction,
    ValidateExternalPaperFill,
    ready_capability,
)
from apex.application.securities_paper import (
    ApplySecuritiesCorporateActions,
    ApplySecuritiesPaperFillIncrement,
    ApplySecuritiesPaperFillIncrementCommand,
    BuildSecuritiesPaperOpportunities,
    BuildSecuritiesPaperOpportunityCommand,
    CreateSecuritiesPaperRun,
    CreateSecuritiesPaperRunCommand,
    ExecuteSecuritiesPaperOpportunityCommand,
    ExitSecuritiesPositionCommand,
    ExitSecuritiesPositions,
    InMemorySecuritiesPaperCampaignBoundary,
    InMemorySecuritiesPaperRepository,
    MarkSecuritiesPositions,
    RunSecuritiesPaperExecution,
    SecuritiesPaperApplicationError,
    SecuritiesPaperIdempotencyConflictError,
    reconcile_securities_trial,
)
from apex.domain.campaign import Campaign, CampaignPolicy, CampaignSnapshot
from apex.domain.external_paper import (
    ExternalPaperAuthorization,
    ExternalPaperInstruction,
    ExternalPaperInternalExecutionBinding,
)
from apex.domain.ledger import JournalFactory, Ledger
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesCorporateActionEvidence,
    SecuritiesSignal,
    SecuritiesSignalDirection,
)
from apex.domain.securities_paper import (
    ExternalSecuritiesPaperRun,
    SecuritiesExecutionPolicy,
    SecuritiesExecutionSource,
    SecuritiesExitReason,
    SecuritiesFillState,
    SecuritiesOrderAction,
    SecuritiesPaperConformanceClassification,
    SecuritiesPaperError,
    SecuritiesPaperOrder,
    SecuritiesPositionStatus,
    authorize_paper_opportunity,
    compare_paper_execution,
    create_securities_order,
    execute_next_bar_order,
)
from apex.domain.values import (
    ContentHash,
    Money,
    Quantity,
    RiskFraction,
    content_digest,
)

NOW = datetime(2026, 7, 20, 21, 1, tzinfo=UTC)
INSTRUMENT_ID = "fixture-security"


def _hash(value: object) -> ContentHash:
    return ContentHash(content_digest(value))


def _bar(
    day: int,
    *,
    open_price: str = "100",
    close: str = "101",
    volume: str = "1000",
) -> SecuritiesBarEvidence:
    opens = NOW + timedelta(days=day, hours=17, minutes=29)
    closes = opens + timedelta(hours=6, minutes=30)
    return SecuritiesBarEvidence(
        instrument_id=INSTRUMENT_ID,
        interval="ONE_DAY",
        adjustment_state=SecuritiesAdjustmentState.RAW,
        period_start=opens,
        period_end=closes,
        available_at=closes + timedelta(minutes=1),
        open=open_price,
        high=str(max(Decimal(open_price), Decimal(close)) + 1),
        low=str(min(Decimal(open_price), Decimal(close)) - 1),
        close=close,
        volume=volume,
        source_identity="fixture-bars",
    )


def _signal() -> SecuritiesSignal:
    source = _bar(0)
    return SecuritiesSignal(
        signal_id="fixture-long-signal",
        research_run_id="fixture-research-run",
        specification_id="fixture-specification",
        instrument_id=INSTRUMENT_ID,
        signal_at=source.available_at,
        direction=SecuritiesSignalDirection.LONG,
        target_weight=Decimal("0.1"),
        reference_close=source.close,
        feature_start=source.period_start,
        feature_end=source.period_end,
        source_bar_ids=(source.bar_id,),
        reason_codes=("FIXTURE_LONG",),
        model_deployment_id=None,
        model_artifact_hash=None,
        canonical_hash=content_digest("fixture-long-signal"),
    )


def _policy() -> SecuritiesExecutionPolicy:
    return SecuritiesExecutionPolicy.next_bar_cross_v1(created_at=NOW)


def _fill_increment_command(
    *,
    order: SecuritiesPaperOrder,
    quantity: str,
    cumulative: str,
    source: str,
    economic: str,
    position_id: str | None = None,
    action: SecuritiesOrderAction = SecuritiesOrderAction.BUY,
) -> ApplySecuritiesPaperFillIncrementCommand:
    command = ApplySecuritiesPaperFillIncrementCommand(
        campaign_id="fixture-securities-campaign",
        paper_trial_authorization_id="fixture-securities-trial",
        strategy_id="fixture-securities-paper-v1",
        strategy_version="v1",
        strategy_configuration_hash=_hash("fixture-securities-paper-config"),
        order_id=order.order_id,
        risk_reservation_id="fixture-reservation",
        position_id=position_id,
        instrument_id=INSTRUMENT_ID,
        symbol="FIX",
        action=action,
        incremental_quantity=Quantity(quantity, "1"),
        expected_cumulative_quantity=Quantity(cumulative, "1"),
        authorized_quantity=order.requested_quantity,
        fill_price=Decimal("100"),
        fee=Money("1"),
        fill_at=order.earliest_fill_at,
        execution_source=SecuritiesExecutionSource.INTERNAL_SIMULATION,
        source_evidence_id=source,
        economic_identity_hash=_hash(economic),
        external_validated_fill_id=None,
        internal_simulation_evidence_id=source,
        execution_policy_hash=_policy().canonical_hash,
        idempotency_key=source,
        canonical_input_hash=_hash("placeholder"),
    )
    return replace(command, canonical_input_hash=command.expected_hash())


def _descriptor(
    authority: StrategyAuthorityState = StrategyAuthorityState.PAPER,
) -> StrategyDescriptor:
    return StrategyDescriptor(
        strategy_id="fixture-securities-paper-v1",
        version="v1",
        display_name="Fixture Securities PAPER",
        family=StrategyFamily.SECURITIES,
        implementation_state=StrategyImplementationState.FIXTURE,
        authority_state=authority,
        evidence_state=StrategyEvidenceState.FIXTURE,
        last_evidence_decision="fixture-review-approved",
        supported_instrument_class="CONTINUOUS_SECURITIES",
        maximum_proposals_per_cycle=3,
        configuration_hash=_hash("fixture-securities-paper-config"),
    )


def _trial(
    *,
    state: PaperTrialAuthorizationState = PaperTrialAuthorizationState.ACTIVE,
) -> PaperTrialAuthorization:
    policy = _policy()
    descriptor = _descriptor()
    return PaperTrialAuthorization(
        authorization_id="fixture-securities-trial",
        review_id="fixture-review",
        strategy_id=descriptor.strategy_id,
        strategy_version=descriptor.version,
        campaign_id="fixture-securities-campaign",
        strategy_configuration_hash=descriptor.configuration_hash,
        limits=PaperTrialLimits(
            paper_execution_policy_id=policy.policy_id,
            paper_execution_policy_hash=policy.canonical_hash,
            starts_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=30),
            maximum_paper_cycles=10,
            maximum_approved_opportunities=10,
            maximum_paper_orders=10,
            maximum_simultaneous_open_positions=4,
            maximum_per_opportunity_risk_fraction=RiskFraction("0.5"),
            maximum_strategy_active_risk_fraction=RiskFraction("0.8"),
            maximum_cumulative_authorized_risk=Money("10000"),
            maximum_cumulative_realized_paper_loss=Money("1000"),
            maximum_paper_drawdown=Money("1000"),
            maximum_capital_lockup_seconds=86400,
            allowed_instrument_class="CONTINUOUS_SECURITIES",
            allowed_provider="FIXTURE_MANAGED_DATA",
        ),
        state=state,
        authorized_at=NOW,
        suspension_reason=None,
        revocation_reason=None,
        operator_action_id="fixture-operator-action",
        configuration_hash=_hash("fixture-trial-config"),
        canonical_input_hash=_hash("fixture-trial-input"),
        canonical_result_hash=_hash("fixture-trial-result"),
        usage=PaperTrialUsage(),
    )


def _campaign_boundary() -> InMemorySecuritiesPaperCampaignBoundary:
    policy = CampaignPolicy(
        policy_version_id="fixture-campaign-policy",
        profile="FIXTURE_PAPER",
        requested_risk_fraction="0.1",
        max_trade_loss_fraction="0.5",
        max_open_loss_fraction="0.8",
        min_synthetic_edge="0",
        max_spread="1",
        max_capital_lockup_seconds=86400,
        content_hash=_hash("fixture-campaign-policy"),
    )
    campaign = Campaign(
        campaign_id="fixture-securities-campaign",
        contribution=Money("10000"),
        authorized_loss=Money("5000"),
        policy=policy,
    )
    return InMemorySecuritiesPaperCampaignBoundary(
        (
            CampaignSnapshot(
                campaign=campaign,
                ledger_equity=Money("10000"),
                settled_cash=Money("10000"),
                active_reservations=Money(0),
            ),
        )
    )


def _run_fixture() -> tuple[
    InMemorySecuritiesPaperRepository,
    InMemorySecuritiesPaperCampaignBoundary,
    object,
    object,
]:
    repository = InMemorySecuritiesPaperRepository()
    repository.ledger.post(JournalFactory.contribution("fixture-capital", Money("10000"), NOW))
    campaigns = _campaign_boundary()
    run = CreateSecuritiesPaperRun(
        repository=repository,
        campaigns=campaigns,
    ).execute(
        CreateSecuritiesPaperRunCommand(
            campaign_id="fixture-securities-campaign",
            trial=_trial(),
            descriptor=_descriptor(),
            data_batch_id="fixture-batch",
            data_batch_hash=_hash("fixture-batch"),
            policy=_policy(),
            start_date="2026-07-20",
            end_date="2026-07-30",
            signal_count=1,
            idempotency_key="fixture-run-v1",
            requested_at=NOW,
        )
    )
    opportunity = BuildSecuritiesPaperOpportunities(repository).execute(
        BuildSecuritiesPaperOpportunityCommand(
            run_id=run.run_id,
            signal=_signal(),
            next_bar=_bar(1),
            requested_quantity=Quantity("10", "1"),
            policy=_policy(),
            holding_horizon_sessions=3,
        )
    )
    return repository, campaigns, run, opportunity


def test_paper_run_requires_global_authority_and_active_exact_trial() -> None:
    repository = InMemorySecuritiesPaperRepository()
    service = CreateSecuritiesPaperRun(
        repository=repository,
        campaigns=_campaign_boundary(),
    )
    base = CreateSecuritiesPaperRunCommand(
        campaign_id="fixture-securities-campaign",
        trial=_trial(),
        descriptor=_descriptor(StrategyAuthorityState.SHADOW),
        data_batch_id="batch",
        data_batch_hash=_hash("batch"),
        policy=_policy(),
        start_date="2026-07-20",
        end_date="2026-07-21",
        signal_count=1,
        idempotency_key="authority-rejected",
        requested_at=NOW,
    )
    with pytest.raises(SecuritiesPaperApplicationError, match="GLOBAL_PAPER"):
        service.execute(base)
    with pytest.raises(SecuritiesPaperApplicationError, match="ACTIVE_PAPER"):
        service.execute(
            replace(
                base,
                descriptor=_descriptor(),
                trial=_trial(state=PaperTrialAuthorizationState.REVOKED),
            )
        )


def test_next_bar_policy_blocks_same_bar_lookahead() -> None:
    policy = _policy()
    signal = _signal()
    order = create_securities_order(
        run_id="run",
        instrument_id=INSTRUMENT_ID,
        action=SecuritiesOrderAction.BUY,
        quantity=Quantity("10", "1"),
        reserved_amount=Money("2000"),
        decision_at=signal.signal_at,
        earliest_fill_at=_bar(1).period_start,
        expires_at=_bar(2).period_start,
        idempotency_key="no-lookahead",
        authorization_id="authorization",
        opportunity_id="opportunity",
    )
    with pytest.raises(SecuritiesPaperError, match="same-bar"):
        execute_next_bar_order(
            order=order,
            bar=_bar(0),
            policy=policy,
            available_cash=Money("10000"),
        )


@pytest.mark.parametrize(
    ("volume", "expected_state", "expected_quantity"),
    (
        ("1000", SecuritiesFillState.FULL_FILL, Decimal("10")),
        ("50", SecuritiesFillState.PARTIAL_FILL, Decimal("5")),
        ("0", SecuritiesFillState.NO_FILL, Decimal("0")),
    ),
)
def test_execution_is_full_partial_or_no_fill_from_volume(
    volume: str,
    expected_state: SecuritiesFillState,
    expected_quantity: Decimal,
) -> None:
    repository, campaigns, run, opportunity = _run_fixture()
    result = RunSecuritiesPaperExecution(
        repository=repository,
        campaigns=campaigns,
    ).execute(
        ExecuteSecuritiesPaperOpportunityCommand(
            run_id=run.run_id,  # type: ignore[union-attr]
            opportunity=opportunity,  # type: ignore[arg-type]
            signal=_signal(),
            next_bar=_bar(1, volume=volume),
            policy=_policy(),
            trial=_trial(),
            strategy_authority_decision_id="fixture-authority-decision",
            symbol="FIX",
            idempotency_key=f"execution-{volume}",
            expires_at=_bar(2).period_start,
        )
    )
    assert result.fill.state is expected_state
    assert result.fill.quantity.value == expected_quantity
    assert result.order.reserved_amount == Money(0)
    assert (result.position is not None) is (expected_quantity > 0)


def test_execution_replay_retains_one_order_fill_position_and_journal_set() -> None:
    repository, campaigns, run, opportunity = _run_fixture()
    replayed_opportunity = BuildSecuritiesPaperOpportunities(repository).execute(
        BuildSecuritiesPaperOpportunityCommand(
            run_id=run.run_id,  # type: ignore[union-attr]
            signal=_signal(),
            next_bar=_bar(1),
            requested_quantity=Quantity("10", "1"),
            policy=_policy(),
            holding_horizon_sessions=3,
        )
    )
    assert replayed_opportunity.opportunity_id == opportunity.opportunity_id  # type: ignore[union-attr]
    assert repository.run(run.run_id).opportunity_count == 1  # type: ignore[union-attr]
    service = RunSecuritiesPaperExecution(repository=repository, campaigns=campaigns)
    command = ExecuteSecuritiesPaperOpportunityCommand(
        run_id=run.run_id,  # type: ignore[union-attr]
        opportunity=opportunity,  # type: ignore[arg-type]
        signal=_signal(),
        next_bar=_bar(1),
        policy=_policy(),
        trial=_trial(),
        strategy_authority_decision_id="fixture-authority-decision",
        symbol="FIX",
        idempotency_key="idempotent-execution",
        expires_at=_bar(2).period_start,
    )
    first = service.execute(command)
    second = service.execute(command)
    assert first.order.order_id == second.order.order_id
    assert len(repository.orders) == len(repository.fills) == len(repository._positions) == 1
    assert len(repository.ledger.journals) == 3


def test_shared_fill_increment_accumulates_replays_and_rejects_conflicts() -> None:
    repository = InMemorySecuritiesPaperRepository()
    order = create_securities_order(
        run_id="fixture-shared-increment-run",
        instrument_id=INSTRUMENT_ID,
        action=SecuritiesOrderAction.BUY,
        quantity=Quantity("2", "1"),
        reserved_amount=Money("202"),
        decision_at=NOW,
        earliest_fill_at=NOW + timedelta(days=1),
        expires_at=NOW + timedelta(days=2),
        idempotency_key="fixture-shared-increment-order",
        authorization_id="fixture-authorization",
        opportunity_id="fixture-opportunity",
    )
    repository.orders[order.order_id] = order
    service = ApplySecuritiesPaperFillIncrement(repository)

    first = service.execute(
        _fill_increment_command(
            order=order,
            quantity="1",
            cumulative="1",
            source="internal-fill-1",
            economic="economic-fill-1",
        )
    )
    assert first.order.state.value == "PARTIALLY_FILLED"
    assert first.position is not None
    assert first.position.open_quantity == Quantity("1", "1")
    assert len(repository.fills_for_order(order.order_id)) == 1
    assert len(repository.ledger.journals) == 2

    replay = service.execute(
        _fill_increment_command(
            order=order,
            quantity="1",
            cumulative="1",
            source="internal-fill-1",
            economic="economic-fill-1",
        )
    )
    assert replay.fill.fill_id == first.fill.fill_id
    assert len(repository.fills_for_order(order.order_id)) == 1
    assert len(repository.ledger.journals) == 2

    second = service.execute(
        _fill_increment_command(
            order=repository.order(order.order_id),
            position_id=first.position.position_id,
            quantity="1",
            cumulative="2",
            source="internal-fill-2",
            economic="economic-fill-2",
        )
    )
    assert second.order.state.value == "FILLED"
    assert second.fill.increment_ordinal == 2
    assert second.fill.remaining_quantity == Quantity("0", "1")
    assert second.position is not None
    assert second.position.open_quantity == Quantity("2", "1")
    assert len(repository.fills_for_order(order.order_id)) == 2
    assert len(repository.ledger.journals) == 4

    sell_order = create_securities_order(
        run_id=order.run_id,
        instrument_id=INSTRUMENT_ID,
        action=SecuritiesOrderAction.SELL,
        quantity=Quantity("2", "1"),
        reserved_amount=Money("0"),
        decision_at=NOW + timedelta(days=3),
        earliest_fill_at=NOW + timedelta(days=4),
        expires_at=NOW + timedelta(days=5),
        idempotency_key="fixture-shared-increment-sell",
        authorization_id="fixture-authorization",
        position_id=second.position.position_id,
    )
    repository.orders[sell_order.order_id] = sell_order
    partial_sale = service.execute(
        _fill_increment_command(
            order=sell_order,
            position_id=second.position.position_id,
            quantity="1",
            cumulative="1",
            source="internal-sell-1",
            economic="economic-sell-1",
            action=SecuritiesOrderAction.SELL,
        )
    )
    assert partial_sale.position is not None
    assert partial_sale.position.open_quantity == Quantity("1", "1")
    assert partial_sale.position.realized_pnl == Decimal("0")
    final_sale = service.execute(
        _fill_increment_command(
            order=repository.order(sell_order.order_id),
            position_id=second.position.position_id,
            quantity="1",
            cumulative="2",
            source="internal-sell-2",
            economic="economic-sell-2",
            action=SecuritiesOrderAction.SELL,
        )
    )
    assert final_sale.order.state.value == "FILLED"
    assert final_sale.position is not None
    assert final_sale.position.state is SecuritiesPositionStatus.CLOSED
    assert final_sale.position.open_quantity == Quantity("0", "1")
    assert len(repository.ledger.journals) == 8

    with pytest.raises(SecuritiesPaperIdempotencyConflictError, match="economic"):
        service.execute(
            _fill_increment_command(
                order=repository.order(order.order_id),
                position_id=first.position.position_id,
                quantity="1",
                cumulative="2",
                source="internal-fill-3",
                economic="economic-fill-2",
            )
        )


def test_validated_external_fill_uses_the_shared_increment_accounting_path() -> None:
    securities, _campaigns, run, _opportunity = _run_fixture()
    order = create_securities_order(
        run_id=run.run_id,
        instrument_id=INSTRUMENT_ID,
        action=SecuritiesOrderAction.BUY,
        quantity=Quantity("1", "1"),
        reserved_amount=Money("100"),
        decision_at=NOW - timedelta(minutes=1),
        earliest_fill_at=NOW,
        expires_at=NOW + timedelta(days=1),
        idempotency_key="external-bound-order",
        authorization_id="fixture-internal-authorization",
        opportunity_id="fixture-opportunity",
    )
    securities.orders[order.order_id] = order
    authorization = ExternalPaperAuthorization.create(
        campaign_id="fixture-securities-campaign",
        paper_trial_authorization_id="fixture-securities-trial",
        portfolio_run_id="fixture-portfolio-run",
        portfolio_instruction_id="fixture-portfolio-instruction",
        allowed_symbols=("FIX",),
        maximum_capital="100",
        maximum_position_quantity="1",
        maximum_order_count=1,
        starts_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(days=1),
        operator_action_id="fixture-external-approval",
    )
    external = InMemoryExternalPaperRepository()
    external.save_authorization(authorization)
    instruction = ExternalPaperInstruction.authorized(
        authorization=authorization,
        symbol="FIX",
        quantity="1",
        maximum_notional="100",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="external-shared-fill",
    )
    external.save_instruction(instruction)
    binding = ExternalPaperInternalExecutionBinding.pending(
        authorization=authorization,
        instruction=instruction,
        securities_paper_run_id=run.run_id,
        internal_order_id=order.order_id,
        risk_reservation_id="fixture-reservation",
        execution_policy_id=run.execution_policy_id,
        execution_policy_hash=run.execution_policy_hash.value,
        instrument_id=INSTRUMENT_ID,
        intended_session="2026-07-21",
        idempotency_key="external-shared-binding",
    )
    PrepareAuthorizedExternalPaperExecution(repository=external).execute(binding=binding, now=NOW)
    fixture = DeterministicPaperBrokerFixture(
        scenarios={"external-shared-fill": FULL_FILL}, now=NOW
    )
    submission = SubmitAuthorizedPaperInstruction(repository=external, broker=fixture).execute(
        instruction_id=instruction.instruction_id,
        capability=ready_capability(NOW),
        now=NOW,
    )
    assert submission.evidence is not None
    validated = ValidateExternalPaperFill(repository=external).execute(evidence=submission.evidence)
    processed = ProcessExternalPaperFills(
        repository=external,
        securities_repository=securities,
    ).execute(instruction_ids=(instruction.instruction_id,))
    replay = ProcessExternalPaperFills(
        repository=external,
        securities_repository=securities,
    ).execute(instruction_ids=(instruction.instruction_id,))

    assert processed[0].validated_fill_ids == (validated.validated_fill_id,)
    assert processed[0].applied_fill_ids
    assert replay[0].applied_fill_ids == processed[0].applied_fill_ids
    assert fixture.network_calls == 0
    assert securities.order(order.order_id).state.value == "FILLED"
    assert (
        external.binding_for_instruction(instruction.instruction_id).state.value == "FULLY_APPLIED"
    )
    assert securities.fills_for_order(order.order_id)[0].execution_source is (
        SecuritiesExecutionSource.EXTERNAL_PAPER_VALIDATED_FILL
    )
    assert len(securities.ledger.journals) == 2


def test_external_paper_preparation_creates_the_unfilled_internal_reservation_and_order() -> None:
    securities, _campaigns, run, opportunity = _run_fixture()
    assert run is not None
    authorization = authorize_paper_opportunity(
        opportunity=opportunity,
        settled_cash=Money("1000"),
        risk_capacity=Money("1000"),
        active_risk=Money("0"),
        maximum_per_opportunity_risk=Money("1000"),
        maximum_strategy_active_risk=Money("1000"),
        trial_cumulative_remaining_risk=Money("1000"),
    )
    pending = create_securities_order(
        run_id=run.run_id,
        instrument_id=INSTRUMENT_ID,
        action=SecuritiesOrderAction.BUY,
        quantity=authorization.quantity,
        reserved_amount=authorization.maximum_loss,
        decision_at=opportunity.decision_at,
        earliest_fill_at=opportunity.earliest_fill_at,
        expires_at=opportunity.earliest_fill_at + timedelta(hours=1),
        idempotency_key="prepared-external-order",
        authorization_id=authorization.authorization_id,
        opportunity_id=opportunity.opportunity_id,
    )
    external_authorization = ExternalPaperAuthorization.create(
        campaign_id=run.campaign_id,
        paper_trial_authorization_id=run.trial_authorization_id,
        portfolio_run_id="prepared-portfolio-run",
        portfolio_instruction_id="prepared-portfolio-instruction",
        allowed_symbols=("FIX",),
        maximum_capital="1000",
        maximum_position_quantity="10",
        maximum_order_count=1,
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
        operator_action_id="prepared-external-approval",
    )
    external = InMemoryExternalPaperRepository()
    external.save_authorization(external_authorization)
    instruction = ExternalPaperInstruction.authorized(
        authorization=external_authorization,
        symbol="FIX",
        quantity=str(authorization.quantity.value),
        maximum_notional="1000",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="prepared-external-instruction",
    )
    external.save_instruction(instruction)
    binding = ExternalPaperInternalExecutionBinding.pending(
        authorization=external_authorization,
        instruction=instruction,
        securities_paper_run_id=run.run_id,
        internal_order_id=pending.order_id,
        risk_reservation_id=authorization.reservation_id,
        execution_policy_id=run.execution_policy_id,
        execution_policy_hash=run.execution_policy_hash.value,
        instrument_id=INSTRUMENT_ID,
        intended_session="2026-07-21",
        idempotency_key="prepared-external-binding",
    )

    prepared = PrepareAuthorizedExternalPaperExecution(
        repository=external,
        securities_repository=securities,
    ).execute(
        binding=binding,
        now=NOW,
        securities_authorization=authorization,
        pending_order=pending,
    )

    assert prepared.internal_order_id == pending.order_id
    assert securities.order(pending.order_id).state.value == "READY"
    assert securities.order(pending.order_id).filled_quantity == Quantity("0", "1")


def test_marks_are_sequential_and_never_post_realized_journals() -> None:
    repository, campaigns, run, opportunity = _run_fixture()
    result = RunSecuritiesPaperExecution(
        repository=repository,
        campaigns=campaigns,
    ).execute(
        ExecuteSecuritiesPaperOpportunityCommand(
            run_id=run.run_id,  # type: ignore[union-attr]
            opportunity=opportunity,  # type: ignore[arg-type]
            signal=_signal(),
            next_bar=_bar(1),
            policy=_policy(),
            trial=_trial(),
            strategy_authority_decision_id="authority",
            symbol="FIX",
            idempotency_key="mark-execution",
            expires_at=_bar(2).period_start,
        )
    )
    assert result.position is not None
    service = MarkSecuritiesPositions(repository)
    before = len(repository.ledger.journals)
    first = service.execute(
        position_id=result.position.position_id,
        bar=_bar(2, close="110"),
        policy=_policy(),
        as_of=_bar(2).available_at,
    )
    second = service.execute(
        position_id=result.position.position_id,
        bar=_bar(3, close="111"),
        policy=_policy(),
        as_of=_bar(3).available_at,
    )
    assert first.mark.unrealized_pnl < second.mark.unrealized_pnl
    assert second.position.row_version == 3
    replayed = service.execute(
        position_id=result.position.position_id,
        bar=_bar(2, close="110"),
        policy=_policy(),
        as_of=_bar(2).available_at,
    )
    assert replayed.mark.mark_id == first.mark.mark_id
    assert len(repository.marks) == 2
    assert len(repository.ledger.journals) == before


def test_split_dividend_symbol_change_and_delisting_are_exact_and_fail_closed() -> None:
    repository, campaigns, run, opportunity = _run_fixture()
    result = RunSecuritiesPaperExecution(
        repository=repository,
        campaigns=campaigns,
    ).execute(
        ExecuteSecuritiesPaperOpportunityCommand(
            run_id=run.run_id,  # type: ignore[union-attr]
            opportunity=opportunity,  # type: ignore[arg-type]
            signal=_signal(),
            next_bar=_bar(1),
            policy=_policy(),
            trial=_trial(),
            strategy_authority_decision_id="authority",
            symbol="FIX",
            idempotency_key="action-execution",
            expires_at=_bar(2).period_start,
        )
    )
    assert result.position is not None
    service = ApplySecuritiesCorporateActions(repository)

    def action(kind: str, **values: str) -> SecuritiesCorporateActionEvidence:
        return SecuritiesCorporateActionEvidence(
            instrument_id=INSTRUMENT_ID,
            action_type=kind,
            effective_date="2026-07-23",
            available_at=_bar(2).available_at,
            source_identity=f"fixture-{kind}",
            **values,
        )

    split = service.execute(
        position_id=result.position.position_id,
        action=action("SPLIT", split_ratio="2"),
        applied_at=_bar(2).available_at,
        quantity_increment=Decimal("1"),
    )
    assert split.position.open_quantity.value == 20
    assert split.application.prior_cost_basis == split.application.resulting_cost_basis
    dividend = service.execute(
        position_id=result.position.position_id,
        action=action("CASH_DIVIDEND", cash_amount="0.5"),
        applied_at=_bar(2).available_at + timedelta(minutes=1),
        quantity_increment=Decimal("1"),
    )
    assert dividend.application.cash_entitlement == Money("10")
    assert repository.ledger.metrics().dividend_income == Money("10")
    renamed = service.execute(
        position_id=result.position.position_id,
        action=action("SYMBOL_CHANGE", successor_symbol="NEW"),
        applied_at=_bar(2).available_at + timedelta(minutes=2),
        quantity_increment=Decimal("1"),
    )
    assert renamed.position.current_symbol == "NEW"
    delisted = service.execute(
        position_id=result.position.position_id,
        action=action("DELISTING"),
        applied_at=_bar(2).available_at + timedelta(minutes=3),
        quantity_increment=Decimal("1"),
    )
    assert delisted.position.state is SecuritiesPositionStatus.DELISTING_BLOCKED
    assert delisted.application.reason_codes == ("DELISTING_TERMINAL_VALUE_REQUIRED",)
    before_replay = len(repository.ledger.journals)
    replayed_dividend = service.execute(
        position_id=result.position.position_id,
        action=action("CASH_DIVIDEND", cash_amount="0.5"),
        applied_at=_bar(2).available_at + timedelta(minutes=10),
        quantity_increment=Decimal("1"),
    )
    assert replayed_dividend.application.application_id == dividend.application.application_id
    assert len(repository.ledger.journals) == before_replay


def test_partial_then_full_exit_reconciles_cost_cash_and_realized_pnl() -> None:
    repository, campaigns, run, opportunity = _run_fixture()
    opened = RunSecuritiesPaperExecution(
        repository=repository,
        campaigns=campaigns,
    ).execute(
        ExecuteSecuritiesPaperOpportunityCommand(
            run_id=run.run_id,  # type: ignore[union-attr]
            opportunity=opportunity,  # type: ignore[arg-type]
            signal=_signal(),
            next_bar=_bar(1),
            policy=_policy(),
            trial=_trial(),
            strategy_authority_decision_id="authority",
            symbol="FIX",
            idempotency_key="exit-opening",
            expires_at=_bar(2).period_start,
        )
    )
    assert opened.position is not None
    service = ExitSecuritiesPositions(repository)
    partial = service.execute(
        ExitSecuritiesPositionCommand(
            position_id=opened.position.position_id,
            reason=SecuritiesExitReason.RISK_REDUCTION,
            decision_at=_bar(2).available_at,
            next_bar=_bar(3, open_price="110", close="111"),
            requested_quantity=Quantity("4", "1"),
            policy=_policy(),
            source_identity="fixture-risk-reduction",
            idempotency_key="partial-exit",
            expires_at=_bar(4).period_start,
        )
    )
    assert partial.position.open_quantity.value == 6
    closed = service.execute(
        ExitSecuritiesPositionCommand(
            position_id=opened.position.position_id,
            reason=SecuritiesExitReason.OPERATOR_EXIT,
            decision_at=_bar(4).available_at,
            next_bar=_bar(5, open_price="112", close="113"),
            requested_quantity=Quantity("6", "1"),
            policy=_policy(),
            source_identity="fixture-operator-exit",
            idempotency_key="complete-exit",
            expires_at=_bar(6).period_start,
        )
    )
    metrics = repository.ledger.metrics()
    assert closed.position.state is SecuritiesPositionStatus.CLOSED
    assert closed.position.open_quantity.value == 0
    assert metrics.position_cost == Money(0)
    assert metrics.cash.amount == (
        Decimal("10000")
        - opened.fill.notional.amount
        - opened.fill.modeled_commission.amount
        + partial.fill.notional.amount
        - partial.fill.modeled_commission.amount
        + closed.fill.notional.amount
        - closed.fill.modeled_commission.amount
    )
    assert metrics.net_trading_pnl == (closed.position.realized_pnl - metrics.fees.amount)


def test_external_paper_reduction_prepares_and_applies_partial_then_complete_close() -> None:
    securities, campaigns, run, opportunity = _run_fixture()
    opened = RunSecuritiesPaperExecution(
        repository=securities,
        campaigns=campaigns,
    ).execute(
        ExecuteSecuritiesPaperOpportunityCommand(
            run_id=run.run_id,  # type: ignore[union-attr]
            opportunity=opportunity,  # type: ignore[arg-type]
            signal=_signal(),
            next_bar=_bar(1),
            policy=_policy(),
            trial=_trial(),
            strategy_authority_decision_id="external-reduction-authority",
            symbol="FIX",
            idempotency_key="external-reduction-opening",
            expires_at=_bar(2).period_start,
        )
    )
    assert opened.position is not None
    external = InMemoryExternalPaperRepository()
    authorization = ExternalPaperAuthorization.create(
        campaign_id=run.campaign_id,
        paper_trial_authorization_id=run.trial_authorization_id,
        portfolio_run_id="external-reduction-portfolio-run",
        portfolio_instruction_id="external-reduction-portfolio-instruction",
        allowed_symbols=("FIX",),
        maximum_capital="10000",
        maximum_position_quantity="10",
        maximum_order_count=2,
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
        operator_action_id="external-reduction-review",
    )
    external.save_authorization(authorization)

    def reduce(*, quantity: str, key: str) -> None:
        instruction = ExternalPaperInstruction.authorized(
            authorization=authorization,
            symbol="FIX",
            side="SELL",
            quantity=quantity,
            maximum_notional="10000",
            expires_at=NOW + timedelta(hours=1),
            idempotency_key=key,
        )
        external.save_instruction(instruction)
        prepared = PrepareAuthorizedExternalPaperReduction(
            repository=external,
            securities_repository=securities,
        ).execute(
            instruction_id=instruction.instruction_id,
            position_id=opened.position.position_id,
            decision_at=NOW - timedelta(minutes=2),
            earliest_fill_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        )
        assert prepared.order.action is SecuritiesOrderAction.SELL
        assert prepared.order.position_id == opened.position.position_id
        fixture = DeterministicPaperBrokerFixture(
            scenarios={key: FixturePaperScenario(key, "filled", quantity)}, now=NOW
        )
        SubmitAuthorizedPaperInstruction(repository=external, broker=fixture).execute(
            instruction_id=instruction.instruction_id,
            capability=ready_capability(NOW),
            now=NOW,
        )
        synced, reconciliation = SyncExternalPaperInstruction(
            repository=external,
            broker=fixture,
            securities_repository=securities,
        ).execute(instruction_id=instruction.instruction_id)
        applied = ProcessExternalPaperFills(
            repository=external,
            securities_repository=securities,
        ).execute(instruction_ids=(instruction.instruction_id,))
        assert fixture.network_calls == 0
        assert synced.state.value == "FILLED"
        assert reconciliation.classification.value == "RECONCILED"
        assert len(applied[0].applied_fill_ids) == 1

    reduce(quantity="4", key="external-reduction-partial")
    partial = securities.position(opened.position.position_id)
    assert partial.open_quantity == Quantity("6", "1")
    assert partial.cumulative_sale_proceeds == Money("400")
    assert partial.realized_pnl == (Decimal("100") - opened.position.average_cost) * Decimal("4")

    reduce(quantity="6", key="external-reduction-close")
    closed = securities.position(opened.position.position_id)
    assert closed.state is SecuritiesPositionStatus.CLOSED
    assert closed.open_quantity == Quantity("0", "1")
    assert closed.cumulative_sale_proceeds == Money("1000")
    assert closed.realized_pnl == (Decimal("100") - opened.position.average_cost) * Decimal("10")
    assert all(journal.balanced for journal in securities.ledger.journals)


def test_suspended_trial_blocks_opening_but_exit_service_has_no_opening_gate() -> None:
    suspended = reconcile_securities_trial(
        _trial(),
        usage=PaperTrialUsage(
            completed_cycles=10,
            paper_drawdown=Money("1000"),
        ),
    )
    assert suspended.state is PaperTrialAuthorizationState.SUSPENDED_LIMIT
    repository, campaigns, run, opportunity = _run_fixture()
    with pytest.raises(SecuritiesPaperApplicationError, match="ACTIVE_PAPER"):
        RunSecuritiesPaperExecution(
            repository=repository,
            campaigns=campaigns,
        ).execute(
            ExecuteSecuritiesPaperOpportunityCommand(
                run_id=run.run_id,  # type: ignore[union-attr]
                opportunity=opportunity,  # type: ignore[arg-type]
                signal=_signal(),
                next_bar=_bar(1),
                policy=_policy(),
                trial=suspended,
                strategy_authority_decision_id="authority",
                symbol="FIX",
                idempotency_key="blocked-opening",
                expires_at=_bar(2).period_start,
            )
        )


def test_sale_and_dividend_journals_balance_and_report_income() -> None:
    ledger = Ledger()
    ledger.post(JournalFactory.contribution("capital", Money("100"), NOW))
    ledger.post(JournalFactory.purchase("buy", Money("40"), NOW))
    ledger.post(JournalFactory.sale("sell", Money("50"), Money("40"), NOW))
    ledger.post(JournalFactory.dividend("dividend", Money("2"), NOW))
    assert all(item.balanced for item in ledger.journals)
    assert ledger.metrics().cash == Money("112")
    assert ledger.metrics().dividend_income == Money("2")
    assert ledger.metrics().net_trading_pnl == Decimal("12")


def test_paper_conformance_is_exact_explained_and_honestly_unavailable() -> None:
    _repository, _campaigns, run, _opportunity = _run_fixture()
    events = (("ORDER", "one"), ("FILL", "one"))

    def external(
        state: str,
        retained: tuple[tuple[str, str], ...],
    ) -> ExternalSecuritiesPaperRun:
        return ExternalSecuritiesPaperRun(
            external_run_id=f"external-{state}-{len(retained)}",
            engine_identity="LEAN",
            engine_version="fixture-v1",
            state=state,
            apex_run_id=run.run_id,  # type: ignore[union-attr]
            request_hash=_hash((state, retained)),
            result_hash=_hash(retained) if retained else None,
            order_count=1,
            fill_count=1,
            position_count=1,
            ending_cash=Money("10000"),
            realized_pnl=Decimal(0),
            maximum_drawdown=Decimal(0),
            canonical_events=retained,
            warnings=("EXTERNAL ENGINE OUTPUT IS EVIDENCE, NOT APEX AUTHORITY",),
        )

    exact = compare_paper_execution(
        run=run,  # type: ignore[arg-type]
        apex_events=events,
        external=external("COMPLETED", events),
    )
    explained = compare_paper_execution(
        run=run,  # type: ignore[arg-type]
        apex_events=events,
        external=external("COMPLETED", events[:1]),
    )
    unavailable = compare_paper_execution(
        run=run,  # type: ignore[arg-type]
        apex_events=events,
        external=external("LEAN_RUNTIME_UNAVAILABLE", ()),
    )
    assert exact.classification is SecuritiesPaperConformanceClassification.CONFORMANT
    assert explained.classification is SecuritiesPaperConformanceClassification.EXPLAINED_DIFFERENCE
    assert unavailable.classification is SecuritiesPaperConformanceClassification.ENGINE_UNAVAILABLE
