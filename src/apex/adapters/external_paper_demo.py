"""Deterministic end-to-end External PAPER acceptance proof with no network I/O."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from apex.adapters.external_paper_fixture import (
    FULL_FILL,
    PARTIAL_FILL,
    REJECTED,
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
    AlpacaPaperAcceptanceCampaign,
    CancelExternalPaperInstruction,
    CreateAuthorizedExternalPaperInstruction,
    CreateExternalPaperAuthorization,
    CreatePersistentAlpacaPaperAcceptanceWorkflow,
    ExternalPaperReadinessResult,
    FlattenExternalPaperCampaign,
    InMemoryExternalPaperRepository,
    KillExternalPaper,
    PrepareAuthorizedExternalPaperExecution,
    PrepareAuthorizedExternalPaperReduction,
    ProcessExternalPaperFills,
    ResolveAmbiguousExternalPaperSubmission,
    SubmitAuthorizedPaperInstruction,
    SyncExternalPaperInstruction,
    SynchronizeExternalPaperCampaign,
    external_paper_authorization_confirmation,
    external_paper_flatten_confirmation,
    external_paper_kill_confirmation,
    ready_capability,
)
from apex.application.securities_paper import (
    BuildSecuritiesPaperOpportunities,
    BuildSecuritiesPaperOpportunityCommand,
    CreateSecuritiesPaperRun,
    CreateSecuritiesPaperRunCommand,
    ExecuteSecuritiesPaperOpportunityCommand,
    InMemorySecuritiesPaperCampaignBoundary,
    InMemorySecuritiesPaperRepository,
    RunSecuritiesPaperExecution,
)
from apex.domain.campaign import Campaign, CampaignPolicy, CampaignSnapshot
from apex.domain.external_paper import (
    ExternalPaperAuthorization,
    ExternalPaperBrokerEvidence,
    ExternalPaperInstruction,
    ExternalPaperInternalExecutionBinding,
)
from apex.domain.ledger import AccountCode, JournalFactory
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesSignal,
    SecuritiesSignalDirection,
)
from apex.domain.securities_paper import (
    SecuritiesExecutionPolicy,
    SecuritiesOrderAction,
    SecuritiesPaperOpportunity,
    SecuritiesPaperRun,
    authorize_paper_opportunity,
    create_securities_order,
)
from apex.domain.values import ContentHash, Money, Quantity, RiskFraction, content_digest

NOW = datetime(2026, 7, 28, 20, 0, tzinfo=UTC)
CAMPAIGN_ID = "external-paper-acceptance-fixture"
INSTRUMENT_ID = "external-paper-acceptance-spy"


def _hash(value: str) -> ContentHash:
    return ContentHash(content_digest(value))


def _policy() -> SecuritiesExecutionPolicy:
    return SecuritiesExecutionPolicy.next_bar_cross_v1(
        created_at=NOW,
        quantity_increment="0.5",
    )


def _descriptor() -> StrategyDescriptor:
    return StrategyDescriptor(
        strategy_id="external-paper-acceptance-strategy",
        version="v1",
        display_name="External PAPER acceptance fixture",
        family=StrategyFamily.SECURITIES,
        implementation_state=StrategyImplementationState.FIXTURE,
        authority_state=StrategyAuthorityState.PAPER,
        evidence_state=StrategyEvidenceState.FIXTURE,
        last_evidence_decision="fixture-review",
        supported_instrument_class="CONTINUOUS_SECURITIES",
        maximum_proposals_per_cycle=10,
        configuration_hash=_hash("external-paper-acceptance-config"),
    )


def _trial(
    policy: SecuritiesExecutionPolicy, descriptor: StrategyDescriptor
) -> PaperTrialAuthorization:
    return PaperTrialAuthorization(
        authorization_id="external-paper-acceptance-trial",
        review_id="external-paper-acceptance-review",
        strategy_id=descriptor.strategy_id,
        strategy_version=descriptor.version,
        campaign_id=CAMPAIGN_ID,
        strategy_configuration_hash=descriptor.configuration_hash,
        limits=PaperTrialLimits(
            paper_execution_policy_id=policy.policy_id,
            paper_execution_policy_hash=policy.canonical_hash,
            starts_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=1),
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
        state=PaperTrialAuthorizationState.ACTIVE,
        authorized_at=NOW,
        suspension_reason=None,
        revocation_reason=None,
        operator_action_id="fixture-operator-review",
        configuration_hash=_hash("external-paper-acceptance-trial"),
        canonical_input_hash=_hash("external-paper-acceptance-trial-input"),
        canonical_result_hash=_hash("external-paper-acceptance-trial-result"),
        usage=PaperTrialUsage(),
    )


def _bar(day: int) -> SecuritiesBarEvidence:
    opened = NOW + timedelta(days=day - 1, hours=-7)
    return SecuritiesBarEvidence(
        instrument_id=INSTRUMENT_ID,
        interval="ONE_DAY",
        adjustment_state=SecuritiesAdjustmentState.RAW,
        period_start=opened,
        period_end=opened + timedelta(hours=6),
        available_at=opened + timedelta(hours=6, minutes=1),
        open="100",
        high="101",
        low="99",
        close="100",
        volume="10000",
        source_identity="external-paper-acceptance-fixture-bars",
    )


def _context() -> tuple[
    InMemorySecuritiesPaperRepository,
    InMemorySecuritiesPaperCampaignBoundary,
    SecuritiesPaperRun,
    SecuritiesPaperOpportunity,
    PaperTrialAuthorization,
    StrategyDescriptor,
    SecuritiesExecutionPolicy,
    SecuritiesSignal,
]:
    policy = _policy()
    descriptor = _descriptor()
    trial = _trial(policy, descriptor)
    campaign_policy = CampaignPolicy(
        policy_version_id="external-paper-acceptance-policy",
        profile="FIXTURE",
        requested_risk_fraction="0.1",
        max_trade_loss_fraction="0.5",
        max_open_loss_fraction="0.8",
        min_synthetic_edge="0",
        max_spread="1",
        max_capital_lockup_seconds=86400,
        content_hash=_hash("external-paper-acceptance-policy"),
    )
    campaign = Campaign(
        campaign_id=CAMPAIGN_ID,
        contribution=Money("1000"),
        authorized_loss=Money("500"),
        policy=campaign_policy,
    )
    campaigns = InMemorySecuritiesPaperCampaignBoundary(
        (
            CampaignSnapshot(
                campaign=campaign,
                ledger_equity=Money("1000"),
                settled_cash=Money("1000"),
                active_reservations=Money("0"),
            ),
        )
    )
    securities = InMemorySecuritiesPaperRepository()
    securities.ledger.post(JournalFactory.contribution("fixture-capital", Money("1000"), NOW))
    run = CreateSecuritiesPaperRun(repository=securities, campaigns=campaigns).execute(
        CreateSecuritiesPaperRunCommand(
            campaign_id=CAMPAIGN_ID,
            trial=trial,
            descriptor=descriptor,
            data_batch_id="external-paper-acceptance-batch",
            data_batch_hash=_hash("external-paper-acceptance-batch"),
            policy=policy,
            start_date="2026-07-28",
            end_date="2026-07-30",
            signal_count=1,
            idempotency_key="external-paper-acceptance-run",
            requested_at=NOW,
        )
    )
    signal = SecuritiesSignal(
        signal_id="external-paper-acceptance-signal",
        research_run_id="external-paper-acceptance-research",
        specification_id="external-paper-acceptance-specification",
        instrument_id=INSTRUMENT_ID,
        signal_at=_bar(0).available_at,
        direction=SecuritiesSignalDirection.LONG,
        target_weight=Decimal("0.1"),
        reference_close=Decimal("100"),
        feature_start=_bar(0).period_start,
        feature_end=_bar(0).period_end,
        source_bar_ids=(_bar(0).bar_id,),
        reason_codes=("FIXTURE",),
        model_deployment_id=None,
        model_artifact_hash=None,
        canonical_hash=content_digest("external-paper-acceptance-signal"),
    )
    opportunity = BuildSecuritiesPaperOpportunities(securities).execute(
        BuildSecuritiesPaperOpportunityCommand(
            run_id=run.run_id,
            signal=signal,
            next_bar=_bar(1),
            requested_quantity=Quantity("1", "0.5"),
            policy=policy,
            holding_horizon_sessions=1,
        )
    )
    return securities, campaigns, run, opportunity, trial, descriptor, policy, signal


def _external_authorization(
    external: InMemoryExternalPaperRepository,
    campaigns: InMemorySecuritiesPaperCampaignBoundary,
    trial: PaperTrialAuthorization,
    descriptor: StrategyDescriptor,
) -> ExternalPaperAuthorization:
    authorization = ExternalPaperAuthorization.create(
        campaign_id=CAMPAIGN_ID,
        paper_trial_authorization_id=trial.authorization_id,
        portfolio_run_id="external-paper-acceptance-portfolio-run",
        portfolio_instruction_id="external-paper-acceptance-portfolio-instruction",
        allowed_symbols=("SPY",),
        maximum_capital="1000",
        maximum_position_quantity="1",
        maximum_order_count=10,
        starts_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(days=1),
        operator_action_id="fixture-external-paper-review",
    )
    return CreateExternalPaperAuthorization(repository=external, campaigns=campaigns).execute(
        authorization=authorization,
        trial=trial,
        descriptor=descriptor,
        portfolio_instruction=SimpleNamespace(
            instruction_id=authorization.portfolio_instruction_id,
            permitted_allocation=Decimal("1"),
        ),
        confirmation_text=external_paper_authorization_confirmation(
            campaign_id=CAMPAIGN_ID,
            trial_id=trial.authorization_id,
            portfolio_instruction_id=authorization.portfolio_instruction_id,
        ),
        now=NOW,
    )


def _open_fill(
    *,
    scenario: FixturePaperScenario = FULL_FILL,
    key: str = "acceptance-full",
    cancel_remainder: bool = False,
    cancel_before_processing: bool = False,
) -> dict[str, object]:
    securities, campaigns, run, opportunity, trial, descriptor, _policy_value, _signal_value = (
        _context()
    )
    external = InMemoryExternalPaperRepository()
    authorization = _external_authorization(external, campaigns, trial, descriptor)
    instruction = CreateAuthorizedExternalPaperInstruction(repository=external).execute(
        instruction=ExternalPaperInstruction.authorized(
            authorization=authorization,
            symbol="SPY",
            quantity="1",
            maximum_notional="100",
            expires_at=NOW + timedelta(hours=1),
            idempotency_key=key,
        ),
        now=NOW,
    )
    paper_authorization = authorize_paper_opportunity(
        opportunity=opportunity,
        settled_cash=Money("1000"),
        risk_capacity=Money("500"),
        active_risk=Money("0"),
        maximum_per_opportunity_risk=Money("500"),
        maximum_strategy_active_risk=Money("500"),
        trial_cumulative_remaining_risk=Money("500"),
    )
    order = create_securities_order(
        run_id=run.run_id,
        instrument_id=INSTRUMENT_ID,
        action=SecuritiesOrderAction.BUY,
        quantity=paper_authorization.quantity,
        reserved_amount=paper_authorization.maximum_loss,
        decision_at=opportunity.decision_at,
        earliest_fill_at=opportunity.earliest_fill_at,
        expires_at=opportunity.earliest_fill_at + timedelta(hours=1),
        idempotency_key=f"{key}-buy",
        authorization_id=paper_authorization.authorization_id,
        opportunity_id=opportunity.opportunity_id,
    )
    binding = ExternalPaperInternalExecutionBinding.pending(
        authorization=authorization,
        instruction=instruction,
        securities_paper_run_id=run.run_id,
        internal_order_id=order.order_id,
        risk_reservation_id=paper_authorization.reservation_id,
        execution_policy_id=run.execution_policy_id,
        execution_policy_hash=run.execution_policy_hash.value,
        instrument_id=INSTRUMENT_ID,
        intended_session="2026-07-29",
        idempotency_key=f"{key}-binding",
    )
    PrepareAuthorizedExternalPaperExecution(
        repository=external, securities_repository=securities
    ).execute(
        binding=binding,
        now=NOW,
        securities_authorization=paper_authorization,
        pending_order=order,
    )
    fixture = DeterministicPaperBrokerFixture(scenarios={key: scenario}, now=NOW)
    first = SubmitAuthorizedPaperInstruction(repository=external, broker=fixture).execute(
        instruction_id=instruction.instruction_id,
        capability=ready_capability(NOW),
        now=NOW,
    )
    replay = SubmitAuthorizedPaperInstruction(repository=external, broker=fixture).execute(
        instruction_id=instruction.instruction_id,
        capability=ready_capability(NOW),
        now=NOW,
    )
    if first.evidence is None:
        ResolveAmbiguousExternalPaperSubmission(repository=external, broker=fixture).execute(
            instruction_id=instruction.instruction_id
        )
    synchronized, reconciliation = SyncExternalPaperInstruction(
        repository=external, broker=fixture, securities_repository=securities
    ).execute(instruction_id=instruction.instruction_id)
    if cancel_before_processing:
        CancelExternalPaperInstruction(repository=external, broker=fixture).execute(
            instruction_id=instruction.instruction_id
        )
        synchronized, reconciliation = SyncExternalPaperInstruction(
            repository=external, broker=fixture, securities_repository=securities
        ).execute(instruction_id=instruction.instruction_id)
    applied = ProcessExternalPaperFills(
        repository=external, securities_repository=securities
    ).execute(instruction_ids=(instruction.instruction_id,))
    journal_count_before_replay = len(securities.ledger.journals)
    replay_applied = ProcessExternalPaperFills(
        repository=external, securities_repository=securities
    ).execute(instruction_ids=(instruction.instruction_id,))
    if cancel_remainder and not cancel_before_processing:
        CancelExternalPaperInstruction(repository=external, broker=fixture).execute(
            instruction_id=instruction.instruction_id
        )
        synchronized, reconciliation = SyncExternalPaperInstruction(
            repository=external, broker=fixture, securities_repository=securities
        ).execute(instruction_id=instruction.instruction_id)
    if cancel_before_processing:
        synchronized, reconciliation = SyncExternalPaperInstruction(
            repository=external, broker=fixture, securities_repository=securities
        ).execute(instruction_id=instruction.instruction_id)
    final_order = securities.order(order.order_id)
    positions = securities.positions(CAMPAIGN_ID)
    position = (
        securities.position(final_order.position_id)
        if final_order.position_id
        else (positions[0] if positions else None)
    )
    retained_binding = external.binding_for_instruction(instruction.instruction_id)
    cash = sum(
        (
            posting.amount.amount if posting.side.value == "DEBIT" else -posting.amount.amount
            for journal in securities.ledger.journals
            for posting in journal.postings
            if posting.account_code is AccountCode.CASH
        ),
        Decimal(),
    )
    return {
        "authority_id": authorization.authorization_id,
        "instruction_id": instruction.instruction_id,
        "order_id": order.order_id,
        "binding_id": binding.binding_id,
        "binding_state": retained_binding.state.value if retained_binding is not None else None,
        "submission_replayed": replay.replayed,
        "broker_evidence_id": first.evidence.evidence_id if first.evidence else None,
        "state": synchronized.state.value,
        "reconciliation": reconciliation.classification.value,
        "applied_fill_count": len(applied[0].applied_fill_ids),
        "replay_journal_count_unchanged": journal_count_before_replay
        == len(securities.ledger.journals),
        "replay_fill_ids": replay_applied[0].applied_fill_ids,
        "quantity": str(position.open_quantity.value) if position is not None else "0",
        "average_cost": str(position.average_cost) if position is not None else None,
        "cash": str(cash),
        "journal_count": len(securities.ledger.journals),
        "journals_balanced": all(journal.balanced for journal in securities.ledger.journals),
        "fixture_network_calls": fixture.network_calls,
    }


def _reduction_and_close() -> dict[str, object]:
    """Exercise the external SELL bridge against a normal fixture PAPER Position."""

    securities, campaigns, run, opportunity, trial, descriptor, policy, signal = _context()
    trial = replace(
        trial,
        limits=replace(
            trial.limits,
            starts_at=NOW - timedelta(days=2),
            expires_at=NOW + timedelta(days=3),
        ),
    )
    opening = RunSecuritiesPaperExecution(
        repository=securities,
        campaigns=campaigns,
    ).execute(
        ExecuteSecuritiesPaperOpportunityCommand(
            run_id=run.run_id,
            opportunity=opportunity,
            signal=signal,
            next_bar=_bar(1),
            policy=policy,
            trial=trial,
            strategy_authority_decision_id="fixture-external-reduction-authority",
            symbol="SPY",
            idempotency_key="fixture-external-reduction-opening",
            expires_at=_bar(2).period_start,
        )
    )
    if opening.position is None:
        raise RuntimeError("fixture opening Position is required for external reduction")
    position_id = opening.position.position_id
    external = InMemoryExternalPaperRepository()
    authorization = _external_authorization(external, campaigns, trial, descriptor)
    fixture = DeterministicPaperBrokerFixture(
        scenarios={
            "fixture-external-reduction-partial": FixturePaperScenario(
                "fixture-external-reduction-partial", "filled", "0.5"
            ),
            "fixture-external-reduction-close": FixturePaperScenario(
                "fixture-external-reduction-close", "filled", "0.5"
            ),
        },
        now=NOW,
    )

    def reduce(*, quantity: str, key: str) -> dict[str, object]:
        instruction = CreateAuthorizedExternalPaperInstruction(repository=external).execute(
            instruction=ExternalPaperInstruction.authorized(
                authorization=authorization,
                symbol="SPY",
                side="SELL",
                quantity=quantity,
                maximum_notional="100",
                expires_at=NOW + timedelta(hours=1),
                idempotency_key=key,
            ),
            now=NOW,
        )
        prepared = PrepareAuthorizedExternalPaperReduction(
            repository=external,
            securities_repository=securities,
        ).execute(
            instruction_id=instruction.instruction_id,
            position_id=position_id,
            decision_at=NOW - timedelta(seconds=30),
            earliest_fill_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(hours=1),
        )
        SubmitAuthorizedPaperInstruction(repository=external, broker=fixture).execute(
            instruction_id=instruction.instruction_id,
            capability=ready_capability(NOW),
            now=NOW,
        )
        synchronized, reconciliation = SyncExternalPaperInstruction(
            repository=external,
            broker=fixture,
            securities_repository=securities,
        ).execute(instruction_id=instruction.instruction_id)
        processed = ProcessExternalPaperFills(
            repository=external,
            securities_repository=securities,
        ).execute(instruction_ids=(instruction.instruction_id,))
        position = securities.position(position_id)
        return {
            "instruction_id": instruction.instruction_id,
            "order_id": prepared.order.order_id,
            "state": synchronized.state.value,
            "reconciliation": reconciliation.classification.value,
            "applied_fill_count": len(processed[0].applied_fill_ids),
            "position_quantity": str(position.open_quantity.value),
            "realized_pnl": str(position.realized_pnl),
            "fixture_network_calls": fixture.network_calls,
        }

    partial = reduce(quantity="0.5", key="fixture-external-reduction-partial")
    complete = reduce(quantity="0.5", key="fixture-external-reduction-close")
    final_position = securities.position(position_id)
    flatten = FlattenExternalPaperCampaign(
        repository=external,
        broker=fixture,
        securities_repository=securities,
    ).execute(
        campaign_id=CAMPAIGN_ID,
        reason="fixture close completion",
        confirmation_text=external_paper_flatten_confirmation(campaign_id=CAMPAIGN_ID),
        operator_action_id="fixture-close-flatten",
    )
    return {
        "partial_reduction": partial,
        "complete_close": complete,
        "final_position_state": final_position.state.value,
        "final_position_quantity": str(final_position.open_quantity.value),
        "flatten_completed": flatten.completed,
        "flatten_blockers": flatten.blockers,
        "journals_balanced": all(journal.balanced for journal in securities.ledger.journals),
    }


def _unknown_activity_kill_and_flatten() -> dict[str, object]:
    """Retain unknown activity, block openings, then prove safe kill/flatten controls."""

    securities, campaigns, run, _opportunity, trial, descriptor, policy, _signal = _context()
    external = InMemoryExternalPaperRepository()
    authorization = _external_authorization(external, campaigns, trial, descriptor)
    instruction = CreateAuthorizedExternalPaperInstruction(repository=external).execute(
        instruction=ExternalPaperInstruction.authorized(
            authorization=authorization,
            symbol="SPY",
            quantity="1",
            maximum_notional="100",
            expires_at=NOW + timedelta(hours=1),
            idempotency_key="acceptance-unknown-activity",
        ),
        now=NOW,
    )
    external.save_binding(
        ExternalPaperInternalExecutionBinding.pending(
            authorization=authorization,
            instruction=instruction,
            securities_paper_run_id=run.run_id,
            internal_order_id="fixture-unknown-activity-order",
            risk_reservation_id="fixture-unknown-activity-reservation",
            execution_policy_id=policy.policy_id,
            execution_policy_hash=policy.canonical_hash.value,
            instrument_id=INSTRUMENT_ID,
            intended_session="2026-07-29",
            idempotency_key="fixture-unknown-activity-binding",
        )
    )

    class UnknownActivityFixture:
        def submit(self, instruction: ExternalPaperInstruction) -> ExternalPaperBrokerEvidence:
            raise AssertionError("unknown activity must block submission before broker I/O")

        def account(self) -> ExternalPaperBrokerEvidence:
            return ExternalPaperBrokerEvidence.create(
                instruction_id=None,
                endpoint_identity="PAPER:GET:/v2/account",
                observation_type="ACCOUNT_READINESS",
                observed_at=NOW,
                broker_reported_at=NOW,
                request_id="fixture-account",
                sanitized_payload={"cash": "1000"},
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
                    request_id="fixture-unknown-activity",
                    sanitized_payload={"order_id": "unknown-order", "symbol": "SPY"},
                ),
            )

        def order(
            self, *, instruction_id: str, broker_order_id: str
        ) -> ExternalPaperBrokerEvidence:
            raise AssertionError("unsubmitted fixture instruction has no broker order")

        def cancel(
            self, *, instruction_id: str, broker_order_id: str
        ) -> ExternalPaperBrokerEvidence:
            raise AssertionError("unsubmitted fixture instruction cannot be canceled")

    fixture = UnknownActivityFixture()
    SynchronizeExternalPaperCampaign(repository=external, broker=fixture).execute(
        campaign_id=CAMPAIGN_ID
    )
    blocked = SubmitAuthorizedPaperInstruction(repository=external, broker=fixture).execute(
        instruction_id=instruction.instruction_id,
        capability=ready_capability(NOW),
        now=NOW,
    )
    kill_replayed = KillExternalPaper(repository=external).execute(
        campaign_id=CAMPAIGN_ID,
        reason="fixture acceptance kill",
        confirmation_text=external_paper_kill_confirmation(campaign_id=CAMPAIGN_ID),
        operator_action_id="fixture-kill",
    )
    flatten = FlattenExternalPaperCampaign(
        repository=external,
        broker=fixture,
        securities_repository=securities,
    ).execute(
        campaign_id=CAMPAIGN_ID,
        reason="fixture acceptance flatten",
        confirmation_text=external_paper_flatten_confirmation(campaign_id=CAMPAIGN_ID),
        operator_action_id="fixture-flatten",
    )
    return {
        "unknown_activity_block": external.has_unknown_broker_activity(CAMPAIGN_ID),
        "blocked_instruction_state": blocked.instruction.state.value,
        "kill_replayed": kill_replayed,
        "flatten_completed": flatten.completed,
        "flatten_blockers": flatten.blockers,
        "fixture_network_calls": 0,
    }


def external_paper_reconciliation_demo() -> dict[str, object]:
    """Run the bounded fixture acceptance core; it never invokes a network transport."""

    full = _open_fill()
    partial = _open_fill(
        scenario=PARTIAL_FILL,
        key="acceptance-partial",
        cancel_remainder=True,
    )
    rejected = _open_fill(scenario=REJECTED, key="acceptance-rejected")
    ambiguous = _open_fill(
        scenario=FixturePaperScenario(
            "AMBIGUOUS_FILLED", "filled", "1", ambiguous_after_submission=True
        ),
        key="acceptance-ambiguous",
    )
    fill_cancel_race = _open_fill(
        scenario=FixturePaperScenario(
            "FILL_CANCEL_RACE", "accepted", "0", cancel_fill_quantity="0.5"
        ),
        key="acceptance-fill-cancel-race",
        cancel_remainder=True,
        cancel_before_processing=True,
    )
    reduction = _reduction_and_close()
    safety_controls = _unknown_activity_kill_and_flatten()
    return {
        "demo": "EXTERNAL_PAPER_RECONCILIATION_FIXTURE_V1",
        "authority": "FIXTURE_ONLY",
        "full_fill": full,
        "partial_fill_canceled_remainder": partial,
        "rejected": rejected,
        "ambiguous_resolved": ambiguous,
        "fill_cancel_race": fill_cancel_race,
        "reduction_and_close": reduction,
        "unknown_activity_kill_and_flatten": safety_controls,
        "actual_broker_network_calls": 0,
        "actual_paper_orders": 0,
        "credential_use": False,
        "live_authority": "NONE",
        "genuine_shadow_mutated": False,
    }


def alpaca_paper_acceptance_demo() -> dict[str, object]:
    """Prepare, but never submit, one engineering-only acceptance instruction."""
    securities, campaigns, run, opportunity, trial, descriptor, _policy_value, _signal = _context()
    external = InMemoryExternalPaperRepository()
    authorization = _external_authorization(external, campaigns, trial, descriptor)
    instruction = CreateAuthorizedExternalPaperInstruction(repository=external).execute(
        instruction=ExternalPaperInstruction.authorized(
            authorization=authorization,
            symbol="SPY",
            quantity="1",
            maximum_notional="1000",
            expires_at=NOW + timedelta(hours=1),
            idempotency_key="alpaca-paper-engineering-acceptance",
        ),
        now=NOW,
    )
    paper_authorization = authorize_paper_opportunity(
        opportunity=opportunity,
        settled_cash=Money("1000"),
        risk_capacity=Money("500"),
        active_risk=Money("0"),
        maximum_per_opportunity_risk=Money("500"),
        maximum_strategy_active_risk=Money("500"),
        trial_cumulative_remaining_risk=Money("500"),
    )
    order = create_securities_order(
        run_id=run.run_id,
        instrument_id=INSTRUMENT_ID,
        action=SecuritiesOrderAction.BUY,
        quantity=paper_authorization.quantity,
        reserved_amount=paper_authorization.maximum_loss,
        decision_at=opportunity.decision_at,
        earliest_fill_at=opportunity.earliest_fill_at,
        expires_at=opportunity.earliest_fill_at + timedelta(hours=1),
        idempotency_key="alpaca-paper-engineering-acceptance-order",
        authorization_id=paper_authorization.authorization_id,
        opportunity_id=opportunity.opportunity_id,
    )
    binding = ExternalPaperInternalExecutionBinding.pending(
        authorization=authorization,
        instruction=instruction,
        securities_paper_run_id=run.run_id,
        internal_order_id=order.order_id,
        risk_reservation_id=paper_authorization.reservation_id,
        execution_policy_id=run.execution_policy_id,
        execution_policy_hash=run.execution_policy_hash.value,
        instrument_id=INSTRUMENT_ID,
        intended_session="2026-07-29",
        idempotency_key="alpaca-paper-engineering-acceptance-binding",
    )
    PrepareAuthorizedExternalPaperExecution(
        repository=external, securities_repository=securities
    ).execute(
        binding=binding, now=NOW, securities_authorization=paper_authorization, pending_order=order
    )
    acceptance = AlpacaPaperAcceptanceCampaign.create(
        idempotency_key="alpaca-paper-engineering-acceptance",
        campaign_id=CAMPAIGN_ID,
        trial_id=trial.authorization_id,
        authorization_id=authorization.authorization_id,
        instruction_id=instruction.instruction_id,
        reservation_id=paper_authorization.reservation_id,
        internal_order_id=order.order_id,
        binding_id=binding.binding_id,
        symbol="SPY",
        expires_at=instruction.expires_at,
        readiness_evidence_ids=(),
    )
    readiness = ExternalPaperReadinessResult(ready_capability(NOW), (), ())

    class DemoAuthorityPort:
        def create_acceptance_components(
            self,
            *,
            symbol: str,
            idempotency_key: str,
            now: datetime,
            readiness: ExternalPaperReadinessResult,
        ) -> tuple[
            AlpacaPaperAcceptanceCampaign,
            ExternalPaperInstruction,
            ExternalPaperInternalExecutionBinding,
        ]:
            assert symbol == "SPY"
            assert idempotency_key == "alpaca-paper-engineering-acceptance"
            assert now == NOW
            assert readiness.capability.state.value == "READY"
            return acceptance, instruction, binding

    workflow = CreatePersistentAlpacaPaperAcceptanceWorkflow(
        repository=external, securities_repository=securities, authority_port=DemoAuthorityPort()
    ).execute(
        symbol="SPY",
        idempotency_key="alpaca-paper-engineering-acceptance",
        readiness=readiness,
        now=NOW,
    )
    acceptance = workflow.acceptance
    preflight = workflow.preflight
    return {
        "purpose": "ENGINEERING_ACCEPTANCE_ONLY",
        "environment": "ALPACA_PAPER",
        "acceptance_id": acceptance.acceptance_id,
        "campaign_id": CAMPAIGN_ID,
        "trial_id": trial.authorization_id,
        "authorization_id": authorization.authorization_id,
        "instruction_id": instruction.instruction_id,
        "reservation_id": paper_authorization.reservation_id,
        "order_id": order.order_id,
        "binding_id": binding.binding_id,
        "preflight": preflight,
        "retained_preflight": external.latest_acceptance_preflight(instruction.instruction_id),
        "actual_paper_orders": 0,
        "network_calls": 0,
        "credential_use": False,
        "live_authority": "NONE",
        "journal_count": len(securities.ledger.journals),
    }
