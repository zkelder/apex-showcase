"""Read-only venue projections into shared PAPER attribution source snapshots."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.cross_venue_allocation_postgres import (
    PostgresCrossVenueAllocationRepository,
)
from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
from apex.adapters.kalshi_operating_postgres import PostgresKalshiOperatingRepository
from apex.adapters.postgres import external_paper_instruction
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.domain.performance_attribution import (
    AttributionSourceSnapshot,
    PaperEvidenceClass,
    PaperVenue,
)
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.values import content_digest


def _seal(source: AttributionSourceSnapshot) -> AttributionSourceSnapshot:
    blank = replace(source, source_hash="")
    return replace(source, source_hash=content_digest(blank))


class RetainedPerformanceAttributionSource:
    """Project retained local evidence only; this adapter has no provider client."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._allocation = PostgresCrossVenueAllocationRepository(engine)
        self._shared = PostgresSharedOpportunityRepository(engine)
        self._crypto = PostgresCryptoPaperRepository(engine)
        self._kalshi = PostgresKalshiOperatingRepository(engine)
        self._external = PostgresExternalPaperRepository(engine)

    def snapshots(
        self, evidence_class: PaperEvidenceClass
    ) -> tuple[AttributionSourceSnapshot, ...]:
        if evidence_class is not PaperEvidenceClass.GENUINE_PAPER:
            return ()
        values: list[AttributionSourceSnapshot] = []
        for decision in self._allocation.decisions():
            for proposal in self._allocation.proposals(decision.decision_id):
                projection = self._shared.projection(proposal.source_projection_id)
                if projection is None:
                    continue
                if proposal.venue is SharedVenue.CRYPTO:
                    values.append(self._crypto_snapshot(decision, proposal, projection))
                elif proposal.venue is SharedVenue.KALSHI:
                    values.append(self._kalshi_snapshot(decision, proposal, projection))
        with self._engine.connect() as connection:
            instruction_ids = tuple(
                connection.execute(
                    sa.select(external_paper_instruction.c.instruction_id).order_by(
                        external_paper_instruction.c.instruction_id
                    )
                ).scalars()
            )
        values.extend(
            snapshot
            for instruction_id in instruction_ids
            if (snapshot := self._alpaca_snapshot(str(instruction_id))) is not None
        )
        return tuple(sorted(values, key=lambda item: item.source_id))

    def snapshot(
        self, source_id: str, evidence_class: PaperEvidenceClass
    ) -> AttributionSourceSnapshot | None:
        return next(
            (item for item in self.snapshots(evidence_class) if item.source_id == source_id),
            None,
        )

    def _crypto_snapshot(
        self, decision: Any, proposal: Any, projection: Any
    ) -> AttributionSourceSnapshot:
        state = self._crypto.state(proposal.paper_account_id)
        authorization = (
            None
            if state is None
            else next(
                (
                    item
                    for item in state.authorizations
                    if item.projection_id == proposal.source_projection_id
                ),
                None,
            )
        )
        intent = (
            None
            if state is None or authorization is None
            else next(
                (
                    item
                    for item in state.intents
                    if item.authorization_id == authorization.authorization_id
                ),
                None,
            )
        )
        order = (
            None
            if state is None or intent is None
            else next((item for item in state.orders if item.intent_id == intent.intent_id), None)
        )
        fills = (
            ()
            if state is None or order is None
            else tuple(item for item in state.fills if item.order_id == order.order_id)
        )
        reservation = (
            None
            if state is None or order is None
            else next(
                (item for item in state.reservations if item.order_id == order.order_id), None
            )
        )
        position = (
            None
            if state is None
            else next(
                (
                    item
                    for item in state.positions
                    if item.product_id == projection.product_or_market_id
                ),
                None,
            )
        )
        reconciliation = (
            None
            if state is None
            else next(
                (
                    item
                    for item in reversed(state.reconciliations)
                    if item.order_id in {None, None if order is None else order.order_id}
                ),
                None,
            )
        )
        filled_quantity = sum((item.base_quantity for item in fills), Decimal(0)) if fills else None
        filled_notional = (
            sum((item.quote_quantity for item in fills), Decimal(0)) if fills else None
        )
        realized_fees = sum((item.fee for item in fills), Decimal(0)) if fills else None
        realized_slippage = sum((item.slippage for item in fills), Decimal(0)) if fills else None
        first_fill = min((item.event_time for item in fills), default=None)
        last_fill = max((item.event_time for item in fills), default=None)
        return _seal(
            AttributionSourceSnapshot(
                source_id=proposal.proposal_id,
                source_hash=proposal.canonical_hash,
                venue=PaperVenue.CRYPTO_INTERNAL_PAPER,
                evidence_class=PaperEvidenceClass.GENUINE_PAPER,
                strategy_or_method_id=projection.strategy_or_method_id,
                strategy_or_method_version="RETAINED_SOURCE_VERSION",
                instrument_id=projection.instrument_id,
                side=proposal.side,
                source_opportunity_id=projection.source_record_id,
                shared_projection_id=projection.projection_id,
                allocation_decision_id=decision.decision_id,
                paper_trial_proposal_id=proposal.proposal_id,
                authorization_id=None if authorization is None else authorization.authorization_id,
                order_id=None if order is None else order.order_id,
                reservation_id=None if reservation is None else reservation.reservation_id,
                reservation_original_amount=None
                if reservation is None
                else reservation.original_amount,
                reservation_current_amount=None
                if reservation is None
                else reservation.remaining_amount,
                reservation_consumed_amount=None
                if reservation is None
                else reservation.consumed_amount,
                fill_ids=tuple(item.fill_id for item in fills),
                position_id=None if position is None else position.position_id,
                settlement_id=None,
                resolution_fact_id=None,
                context_at_consideration_id=decision.context_snapshot_id,
                context_at_execution_id=None,
                consideration_context=decision.context_state.value,
                execution_context=None,
                intended_quantity=None if order is None else order.target_base_quantity,
                filled_quantity=filled_quantity,
                intended_notional=proposal.maximum_notional,
                filled_notional=filled_notional,
                expected_gross_return=projection.expected_gross_return,
                realized_gross_return=None,
                expected_net_return=projection.expected_net_return,
                realized_net_return=None if position is None else position.realized_paper_pnl,
                estimated_fees=projection.modeled_fees,
                realized_fees=realized_fees,
                estimated_slippage=projection.modeled_slippage,
                realized_slippage=realized_slippage,
                modeled_maximum_loss=proposal.maximum_modeled_risk,
                realized_loss=None
                if position is None
                else max(Decimal(0), -position.realized_paper_pnl),
                open_exposure=None if position is None else position.cumulative_cost,
                unrealized_paper_pnl=None,
                expected_lockup_seconds=None
                if projection.estimated_lockup_days is None
                else projection.estimated_lockup_days * 86400,
                actual_lockup_seconds=None,
                considered_at=decision.decided_at,
                first_fill_at=first_fill,
                completed_at=last_fill
                if order is not None and order.remaining_base_quantity == 0
                else None,
                expires_at=proposal.expires_at,
                position_state=None if position is None else position.state,
                reconciliation_status=None
                if reconciliation is None
                else reconciliation.status.value,
                reconciliation_issues=()
                if reconciliation is None
                else reconciliation.discrepancies,
                opportunity_expiration_behavior=None if order is None else order.state.value,
                limitations=(
                    "CRYPTO_UNREALIZED_PNL_UNAVAILABLE_WITHOUT_RETAINED_MARK",
                    "NO_PROVIDER_REQUEST",
                ),
            )
        )

    def _kalshi_snapshot(
        self, decision: Any, proposal: Any, projection: Any
    ) -> AttributionSourceSnapshot:
        state = self._kalshi.state(proposal.paper_account_id)
        authorization = (
            None
            if state is None
            else next(
                (
                    item
                    for item in state.authorizations
                    if item.opportunity_id == projection.source_record_id
                ),
                None,
            )
        )
        order = (
            None
            if state is None or authorization is None
            else next(
                (
                    item
                    for item in state.orders
                    if item.authorization_id == authorization.authorization_id
                ),
                None,
            )
        )
        fills = (
            ()
            if state is None or order is None
            else tuple(item for item in state.fills if item.order_id == order.order_id)
        )
        reservation = (
            None
            if state is None or order is None
            else next(
                (item for item in state.reservations if item.order_id == order.order_id), None
            )
        )
        position = (
            None
            if state is None or order is None
            else next(
                (item for item in state.positions if item.market_ticker == order.market_ticker),
                None,
            )
        )
        settlement = (
            None
            if state is None or position is None
            else next(
                (item for item in state.settlements if item.position_id == position.position_id),
                None,
            )
        )
        resolution = (
            None
            if state is None or settlement is None
            else next(
                (
                    item
                    for item in state.resolutions
                    if item.resolution_fact_id == settlement.resolution_fact_id
                ),
                None,
            )
        )
        reconciliation = (
            None if state is None or not state.reconciliations else state.reconciliations[-1]
        )
        intended = None if order is None else Decimal(order.contracts)
        filled = None if order is None else Decimal(order.filled_contracts)
        first_fill = min((item.evidence_observed_at for item in fills), default=None)
        return _seal(
            AttributionSourceSnapshot(
                source_id=proposal.proposal_id,
                source_hash=proposal.canonical_hash,
                venue=PaperVenue.KALSHI_INTERNAL_PAPER,
                evidence_class=PaperEvidenceClass.GENUINE_PAPER,
                strategy_or_method_id=projection.strategy_or_method_id,
                strategy_or_method_version="RETAINED_SOURCE_VERSION",
                instrument_id=projection.instrument_id,
                side=None if order is None else order.side.value,
                source_opportunity_id=projection.source_record_id,
                shared_projection_id=projection.projection_id,
                allocation_decision_id=decision.decision_id,
                paper_trial_proposal_id=proposal.proposal_id,
                authorization_id=None if authorization is None else authorization.authorization_id,
                order_id=None if order is None else order.order_id,
                reservation_id=None if reservation is None else reservation.reservation_id,
                reservation_original_amount=None
                if reservation is None
                else reservation.original_amount,
                reservation_current_amount=None
                if reservation is None
                else reservation.current_amount,
                reservation_consumed_amount=None,
                fill_ids=tuple(item.fill_id for item in fills),
                position_id=None if position is None else position.position_id,
                settlement_id=None if settlement is None else settlement.settlement_id,
                resolution_fact_id=None if resolution is None else resolution.resolution_fact_id,
                context_at_consideration_id=decision.context_snapshot_id,
                context_at_execution_id=None,
                consideration_context=decision.context_state.value,
                execution_context=None,
                intended_quantity=intended,
                filled_quantity=filled,
                intended_notional=proposal.maximum_notional,
                filled_notional=None
                if not fills
                else sum((item.price * item.contracts for item in fills), Decimal(0)),
                expected_gross_return=projection.expected_gross_return,
                realized_gross_return=None if settlement is None else settlement.gross_payout,
                expected_net_return=projection.expected_net_return,
                realized_net_return=None if settlement is None else settlement.realized_paper_pnl,
                estimated_fees=projection.modeled_fees,
                realized_fees=None if not fills else sum((item.fee for item in fills), Decimal(0)),
                estimated_slippage=projection.modeled_slippage,
                realized_slippage=None,
                modeled_maximum_loss=proposal.maximum_modeled_risk,
                realized_loss=None
                if settlement is None
                else max(Decimal(0), -settlement.realized_paper_pnl),
                open_exposure=None
                if position is None or position.state.value != "OPEN"
                else position.cost_basis,
                unrealized_paper_pnl=None,
                expected_lockup_seconds=None
                if projection.estimated_lockup_days is None
                else projection.estimated_lockup_days * 86400,
                actual_lockup_seconds=None
                if position is None or position.settled_at is None
                else int((position.settled_at - position.opened_at).total_seconds()),
                considered_at=decision.decided_at,
                first_fill_at=first_fill,
                completed_at=None if settlement is None else settlement.settled_at,
                expires_at=proposal.expires_at,
                position_state=None if position is None else position.state.value,
                reconciliation_status=None
                if reconciliation is None
                else reconciliation.status.value,
                reconciliation_issues=() if reconciliation is None else reconciliation.issues,
                opportunity_expiration_behavior=None if order is None else order.state.value,
                limitations=(
                    "VISIBLE_LIQUIDITY_WAS_EVIDENCE_NOT_EXECUTABLE_QUOTE",
                    "NO_PROVIDER_REQUEST",
                ),
            )
        )

    def _alpaca_snapshot(self, instruction_id: str) -> AttributionSourceSnapshot | None:
        instruction = self._external.instruction(instruction_id)
        if instruction is None:
            return None
        fills = self._external.validated_fills_for_instruction(instruction_id)
        valid = tuple(item for item in fills if item.state.value in {"OBSERVED", "APPLIED"})
        reconciliations = self._external.reconciliations_for_instruction(instruction_id)
        reconciliation = reconciliations[-1] if reconciliations else None
        filled_quantity = max((item.cumulative_quantity for item in valid), default=None)
        filled_notional = (
            None
            if not valid
            else sum((item.newly_observed_quantity * item.fill_price for item in valid), Decimal(0))
        )
        realized_fees = None if not valid else sum((item.fee for item in valid), Decimal(0))
        first_fill = min((item.occurred_at for item in valid), default=None)
        status = None if reconciliation is None else reconciliation.classification.value
        issues = () if reconciliation is None or status == "RECONCILED" else (str(status),)
        return _seal(
            AttributionSourceSnapshot(
                source_id=instruction.instruction_id,
                source_hash=instruction.canonical_hash,
                venue=PaperVenue.ALPACA_EXTERNAL_PAPER,
                evidence_class=PaperEvidenceClass.GENUINE_PAPER,
                strategy_or_method_id="ALPACA_EXTERNAL_PAPER_LONG_ONLY_V1",
                strategy_or_method_version="1",
                instrument_id=instruction.symbol,
                side=instruction.side,
                source_opportunity_id=None,
                shared_projection_id=None,
                allocation_decision_id=None,
                paper_trial_proposal_id=None,
                authorization_id=instruction.authorization_id,
                order_id=instruction.broker_order_id,
                reservation_id=None,
                reservation_original_amount=None,
                reservation_current_amount=None,
                reservation_consumed_amount=None,
                fill_ids=tuple(item.validated_fill_id for item in valid),
                position_id=None,
                settlement_id=None,
                resolution_fact_id=None,
                context_at_consideration_id=None,
                context_at_execution_id=None,
                consideration_context=None,
                execution_context=None,
                intended_quantity=instruction.quantity,
                filled_quantity=filled_quantity,
                intended_notional=instruction.maximum_notional,
                filled_notional=filled_notional,
                expected_gross_return=None,
                realized_gross_return=None,
                expected_net_return=None,
                realized_net_return=None,
                estimated_fees=None,
                realized_fees=realized_fees,
                estimated_slippage=None,
                realized_slippage=None,
                modeled_maximum_loss=instruction.maximum_notional,
                realized_loss=None,
                open_exposure=filled_notional,
                unrealized_paper_pnl=None,
                expected_lockup_seconds=None,
                actual_lockup_seconds=None,
                considered_at=None,
                first_fill_at=first_fill,
                completed_at=max((item.occurred_at for item in valid), default=None),
                expires_at=instruction.expires_at,
                position_state=instruction.state.value,
                reconciliation_status=status,
                reconciliation_issues=issues,
                opportunity_expiration_behavior=instruction.state.value,
                limitations=(
                    "EXTERNAL_PAPER_EVIDENCE_NOT_APEX_INTERNAL_FINANCIAL_TRUTH",
                    "NO_PROVIDER_CONTACT_DURING_ATTRIBUTION",
                    "REALIZED_PERFORMANCE_UNAVAILABLE_WITHOUT_VALIDATED_EXIT_AND_ACCOUNTING",
                ),
            )
        )
