"""Governed Securities PAPER services over Apex Campaign, trial, and ledger authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, cast

from apex.application.autopilot import (
    StrategyAuthorityState,
    StrategyDescriptor,
)
from apex.application.evidence_review import (
    PaperTrialAuthorization,
    PaperTrialAuthorizationState,
    PaperTrialUsage,
)
from apex.domain.campaign import CampaignSnapshot
from apex.domain.ledger import Journal, JournalFactory, Ledger
from apex.domain.securities import (
    SecuritiesBarEvidence,
    SecuritiesCorporateActionEvidence,
    SecuritiesSignal,
)
from apex.domain.securities_paper import (
    AppliedCorporateAction,
    AppliedSecuritiesExit,
    AppliedSecuritiesMark,
    ExternalSecuritiesPaperRun,
    SecuritiesActionApplicationState,
    SecuritiesCorporateActionApplication,
    SecuritiesExecutionLineage,
    SecuritiesExecutionPolicy,
    SecuritiesExecutionSource,
    SecuritiesExitDecision,
    SecuritiesExitReason,
    SecuritiesFillEvidence,
    SecuritiesFillState,
    SecuritiesMarkEvidence,
    SecuritiesOrderAction,
    SecuritiesOrderState,
    SecuritiesPaperAuthorization,
    SecuritiesPaperConformanceReport,
    SecuritiesPaperOpportunity,
    SecuritiesPaperOrder,
    SecuritiesPaperRun,
    SecuritiesPaperRunState,
    SecuritiesPositionState,
    add_buy_fill_to_position,
    apply_corporate_action,
    apply_sell_fill,
    authorize_paper_opportunity,
    build_exit_decision,
    build_paper_opportunity,
    compare_paper_execution,
    create_securities_order,
    execute_next_bar_order,
    mark_position,
    position_from_buy_fill,
)
from apex.domain.values import (
    ContentHash,
    Money,
    Quantity,
    content_digest,
    deterministic_id,
    parse_decimal,
)


class SecuritiesPaperApplicationError(RuntimeError):
    """A governed Securities PAPER command cannot proceed safely."""


class SecuritiesPaperIdempotencyConflictError(SecuritiesPaperApplicationError):
    """An idempotency key was reused with different immutable content."""


class SecuritiesPaperRepository(Protocol):
    def save_policy(self, policy: SecuritiesExecutionPolicy) -> SecuritiesExecutionPolicy: ...

    def save_run(self, run: SecuritiesPaperRun) -> SecuritiesPaperRun: ...

    def run(self, run_id: str) -> SecuritiesPaperRun: ...

    def save_opportunity(
        self, opportunity: SecuritiesPaperOpportunity
    ) -> SecuritiesPaperOpportunity: ...

    def save_authorization(
        self, authorization: SecuritiesPaperAuthorization
    ) -> SecuritiesPaperAuthorization: ...

    def save_pending_order(self, order: SecuritiesPaperOrder) -> SecuritiesPaperOrder: ...

    def cancel_open_order(self, order_id: str) -> SecuritiesPaperOrder: ...

    def save_execution(
        self,
        *,
        order: SecuritiesPaperOrder,
        fill: SecuritiesFillEvidence,
        position: SecuritiesPositionState | None,
        lineage: SecuritiesExecutionLineage,
        journals: tuple[Journal, ...],
    ) -> None: ...

    def save_mark(self, applied: AppliedSecuritiesMark) -> None: ...

    def mark(
        self,
        *,
        position_id: str,
        source_bar_id: str,
    ) -> SecuritiesMarkEvidence | None: ...

    def save_action(
        self,
        applied: AppliedCorporateAction,
        *,
        journal: Journal | None,
    ) -> None: ...

    def action_application(
        self,
        *,
        position_id: str,
        action_id: str,
    ) -> SecuritiesCorporateActionApplication | None: ...

    def save_exit(
        self,
        *,
        decision: SecuritiesExitDecision,
        order: SecuritiesPaperOrder,
        fill: SecuritiesFillEvidence,
        applied: AppliedSecuritiesExit,
        journals: tuple[Journal, ...],
    ) -> None: ...

    def save_conformance(
        self,
        report: SecuritiesPaperConformanceReport,
        external: ExternalSecuritiesPaperRun,
    ) -> SecuritiesPaperConformanceReport: ...

    def positions(self, campaign_id: str) -> tuple[SecuritiesPositionState, ...]: ...

    def position(self, position_id: str) -> SecuritiesPositionState: ...

    def has_order(self, order_id: str) -> bool: ...

    def order(self, order_id: str) -> SecuritiesPaperOrder: ...

    def fills_for_order(self, order_id: str) -> tuple[SecuritiesFillEvidence, ...]: ...

    def fill_by_source(
        self,
        *,
        execution_source: SecuritiesExecutionSource,
        source_evidence_id: str,
    ) -> SecuritiesFillEvidence | None: ...

    def fill_by_economic_identity(
        self, economic_identity_hash: ContentHash
    ) -> SecuritiesFillEvidence | None: ...

    def append_fill_increment(
        self,
        *,
        order: SecuritiesPaperOrder,
        fill: SecuritiesFillEvidence,
        position: SecuritiesPositionState | None,
        journals: tuple[Journal, ...],
    ) -> None: ...

    def status(self, campaign_id: str) -> dict[str, object]: ...


class SecuritiesPaperCampaignBoundary(Protocol):
    def snapshot(self, campaign_id: str) -> CampaignSnapshot: ...

    def operating_state(self, campaign_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class CreateSecuritiesPaperRunCommand:
    campaign_id: str
    trial: PaperTrialAuthorization
    descriptor: StrategyDescriptor
    data_batch_id: str
    data_batch_hash: ContentHash
    policy: SecuritiesExecutionPolicy
    start_date: str
    end_date: str
    signal_count: int
    idempotency_key: str
    requested_at: datetime

    @property
    def request_hash(self) -> ContentHash:
        return ContentHash(
            content_digest(
                {
                    "campaign_id": self.campaign_id,
                    "trial_authorization_id": self.trial.authorization_id,
                    "strategy_id": self.descriptor.strategy_id,
                    "strategy_version": self.descriptor.version,
                    "strategy_configuration_hash": self.descriptor.configuration_hash,
                    "data_batch_id": self.data_batch_id,
                    "data_batch_hash": self.data_batch_hash,
                    "execution_policy_id": self.policy.policy_id,
                    "execution_policy_hash": self.policy.canonical_hash,
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                    "signal_count": self.signal_count,
                    "idempotency_key": self.idempotency_key,
                    "requested_at": _aware(self.requested_at),
                }
            )
        )


class CreateSecuritiesPaperRun:
    def __init__(
        self,
        *,
        repository: SecuritiesPaperRepository,
        campaigns: SecuritiesPaperCampaignBoundary,
    ) -> None:
        self._repository = repository
        self._campaigns = campaigns

    def execute(self, command: CreateSecuritiesPaperRunCommand) -> SecuritiesPaperRun:
        now = _aware(command.requested_at)
        status = self._repository.status(command.campaign_id)
        trial = reconcile_securities_trial(
            command.trial,
            usage=_usage_with_status(command.trial.usage, status),
        )
        descriptor = command.descriptor
        reasons: list[str] = []
        if self._campaigns.operating_state(command.campaign_id) != "RUNNING":
            reasons.append("CAMPAIGN_NOT_RUNNING")
        if descriptor.authority_state is not StrategyAuthorityState.PAPER:
            reasons.append("GLOBAL_PAPER_AUTHORITY_REQUIRED")
        if descriptor.supported_instrument_class != "CONTINUOUS_SECURITIES":
            reasons.append("SECURITIES_INSTRUMENT_CLASS_REQUIRED")
        if not trial.active_at(now):
            reasons.append("ACTIVE_PAPER_TRIAL_REQUIRED")
        if (
            trial.campaign_id != command.campaign_id
            or trial.strategy_id != descriptor.strategy_id
            or trial.strategy_version != descriptor.version
            or trial.strategy_configuration_hash != descriptor.configuration_hash
        ):
            reasons.append("TRIAL_STRATEGY_CAMPAIGN_BINDING_MISMATCH")
        if (
            trial.limits.paper_execution_policy_id != command.policy.policy_id
            or trial.limits.paper_execution_policy_hash != command.policy.canonical_hash
        ):
            reasons.append("TRIAL_EXECUTION_POLICY_MISMATCH")
        if trial.limits.allowed_instrument_class != "CONTINUOUS_SECURITIES":
            reasons.append("TRIAL_INSTRUMENT_CLASS_MISMATCH")
        if reasons:
            raise SecuritiesPaperApplicationError(",".join(sorted(set(reasons))))
        self._repository.save_policy(command.policy)
        payload = {
            "request_hash": command.request_hash,
            "trial_authorization_id": trial.authorization_id,
        }
        run = SecuritiesPaperRun(
            run_id=deterministic_id("securities_paper_run", payload),
            campaign_id=command.campaign_id,
            trial_authorization_id=trial.authorization_id,
            strategy_id=descriptor.strategy_id,
            strategy_version=descriptor.version,
            strategy_configuration_hash=descriptor.configuration_hash,
            data_batch_id=command.data_batch_id,
            data_batch_hash=command.data_batch_hash,
            execution_policy_id=command.policy.policy_id,
            execution_policy_hash=command.policy.canonical_hash,
            start_date=command.start_date,
            end_date=command.end_date,
            state=SecuritiesPaperRunState.RUNNING,
            signal_count=command.signal_count,
            opportunity_count=0,
            accepted_opportunity_count=0,
            rejected_opportunity_count=0,
            order_count=0,
            fill_count=0,
            position_count=0,
            closed_position_count=0,
            mark_count=0,
            corporate_action_application_count=0,
            starting_cash=self._campaigns.snapshot(command.campaign_id).settled_cash,
            ending_cash=self._campaigns.snapshot(command.campaign_id).settled_cash,
            gross_realized_pnl=Decimal(0),
            net_realized_pnl=Decimal(0),
            maximum_drawdown=Decimal(0),
            idempotency_key=command.idempotency_key,
            started_at=now,
            completed_at=None,
            failure_code=None,
            canonical_input_hash=command.request_hash,
            canonical_result_hash=None,
        )
        return self._repository.save_run(run)


@dataclass(frozen=True, slots=True)
class BuildSecuritiesPaperOpportunityCommand:
    run_id: str
    signal: SecuritiesSignal
    next_bar: SecuritiesBarEvidence
    requested_quantity: Quantity
    policy: SecuritiesExecutionPolicy
    holding_horizon_sessions: int


class BuildSecuritiesPaperOpportunities:
    def __init__(self, repository: SecuritiesPaperRepository) -> None:
        self._repository = repository

    def execute(
        self, command: BuildSecuritiesPaperOpportunityCommand
    ) -> SecuritiesPaperOpportunity:
        run = self._repository.run(command.run_id)
        if run.state is not SecuritiesPaperRunState.RUNNING:
            raise SecuritiesPaperApplicationError("Securities PAPER run is not active")
        opportunity = build_paper_opportunity(
            run_id=run.run_id,
            signal=command.signal,
            next_bar=command.next_bar,
            requested_quantity=command.requested_quantity,
            policy=command.policy,
            holding_horizon_sessions=command.holding_horizon_sessions,
        )
        return self._repository.save_opportunity(opportunity)


@dataclass(frozen=True, slots=True)
class ExecuteSecuritiesPaperOpportunityCommand:
    run_id: str
    opportunity: SecuritiesPaperOpportunity
    signal: SecuritiesSignal
    next_bar: SecuritiesBarEvidence | None
    policy: SecuritiesExecutionPolicy
    trial: PaperTrialAuthorization
    strategy_authority_decision_id: str
    symbol: str
    idempotency_key: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SecuritiesPaperExecutionResult:
    authorization: SecuritiesPaperAuthorization
    order: SecuritiesPaperOrder
    fill: SecuritiesFillEvidence
    position: SecuritiesPositionState | None
    lineage: SecuritiesExecutionLineage
    journals: tuple[Journal, ...]


class RunSecuritiesPaperExecution:
    def __init__(
        self,
        *,
        repository: SecuritiesPaperRepository,
        campaigns: SecuritiesPaperCampaignBoundary,
    ) -> None:
        self._repository = repository
        self._campaigns = campaigns

    def execute(
        self, command: ExecuteSecuritiesPaperOpportunityCommand
    ) -> SecuritiesPaperExecutionResult:
        run = self._repository.run(command.run_id)
        if run.state is not SecuritiesPaperRunState.RUNNING:
            raise SecuritiesPaperApplicationError("Securities PAPER run is not active")
        if self._campaigns.operating_state(run.campaign_id) != "RUNNING":
            raise SecuritiesPaperApplicationError("CAMPAIGN_NOT_RUNNING")
        status = self._repository.status(run.campaign_id)
        trial = reconcile_securities_trial(
            command.trial,
            usage=_usage_with_status(command.trial.usage, status),
        )
        if not trial.active_at(command.opportunity.decision_at):
            raise SecuritiesPaperApplicationError("ACTIVE_PAPER_TRIAL_REQUIRED")
        if trial.authorization_id != run.trial_authorization_id:
            raise SecuritiesPaperApplicationError("PAPER_TRIAL_RUN_MISMATCH")
        snapshot = self._campaigns.snapshot(run.campaign_id)
        limits = trial.limits
        settled_cash = Money(cast(Decimal, status["cash"]))
        active_position_risk = Money(cast(Decimal, status["position_cost"]))
        active_risk = Money(
            max(
                trial.usage.active_strategy_risk.amount,
                active_position_risk.amount,
            )
        )
        campaign_risk_capacity = Money(
            max(
                Decimal(0),
                snapshot.campaign.authorized_loss.amount - active_position_risk.amount,
            )
        )
        authorization = authorize_paper_opportunity(
            opportunity=command.opportunity,
            settled_cash=settled_cash,
            risk_capacity=campaign_risk_capacity,
            active_risk=active_risk,
            maximum_per_opportunity_risk=Money(
                snapshot.ledger_equity.amount * limits.maximum_per_opportunity_risk_fraction.value
            ),
            maximum_strategy_active_risk=Money(
                snapshot.ledger_equity.amount * limits.maximum_strategy_active_risk_fraction.value
            ),
            trial_cumulative_remaining_risk=Money(
                max(
                    Decimal(0),
                    limits.maximum_cumulative_authorized_risk.amount
                    - trial.usage.cumulative_authorized_risk.amount,
                )
            ),
        )
        self._repository.save_authorization(authorization)
        if not authorization.accepted:
            raise SecuritiesPaperApplicationError("SECURITIES_OPPORTUNITY_REJECTED")
        order = create_securities_order(
            run_id=run.run_id,
            instrument_id=command.opportunity.instrument_id,
            action=SecuritiesOrderAction.BUY,
            quantity=authorization.quantity,
            reserved_amount=authorization.maximum_loss,
            decision_at=command.opportunity.decision_at,
            earliest_fill_at=command.opportunity.earliest_fill_at,
            expires_at=command.expires_at,
            idempotency_key=command.idempotency_key,
            authorization_id=authorization.authorization_id,
            opportunity_id=command.opportunity.opportunity_id,
        )
        applied = execute_next_bar_order(
            order=order,
            bar=command.next_bar,
            policy=command.policy,
            available_cash=settled_cash,
        )
        position = (
            position_from_buy_fill(
                campaign_id=run.campaign_id,
                symbol=command.symbol,
                fill=applied.fill,
            )
            if applied.fill.quantity.value > 0
            else None
        )
        final_fill = replace(
            applied.fill,
            position_id=position.position_id if position is not None else None,
        )
        final_order = replace(
            applied.order,
            position_id=position.position_id if position is not None else None,
        )
        purchase = (
            JournalFactory.purchase(
                final_fill.fill_id,
                final_fill.notional,
                final_fill.occurred_at,
            )
            if final_fill.notional.amount > 0
            else None
        )
        fee = (
            JournalFactory.fee(
                final_fill.fill_id,
                final_fill.modeled_commission,
                final_fill.occurred_at,
            )
            if final_fill.modeled_commission.amount > 0
            else None
        )
        journals = tuple(item for item in (purchase, fee) if item is not None)
        lineage_payload = {
            "run_id": run.run_id,
            "signal_id": command.signal.signal_id,
            "opportunity_id": command.opportunity.opportunity_id,
            "strategy_authority_decision_id": command.strategy_authority_decision_id,
            "trial_authorization_id": trial.authorization_id,
            "allocation_id": authorization.allocation_id,
            "risk_decision_id": authorization.risk_decision_id,
            "reservation_id": authorization.reservation_id,
            "order_id": final_order.order_id,
            "fill_id": final_fill.fill_id,
            "position_id": position.position_id if position else None,
            "execution_policy_hash": command.policy.canonical_hash,
            "data_evidence_hash": command.opportunity.canonical_hash,
        }
        lineage = SecuritiesExecutionLineage(
            lineage_id=deterministic_id("securities_execution_lineage", lineage_payload),
            run_id=run.run_id,
            signal_id=command.signal.signal_id,
            opportunity_id=command.opportunity.opportunity_id,
            strategy_authority_decision_id=command.strategy_authority_decision_id,
            trial_authorization_id=trial.authorization_id,
            allocation_id=authorization.allocation_id,
            risk_decision_id=authorization.risk_decision_id,
            reservation_id=authorization.reservation_id,
            order_id=final_order.order_id,
            fill_id=final_fill.fill_id,
            position_id=position.position_id if position else None,
            execution_policy_hash=command.policy.canonical_hash,
            data_evidence_hash=command.opportunity.canonical_hash,
            external_run_id=None,
            canonical_hash=ContentHash(content_digest(lineage_payload)),
        )
        self._repository.save_execution(
            order=final_order,
            fill=final_fill,
            position=position,
            lineage=lineage,
            journals=journals,
        )
        return SecuritiesPaperExecutionResult(
            authorization=authorization,
            order=final_order,
            fill=final_fill,
            position=position,
            lineage=lineage,
            journals=journals,
        )


@dataclass(frozen=True, slots=True)
class SecuritiesPaperFillIncrementResult:
    order: SecuritiesPaperOrder
    fill: SecuritiesFillEvidence
    position: SecuritiesPositionState | None
    journals: tuple[Journal, ...]


@dataclass(frozen=True, slots=True)
class ApplySecuritiesPaperFillIncrementCommand:
    """Validated, provider-free facts for one immutable PAPER fill increment."""

    campaign_id: str
    paper_trial_authorization_id: str
    strategy_id: str
    strategy_version: str
    strategy_configuration_hash: ContentHash
    order_id: str
    risk_reservation_id: str
    position_id: str | None
    instrument_id: str
    symbol: str
    action: SecuritiesOrderAction
    incremental_quantity: Quantity
    expected_cumulative_quantity: Quantity
    authorized_quantity: Quantity
    fill_price: Decimal
    fee: Money
    fill_at: datetime
    execution_source: SecuritiesExecutionSource
    source_evidence_id: str
    economic_identity_hash: ContentHash
    external_validated_fill_id: str | None
    internal_simulation_evidence_id: str | None
    execution_policy_hash: ContentHash
    idempotency_key: str
    canonical_input_hash: ContentHash

    def expected_hash(self) -> ContentHash:
        return ContentHash(
            content_digest(
                {
                    "campaign_id": self.campaign_id,
                    "paper_trial_authorization_id": self.paper_trial_authorization_id,
                    "strategy_id": self.strategy_id,
                    "strategy_version": self.strategy_version,
                    "strategy_configuration_hash": self.strategy_configuration_hash,
                    "order_id": self.order_id,
                    "risk_reservation_id": self.risk_reservation_id,
                    "position_id": self.position_id,
                    "instrument_id": self.instrument_id,
                    "symbol": self.symbol,
                    "action": self.action,
                    "incremental_quantity": self.incremental_quantity,
                    "expected_cumulative_quantity": self.expected_cumulative_quantity,
                    "authorized_quantity": self.authorized_quantity,
                    "fill_price": self.fill_price,
                    "fee": self.fee,
                    "fill_at": _aware(self.fill_at),
                    "execution_source": self.execution_source,
                    "source_evidence_id": self.source_evidence_id,
                    "economic_identity_hash": self.economic_identity_hash,
                    "external_validated_fill_id": self.external_validated_fill_id,
                    "internal_simulation_evidence_id": self.internal_simulation_evidence_id,
                    "execution_policy_hash": self.execution_policy_hash,
                    "idempotency_key": self.idempotency_key,
                }
            )
        )


class ApplySecuritiesPaperFillIncrement:
    """Shared authoritative accounting for one already-validated fill increment."""

    def __init__(self, repository: SecuritiesPaperRepository) -> None:
        self._repository = repository

    def execute(
        self, command: ApplySecuritiesPaperFillIncrementCommand
    ) -> SecuritiesPaperFillIncrementResult:
        if command.expected_hash() != command.canonical_input_hash:
            raise SecuritiesPaperApplicationError(
                "Securities fill increment canonical input mismatch"
            )
        if command.incremental_quantity.value <= 0 or command.fill_price <= 0:
            raise SecuritiesPaperApplicationError("Securities fill increment must be positive")
        if command.execution_source is SecuritiesExecutionSource.EXTERNAL_PAPER_VALIDATED_FILL:
            if command.external_validated_fill_id != command.source_evidence_id:
                raise SecuritiesPaperApplicationError("external fill source identity mismatch")
            if command.internal_simulation_evidence_id is not None:
                raise SecuritiesPaperApplicationError("external fill cannot have internal evidence")
        elif command.execution_source is SecuritiesExecutionSource.INTERNAL_SIMULATION:
            if command.internal_simulation_evidence_id != command.source_evidence_id:
                raise SecuritiesPaperApplicationError("internal fill source identity mismatch")
            if command.external_validated_fill_id is not None:
                raise SecuritiesPaperApplicationError("internal fill cannot have external evidence")
        else:  # defensive even though the enum is closed
            raise SecuritiesPaperApplicationError("unsupported Securities execution source")
        order = self._repository.order(command.order_id)
        if (
            order.instrument_id != command.instrument_id
            or order.action is not command.action
            or order.requested_quantity != command.authorized_quantity
        ):
            raise SecuritiesPaperApplicationError(
                "Securities fill increment Order lineage mismatch"
            )
        existing = self._repository.fill_by_source(
            execution_source=command.execution_source,
            source_evidence_id=command.source_evidence_id,
        )
        if existing is not None:
            if existing.economic_identity_hash != command.economic_identity_hash:
                raise SecuritiesPaperIdempotencyConflictError(
                    "Securities fill source replay has conflicting economics"
                )
            return SecuritiesPaperFillIncrementResult(
                order=order,
                fill=existing,
                position=(
                    self._repository.position(existing.position_id)
                    if existing.position_id is not None
                    else None
                ),
                journals=(),
            )
        duplicate = self._repository.fill_by_economic_identity(command.economic_identity_hash)
        if duplicate is not None:
            raise SecuritiesPaperIdempotencyConflictError("duplicate Securities economic fill")
        if order.state in (
            SecuritiesOrderState.FILLED,
            SecuritiesOrderState.CANCELED,
            SecuritiesOrderState.EXPIRED,
        ):
            raise SecuritiesPaperApplicationError("terminal Securities Order cannot receive a fill")
        if not order.decision_at < command.fill_at or command.fill_at < order.earliest_fill_at:
            raise SecuritiesPaperApplicationError("Securities fill timestamp is ineligible")
        history = self._repository.fills_for_order(order.order_id)
        applied = sum((item.quantity.value for item in history), Decimal())
        cumulative = applied + command.incremental_quantity.value
        if command.expected_cumulative_quantity.value != cumulative:
            raise SecuritiesPaperApplicationError("Securities fill cumulative quantity mismatch")
        if cumulative > command.authorized_quantity.value:
            raise SecuritiesPaperApplicationError(
                "Securities fill increment exceeds Order quantity"
            )
        position = (
            self._repository.position(command.position_id)
            if command.position_id is not None
            else None
        )
        increment = SecuritiesFillEvidence(
            fill_id=deterministic_id(
                "securities_paper_fill_increment", command.canonical_input_hash
            ),
            run_id=order.run_id,
            order_id=order.order_id,
            position_id=command.position_id,
            instrument_id=command.instrument_id,
            action=command.action,
            quantity=command.incremental_quantity,
            fill_price=command.fill_price,
            notional=Money(command.incremental_quantity.value * command.fill_price),
            modeled_spread=Money(Decimal()),
            modeled_slippage=Money(Decimal()),
            modeled_commission=command.fee,
            decision_at=order.decision_at,
            eligible_fill_at=order.earliest_fill_at,
            occurred_at=_aware(command.fill_at),
            bar_id=command.source_evidence_id,
            execution_policy_hash=command.execution_policy_hash,
            state=(
                SecuritiesFillState.FULL_FILL
                if cumulative == order.requested_quantity.value
                else SecuritiesFillState.PARTIAL_FILL
            ),
            reason_codes=(command.execution_source.value,),
            canonical_hash=command.canonical_input_hash,
            increment_ordinal=len(history) + 1,
            cumulative_quantity=command.expected_cumulative_quantity,
            remaining_quantity=Quantity(
                order.requested_quantity.value - cumulative, order.requested_quantity.increment
            ),
            execution_source=command.execution_source,
            source_evidence_id=command.source_evidence_id,
            economic_identity_hash=command.economic_identity_hash,
        )
        state = (
            SecuritiesOrderState.FILLED
            if cumulative == order.requested_quantity.value
            else SecuritiesOrderState.PARTIALLY_FILLED
        )
        updated_order = replace(
            order,
            filled_quantity=Quantity(cumulative, order.requested_quantity.increment),
            reserved_amount=Money(
                order.reserved_amount.amount
                * (order.requested_quantity.value - cumulative)
                / order.requested_quantity.value
            ),
            state=state,
            last_sequence=order.last_sequence + 1,
        )
        if order.action is SecuritiesOrderAction.BUY:
            updated_position = (
                position_from_buy_fill(
                    campaign_id=command.campaign_id, symbol=command.symbol, fill=increment
                )
                if position is None
                else add_buy_fill_to_position(position=position, fill=increment)
            )
            updated_order = replace(updated_order, position_id=updated_position.position_id)
            increment = replace(increment, position_id=updated_position.position_id)
            journals = tuple(
                item
                for item in (
                    JournalFactory.purchase(
                        increment.fill_id, increment.notional, increment.occurred_at
                    ),
                    JournalFactory.fee(
                        increment.fill_id, increment.modeled_commission, increment.occurred_at
                    )
                    if increment.modeled_commission.amount > 0
                    else None,
                )
                if item is not None
            )
        else:
            if position is None:
                raise SecuritiesPaperApplicationError("Securities sell increment requires Position")
            applied_exit = apply_sell_fill(position=position, fill=increment)
            updated_position = applied_exit.position
            journals = tuple(
                item
                for item in (
                    JournalFactory.sale(
                        increment.fill_id,
                        increment.notional,
                        applied_exit.allocated_cost_basis,
                        increment.occurred_at,
                    ),
                    JournalFactory.fee(
                        increment.fill_id, increment.modeled_commission, increment.occurred_at
                    )
                    if increment.modeled_commission.amount > 0
                    else None,
                )
                if item is not None
            )
        self._repository.append_fill_increment(
            order=updated_order,
            fill=increment,
            position=updated_position,
            journals=journals,
        )
        return SecuritiesPaperFillIncrementResult(
            order=updated_order,
            fill=increment,
            position=updated_position,
            journals=journals,
        )


class MarkSecuritiesPositions:
    def __init__(self, repository: SecuritiesPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        position_id: str,
        bar: SecuritiesBarEvidence,
        policy: SecuritiesExecutionPolicy,
        as_of: datetime,
    ) -> AppliedSecuritiesMark:
        existing = self._repository.mark(
            position_id=position_id,
            source_bar_id=bar.bar_id,
        )
        if existing is not None:
            return AppliedSecuritiesMark(
                position=self._repository.position(position_id),
                mark=existing,
            )
        applied = mark_position(
            position=self._repository.position(position_id),
            bar=bar,
            policy=policy,
            as_of=as_of,
        )
        self._repository.save_mark(applied)
        return applied


class ApplySecuritiesCorporateActions:
    def __init__(self, repository: SecuritiesPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        position_id: str,
        action: SecuritiesCorporateActionEvidence,
        applied_at: datetime,
        quantity_increment: Decimal,
        terminal_value: Decimal | None = None,
    ) -> AppliedCorporateAction:
        position = self._repository.position(position_id)
        run = self._repository.run(position.run_id)
        existing = self._repository.action_application(
            position_id=position_id,
            action_id=action.action_id,
        )
        if existing is not None:
            return AppliedCorporateAction(
                position=position,
                application=existing,
            )
        applied = apply_corporate_action(
            position=position,
            action=action,
            action_batch_id=run.data_batch_id,
            applied_at=applied_at,
            quantity_increment=quantity_increment,
            terminal_value=terminal_value,
        )
        journal = (
            JournalFactory.dividend(
                applied.application.application_id,
                applied.application.cash_entitlement,
                applied.application.applied_at,
            )
            if (
                applied.application.state is SecuritiesActionApplicationState.APPLIED
                and applied.application.cash_entitlement.amount > 0
            )
            else None
        )
        if journal is not None:
            applied = AppliedCorporateAction(
                position=applied.position,
                application=replace(
                    applied.application,
                    ledger_journal_id=journal.journal_id,
                ),
            )
        self._repository.save_action(applied, journal=journal)
        return applied


@dataclass(frozen=True, slots=True)
class ExitSecuritiesPositionCommand:
    position_id: str
    reason: SecuritiesExitReason
    decision_at: datetime
    next_bar: SecuritiesBarEvidence
    requested_quantity: Quantity
    policy: SecuritiesExecutionPolicy
    source_identity: str
    idempotency_key: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SecuritiesExitResult:
    decision: SecuritiesExitDecision
    order: SecuritiesPaperOrder
    fill: SecuritiesFillEvidence
    position: SecuritiesPositionState
    journals: tuple[Journal, ...]


class ExitSecuritiesPositions:
    """Risk-reducing exits remain available regardless of trial/opening state."""

    def __init__(self, repository: SecuritiesPaperRepository) -> None:
        self._repository = repository

    def execute(self, command: ExitSecuritiesPositionCommand) -> SecuritiesExitResult:
        position = self._repository.position(command.position_id)
        decision = build_exit_decision(
            position=position,
            reason=command.reason,
            decision_at=command.decision_at,
            next_bar=command.next_bar,
            source_identity=command.source_identity,
        )
        if command.requested_quantity.value > position.open_quantity.value:
            raise SecuritiesPaperApplicationError("exit quantity exceeds the Position")
        authorization_id = deterministic_id(
            "securities_exit_authorization",
            {
                "position_id": position.position_id,
                "exit_decision_id": decision.exit_decision_id,
            },
        )
        order = create_securities_order(
            run_id=position.run_id,
            instrument_id=position.instrument_id,
            action=SecuritiesOrderAction.SELL,
            quantity=command.requested_quantity,
            reserved_amount=Money(position.average_cost * command.requested_quantity.value),
            decision_at=command.decision_at,
            earliest_fill_at=command.next_bar.period_start,
            expires_at=command.expires_at,
            idempotency_key=command.idempotency_key,
            authorization_id=authorization_id,
            position_id=position.position_id,
        )
        applied_fill = execute_next_bar_order(
            order=order,
            bar=command.next_bar,
            policy=command.policy,
            # A SELL is bounded by Position quantity, not by new purchasing cash.
            available_cash=Money(0),
        )
        if applied_fill.fill.quantity.value == 0:
            raise SecuritiesPaperApplicationError("exit Order did not fill")
        applied_exit = apply_sell_fill(position=position, fill=applied_fill.fill)
        sale = JournalFactory.sale(
            applied_fill.fill.fill_id,
            applied_fill.fill.notional,
            applied_exit.allocated_cost_basis,
            applied_fill.fill.occurred_at,
        )
        fee = JournalFactory.fee(
            applied_fill.fill.fill_id,
            applied_fill.fill.modeled_commission,
            applied_fill.fill.occurred_at,
        )
        journals = (sale, fee)
        self._repository.save_exit(
            decision=decision,
            order=applied_fill.order,
            fill=applied_fill.fill,
            applied=applied_exit,
            journals=journals,
        )
        return SecuritiesExitResult(
            decision=decision,
            order=applied_fill.order,
            fill=applied_fill.fill,
            position=applied_exit.position,
            journals=journals,
        )


class BuildSecuritiesPaperConformance:
    def __init__(self, repository: SecuritiesPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        run: SecuritiesPaperRun,
        apex_events: tuple[tuple[str, str], ...],
        external: ExternalSecuritiesPaperRun,
    ) -> SecuritiesPaperConformanceReport:
        report = compare_paper_execution(
            run=run,
            apex_events=apex_events,
            external=external,
        )
        return self._repository.save_conformance(report, external)


def reconcile_securities_trial(
    trial: PaperTrialAuthorization,
    *,
    usage: PaperTrialUsage,
) -> PaperTrialAuthorization:
    """Suspend only new opening activity when any durable trial limit binds."""

    reasons: list[str] = []
    limits = trial.limits
    if usage.completed_cycles >= limits.maximum_paper_cycles:
        reasons.append("MAXIMUM_PAPER_CYCLES_REACHED")
    if usage.approved_opportunities >= limits.maximum_approved_opportunities:
        reasons.append("MAXIMUM_APPROVED_OPPORTUNITIES_REACHED")
    if usage.paper_orders >= limits.maximum_paper_orders:
        reasons.append("MAXIMUM_PAPER_ORDERS_REACHED")
    if usage.open_positions >= limits.maximum_simultaneous_open_positions:
        reasons.append("MAXIMUM_OPEN_POSITIONS_REACHED")
    if usage.cumulative_authorized_risk.amount >= limits.maximum_cumulative_authorized_risk.amount:
        reasons.append("MAXIMUM_CUMULATIVE_RISK_REACHED")
    if usage.cumulative_realized_result <= -limits.maximum_cumulative_realized_paper_loss.amount:
        reasons.append("MAXIMUM_REALIZED_LOSS_REACHED")
    if usage.paper_drawdown.amount >= limits.maximum_paper_drawdown.amount:
        reasons.append("MAXIMUM_DRAWDOWN_REACHED")
    if usage.maximum_capital_lockup_seconds >= limits.maximum_capital_lockup_seconds:
        reasons.append("MAXIMUM_CAPITAL_LOCKUP_REACHED")
    state = PaperTrialAuthorizationState.SUSPENDED_LIMIT if reasons else trial.state
    result_hash = ContentHash(
        content_digest(
            {
                "authorization_id": trial.authorization_id,
                "usage": usage,
                "state": state,
                "reasons": tuple(reasons),
            }
        )
    )
    return replace(
        trial,
        state=state,
        usage=usage,
        suspension_reason=",".join(reasons) if reasons else None,
        canonical_result_hash=result_hash,
    )


def _usage_with_status(
    usage: PaperTrialUsage,
    status: dict[str, object],
) -> PaperTrialUsage:
    """Merge command evidence with same-Campaign durable financial usage."""

    return replace(
        usage,
        completed_cycles=max(
            usage.completed_cycles,
            cast(int, status.get("completed_run_count", 0)),
        ),
        approved_opportunities=max(
            usage.approved_opportunities,
            cast(int, status.get("accepted_opportunity_count", 0)),
        ),
        paper_orders=max(
            usage.paper_orders,
            cast(int, status.get("order_count", 0)),
        ),
        open_positions=max(
            usage.open_positions,
            cast(int, status.get("open_position_count", 0)),
        ),
        active_strategy_risk=Money(
            max(
                usage.active_strategy_risk.amount,
                cast(Decimal, status.get("position_cost", Decimal(0))),
            )
        ),
        cumulative_authorized_risk=Money(
            max(
                usage.cumulative_authorized_risk.amount,
                cast(
                    Decimal,
                    status.get("cumulative_authorized_risk", Decimal(0)),
                ),
            )
        ),
    )


class InMemorySecuritiesPaperRepository:
    """Atomic-by-construction fixture repository for services and product demo."""

    def __init__(self, *, ledger: Ledger | None = None) -> None:
        self.ledger = ledger or Ledger()
        self.policies: dict[str, SecuritiesExecutionPolicy] = {}
        self.runs: dict[str, SecuritiesPaperRun] = {}
        self.run_idempotency: dict[str, tuple[ContentHash, str]] = {}
        self.opportunities: dict[str, SecuritiesPaperOpportunity] = {}
        self.authorizations: dict[str, SecuritiesPaperAuthorization] = {}
        self.orders: dict[str, SecuritiesPaperOrder] = {}
        self.fills: dict[str, SecuritiesFillEvidence] = {}
        self._positions: dict[str, SecuritiesPositionState] = {}
        self.lineages: dict[str, SecuritiesExecutionLineage] = {}
        self.marks: dict[str, AppliedSecuritiesMark] = {}
        self.actions: dict[str, AppliedCorporateAction] = {}
        self.exits: dict[str, SecuritiesExitDecision] = {}
        self.external_runs: dict[str, ExternalSecuritiesPaperRun] = {}
        self.conformance: dict[str, SecuritiesPaperConformanceReport] = {}

    def save_policy(self, policy: SecuritiesExecutionPolicy) -> SecuritiesExecutionPolicy:
        return _immutable_save(self.policies, policy.policy_id, policy)

    def save_run(self, run: SecuritiesPaperRun) -> SecuritiesPaperRun:
        identity = self.run_idempotency.get(run.idempotency_key)
        if identity is not None and identity[0] != run.canonical_input_hash:
            raise SecuritiesPaperIdempotencyConflictError(
                "Securities PAPER run idempotency conflict"
            )
        existing = self.runs.get(run.run_id)
        if existing is not None and existing.canonical_input_hash != run.canonical_input_hash:
            raise SecuritiesPaperIdempotencyConflictError("Securities PAPER run identity conflict")
        self.runs[run.run_id] = run
        self.run_idempotency[run.idempotency_key] = (
            run.canonical_input_hash,
            run.run_id,
        )
        return run

    def run(self, run_id: str) -> SecuritiesPaperRun:
        try:
            return self.runs[run_id]
        except KeyError as error:
            raise SecuritiesPaperApplicationError("Securities PAPER run was not found") from error

    def save_opportunity(
        self, opportunity: SecuritiesPaperOpportunity
    ) -> SecuritiesPaperOpportunity:
        inserted = opportunity.opportunity_id not in self.opportunities
        retained = _immutable_save(
            self.opportunities,
            opportunity.opportunity_id,
            opportunity,
        )
        if inserted:
            run = self.runs[opportunity.run_id]
            self.runs[run.run_id] = replace(
                run,
                opportunity_count=run.opportunity_count + 1,
            )
        return retained

    def save_authorization(
        self, authorization: SecuritiesPaperAuthorization
    ) -> SecuritiesPaperAuthorization:
        inserted = authorization.authorization_id not in self.authorizations
        retained = _immutable_save(
            self.authorizations,
            authorization.authorization_id,
            authorization,
        )
        if inserted and not authorization.accepted:
            opportunity = self.opportunities[authorization.opportunity_id]
            run = self.runs[opportunity.run_id]
            self.runs[run.run_id] = replace(
                run,
                rejected_opportunity_count=run.rejected_opportunity_count + 1,
            )
        return retained

    def save_pending_order(self, order: SecuritiesPaperOrder) -> SecuritiesPaperOrder:
        existing = self.orders.get(order.order_id)
        if existing is not None:
            if existing.canonical_hash != order.canonical_hash:
                raise SecuritiesPaperIdempotencyConflictError(
                    "Securities pending Order identity conflict"
                )
            return existing
        if order.state is not SecuritiesOrderState.READY or order.filled_quantity.value != 0:
            raise SecuritiesPaperApplicationError("pending Securities Order must be unfilled READY")
        self.orders[order.order_id] = order
        run = self.runs[order.run_id]
        self.runs[run.run_id] = replace(
            run,
            accepted_opportunity_count=run.accepted_opportunity_count + 1,
            order_count=run.order_count + 1,
        )
        return order

    def cancel_open_order(self, order_id: str) -> SecuritiesPaperOrder:
        order = self.order(order_id)
        if order.state is SecuritiesOrderState.CANCELED:
            return order
        if order.state not in {SecuritiesOrderState.READY, SecuritiesOrderState.PARTIALLY_FILLED}:
            raise SecuritiesPaperApplicationError("Securities PAPER Order cannot be canceled")
        canceled = replace(
            order,
            reserved_amount=Money(0),
            state=SecuritiesOrderState.CANCELED,
            last_sequence=order.last_sequence + 1,
        )
        self.orders[order_id] = canceled
        return canceled

    def save_execution(
        self,
        *,
        order: SecuritiesPaperOrder,
        fill: SecuritiesFillEvidence,
        position: SecuritiesPositionState | None,
        lineage: SecuritiesExecutionLineage,
        journals: tuple[Journal, ...],
    ) -> None:
        staged_ledger = Ledger()
        staged_ledger.post_many(self.ledger.journals)
        staged_ledger.post_many(journals)
        _validate_immutable(self.orders, order.order_id, order)
        _validate_immutable(self.fills, fill.fill_id, fill)
        _validate_immutable(self.lineages, lineage.lineage_id, lineage)
        if position is not None:
            _validate_immutable(self._positions, position.position_id, position)
        inserted = order.order_id not in self.orders
        self.ledger.post_many(journals)
        self.orders[order.order_id] = order
        self.fills[fill.fill_id] = fill
        self.lineages[lineage.lineage_id] = lineage
        if position is not None:
            self._positions[position.position_id] = position
        if inserted:
            run = self.runs[order.run_id]
            self.runs[run.run_id] = replace(
                run,
                accepted_opportunity_count=run.accepted_opportunity_count + 1,
                order_count=run.order_count + 1,
                fill_count=run.fill_count + int(fill.quantity.value > 0),
                position_count=run.position_count + int(position is not None),
            )

    def save_mark(self, applied: AppliedSecuritiesMark) -> None:
        _validate_immutable(self.marks, applied.mark.mark_id, applied)
        self.marks[applied.mark.mark_id] = applied
        self._positions[applied.position.position_id] = applied.position
        run = self.runs[applied.mark.run_id]
        self.runs[run.run_id] = replace(run, mark_count=run.mark_count + 1)

    def mark(
        self,
        *,
        position_id: str,
        source_bar_id: str,
    ) -> SecuritiesMarkEvidence | None:
        return next(
            (
                item.mark
                for item in self.marks.values()
                if item.mark.position_id == position_id and item.mark.source_bar_id == source_bar_id
            ),
            None,
        )

    def save_action(
        self,
        applied: AppliedCorporateAction,
        *,
        journal: Journal | None,
    ) -> None:
        _validate_immutable(
            self.actions,
            applied.application.application_id,
            applied,
        )
        if journal is not None:
            self.ledger.post(journal)
        self.actions[applied.application.application_id] = applied
        self._positions[applied.position.position_id] = applied.position
        run = self.runs[applied.position.run_id]
        self.runs[run.run_id] = replace(
            run,
            corporate_action_application_count=(run.corporate_action_application_count + 1),
        )

    def action_application(
        self,
        *,
        position_id: str,
        action_id: str,
    ) -> SecuritiesCorporateActionApplication | None:
        return next(
            (
                item.application
                for item in self.actions.values()
                if item.application.position_id == position_id
                and item.application.action_id == action_id
            ),
            None,
        )

    def save_exit(
        self,
        *,
        decision: SecuritiesExitDecision,
        order: SecuritiesPaperOrder,
        fill: SecuritiesFillEvidence,
        applied: AppliedSecuritiesExit,
        journals: tuple[Journal, ...],
    ) -> None:
        staged_ledger = Ledger()
        staged_ledger.post_many(self.ledger.journals)
        staged_ledger.post_many(journals)
        _validate_immutable(self.exits, decision.exit_decision_id, decision)
        _validate_immutable(self.orders, order.order_id, order)
        _validate_immutable(self.fills, fill.fill_id, fill)
        self.ledger.post_many(journals)
        self.exits[decision.exit_decision_id] = decision
        self.orders[order.order_id] = order
        self.fills[fill.fill_id] = fill
        self._positions[applied.position.position_id] = applied.position
        run = self.runs[order.run_id]
        self.runs[run.run_id] = replace(
            run,
            order_count=run.order_count + 1,
            fill_count=run.fill_count + 1,
            closed_position_count=run.closed_position_count
            + int(applied.position.state.value == "CLOSED"),
        )

    def save_conformance(
        self,
        report: SecuritiesPaperConformanceReport,
        external: ExternalSecuritiesPaperRun,
    ) -> SecuritiesPaperConformanceReport:
        _immutable_save(self.external_runs, external.external_run_id, external)
        return _immutable_save(self.conformance, report.report_id, report)

    def positions(self, campaign_id: str) -> tuple[SecuritiesPositionState, ...]:
        return tuple(
            sorted(
                (item for item in self._positions.values() if item.campaign_id == campaign_id),
                key=lambda item: item.position_id,
            )
        )

    def position(self, position_id: str) -> SecuritiesPositionState:
        try:
            return self._positions[position_id]
        except KeyError as error:
            raise SecuritiesPaperApplicationError("Securities Position was not found") from error

    def has_order(self, order_id: str) -> bool:
        return order_id in self.orders

    def order(self, order_id: str) -> SecuritiesPaperOrder:
        try:
            return self.orders[order_id]
        except KeyError as error:
            raise SecuritiesPaperApplicationError("Securities PAPER Order was not found") from error

    def fills_for_order(self, order_id: str) -> tuple[SecuritiesFillEvidence, ...]:
        return tuple(
            sorted(
                (item for item in self.fills.values() if item.order_id == order_id),
                key=lambda item: item.increment_ordinal,
            )
        )

    def fill_by_source(
        self,
        *,
        execution_source: SecuritiesExecutionSource,
        source_evidence_id: str,
    ) -> SecuritiesFillEvidence | None:
        return next(
            (
                item
                for item in self.fills.values()
                if item.execution_source is execution_source
                and item.source_evidence_id == source_evidence_id
            ),
            None,
        )

    def fill_by_economic_identity(
        self, economic_identity_hash: ContentHash
    ) -> SecuritiesFillEvidence | None:
        return next(
            (
                item
                for item in self.fills.values()
                if (item.economic_identity_hash or item.canonical_hash) == economic_identity_hash
            ),
            None,
        )

    def append_fill_increment(
        self,
        *,
        order: SecuritiesPaperOrder,
        fill: SecuritiesFillEvidence,
        position: SecuritiesPositionState | None,
        journals: tuple[Journal, ...],
    ) -> None:
        prior = self.orders.get(order.order_id)
        if prior is None:
            raise SecuritiesPaperApplicationError("Securities PAPER Order was not found")
        history = self.fills_for_order(order.order_id)
        if fill.fill_id in self.fills:
            if self.fills[fill.fill_id].canonical_hash != fill.canonical_hash:
                raise SecuritiesPaperIdempotencyConflictError("Securities fill identity conflict")
            return
        if any(
            (item.economic_identity_hash or item.canonical_hash)
            == (fill.economic_identity_hash or fill.canonical_hash)
            for item in history
        ):
            raise SecuritiesPaperIdempotencyConflictError("duplicate Securities economic fill")
        prior_quantity = sum((item.quantity.value for item in history), Decimal())
        if (
            fill.increment_ordinal != len(history) + 1
            or fill.cumulative_quantity is None
            or fill.cumulative_quantity.value != prior_quantity + fill.quantity.value
            or fill.cumulative_quantity.value > prior.requested_quantity.value
        ):
            raise SecuritiesPaperApplicationError("invalid Securities fill increment")
        staged = Ledger()
        staged.post_many(self.ledger.journals)
        staged.post_many(journals)
        self.ledger.post_many(journals)
        self.fills[fill.fill_id] = fill
        self.orders[order.order_id] = order
        if position is not None:
            self._positions[position.position_id] = position

    def status(self, campaign_id: str) -> dict[str, object]:
        positions = self.positions(campaign_id)
        metrics = self.ledger.metrics()
        return {
            "campaign_id": campaign_id,
            "paper_run_count": sum(item.campaign_id == campaign_id for item in self.runs.values()),
            "completed_run_count": sum(
                item.campaign_id == campaign_id
                and item.state
                in (
                    SecuritiesPaperRunState.COMPLETED,
                    SecuritiesPaperRunState.PARTIAL,
                    SecuritiesPaperRunState.FAILED,
                    SecuritiesPaperRunState.CANCELED,
                )
                for item in self.runs.values()
            ),
            "accepted_opportunity_count": sum(
                item.accepted
                and self.runs[self.opportunities[item.opportunity_id].run_id].campaign_id
                == campaign_id
                for item in self.authorizations.values()
            ),
            "order_count": sum(
                self.runs[item.run_id].campaign_id == campaign_id for item in self.orders.values()
            ),
            "fill_count": sum(
                self.runs[item.run_id].campaign_id == campaign_id for item in self.fills.values()
            ),
            "open_position_count": sum(item.open_quantity.value > 0 for item in positions),
            "closed_position_count": sum(item.open_quantity.value == 0 for item in positions),
            "cash": metrics.cash.amount,
            "position_cost": metrics.position_cost.amount,
            "cumulative_authorized_risk": sum(
                (
                    item.maximum_loss.amount
                    for item in self.authorizations.values()
                    if item.accepted
                    and self.runs[self.opportunities[item.opportunity_id].run_id].campaign_id
                    == campaign_id
                ),
                Decimal(0),
            ),
            "realized_pnl": metrics.realized_trading_pnl_before_fees,
            "dividend_income": metrics.dividend_income.amount,
            "fees": metrics.fees.amount,
            "net_pnl": metrics.net_trading_pnl,
        }


class InMemorySecuritiesPaperCampaignBoundary:
    def __init__(
        self,
        snapshots: tuple[CampaignSnapshot, ...],
        *,
        operating_states: dict[str, str] | None = None,
    ) -> None:
        self._snapshots = {item.campaign.campaign_id: item for item in snapshots}
        self._states = operating_states or {
            item.campaign.campaign_id: "RUNNING" for item in snapshots
        }

    def snapshot(self, campaign_id: str) -> CampaignSnapshot:
        try:
            return self._snapshots[campaign_id]
        except KeyError as error:
            raise SecuritiesPaperApplicationError("Campaign was not found") from error

    def operating_state(self, campaign_id: str) -> str:
        try:
            return self._states[campaign_id]
        except KeyError as error:
            raise SecuritiesPaperApplicationError("Campaign state was not found") from error

    def replace_snapshot(self, snapshot: CampaignSnapshot) -> None:
        self._snapshots[snapshot.campaign.campaign_id] = snapshot

    def set_operating_state(self, campaign_id: str, state: str) -> None:
        if state not in {"RUNNING", "PAUSED", "KILLED", "EXPIRED"}:
            raise SecuritiesPaperApplicationError("Campaign operating state is invalid")
        self._states[campaign_id] = state


def complete_run(
    run: SecuritiesPaperRun,
    *,
    repository: SecuritiesPaperRepository,
    completed_at: datetime,
) -> SecuritiesPaperRun:
    status = repository.status(run.campaign_id)
    cash = cast(Decimal, status["cash"])
    realized_pnl = cast(Decimal, status["realized_pnl"])
    dividend_income = cast(Decimal, status["dividend_income"])
    net_pnl = cast(Decimal, status["net_pnl"])
    result = {
        "run_id": run.run_id,
        "order_count": run.order_count,
        "fill_count": run.fill_count,
        "open_position_count": status["open_position_count"],
        "closed_position_count": run.closed_position_count,
        "cash": cash,
        "realized_pnl": realized_pnl,
        "dividend_income": dividend_income,
        "fees": status["fees"],
        "net_pnl": net_pnl,
    }
    completed = replace(
        run,
        state=SecuritiesPaperRunState.COMPLETED,
        ending_cash=Money(cash),
        gross_realized_pnl=parse_decimal(
            realized_pnl + dividend_income,
            allow_negative=True,
        ),
        net_realized_pnl=parse_decimal(net_pnl, allow_negative=True),
        completed_at=_aware(completed_at),
        canonical_result_hash=ContentHash(content_digest(result)),
    )
    return repository.save_run(completed)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SecuritiesPaperApplicationError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _validate_immutable[T](mapping: dict[str, T], key: str, value: T) -> None:
    existing = mapping.get(key)
    if existing is not None and existing != value:
        raise SecuritiesPaperIdempotencyConflictError(
            "immutable Securities PAPER evidence conflict"
        )


def _immutable_save[T](mapping: dict[str, T], key: str, value: T) -> T:
    _validate_immutable(mapping, key, value)
    mapping[key] = value
    return value
