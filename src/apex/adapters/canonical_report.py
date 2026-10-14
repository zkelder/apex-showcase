"""Canonical JSON serialization for deterministic simulation reports."""

from __future__ import annotations

from hashlib import sha256

from apex.application.interfaces import (
    InvariantRejectedResult,
    NoProposalResult,
    PartialFillCanceledResult,
    PartialFillCanceledSettledNoResult,
    PartialFillCanceledSettledVoidResult,
    PolicyRejectedResult,
    RunResult,
    UnfilledExpiredResult,
)
from apex.domain.values import ValueValidationError, canonical_json_bytes


class CanonicalReportError(TypeError):
    """A report contained a value without a deterministic JSON representation."""


class CanonicalReportRenderer:
    """Render reports as compact, sorted UTF-8 JSON with one trailing newline."""

    def render(self, report: object) -> bytes:
        try:
            return canonical_json_bytes(report) + b"\n"
        except ValueValidationError as error:
            raise CanonicalReportError(str(error)) from error


def report_document(result: RunResult) -> dict[str, object]:
    """Select the explicit schema for the terminal simulation outcome."""

    document: dict[str, object] = {
        "schema_version": "2",
        "run_id": result.run_id,
        "run_outcome": result.run_outcome,
        "fixture_id": result.fixture_id,
        "fixture_manifest_hash": result.fixture_manifest_hash.value,
        "engine_version": result.engine_version,
        "seed": result.seed,
        "logical_time": result.logical_time,
        "runtime_capability": result.runtime_capability,
        "execution_evidence": result.execution_evidence,
        "edge_evidence": result.edge_evidence,
        "calibration_evidence": result.calibration_evidence,
        "campaign": {
            "campaign_id": result.campaign.campaign_id,
            "policy_version": result.campaign.policy.policy_version_id,
            "contribution": result.campaign.contribution.amount,
            "authorized_campaign_loss": result.campaign.authorized_loss.amount,
        },
        "final_ledger_equity": result.metrics.ledger_equity.amount,
        "realized_trading_pnl_before_fees": result.metrics.realized_trading_pnl_before_fees,
        "fees": result.metrics.fees.amount,
        "net_trading_pnl": result.metrics.net_trading_pnl,
        "selected_instrument": result.information_set.instrument.instrument_id,
        "strategy_version": result.strategy_evaluation.strategy_version,
        "policy_version": result.campaign.policy.policy_version_id,
        "information_set_hash": result.information_set.content_hash.value,
        "information_set": result.information_set,
        "strategy_evaluation": result.strategy_evaluation,
        "ledger_journals": result.journals,
    }
    if isinstance(result, NoProposalResult):
        return document
    if isinstance(result, PolicyRejectedResult):
        document.update(
            {
                "proposal": result.proposal,
                "allocation": result.allocation,
                "policy_reason_codes": result.policy_reason_codes,
                "policy_explanation": result.policy_explanation,
            }
        )
        return document
    if isinstance(result, InvariantRejectedResult):
        risk_decision = result.risk_decision
        document.update(
            {
                "proposal": result.proposal,
                "allocation": result.allocation,
                "risk_decision": risk_decision,
                "invariant_reason_codes": result.invariant_reason_codes,
                "invariant_explanation": result.invariant_explanation,
                "freshness_evidence": {
                    "observation_source_id": risk_decision.observation_source_id,
                    "observation_available_at": risk_decision.observation_available_at,
                    "authorization_at": risk_decision.authorization_at,
                    "observed_age_seconds": risk_decision.observed_age_seconds,
                    "maximum_age_seconds": risk_decision.maximum_age_seconds,
                    "information_set_id": risk_decision.information_set_id,
                    "information_set_hash": risk_decision.information_set_hash,
                },
            }
        )
        return document
    if isinstance(result, UnfilledExpiredResult):
        document.update(
            {
                "proposal": result.proposal,
                "allocation": result.allocation,
                "risk_decision": result.risk_decision,
                "reservation_lifecycle": result.reservation_lifecycle,
                "initial_reserved_maximum_loss": (result.initial_reservation.maximum_loss),
                "final_reservation": result.final_reservation,
                "released_reservation_amount": result.released_reservation_amount,
                "risk_capacity_before_order": result.risk_capacity_before_order,
                "risk_capacity_while_open": result.risk_capacity_while_open,
                "risk_capacity_after_expiration": (result.risk_capacity_after_expiration),
                "order": result.order,
                "ordered_quantity": result.order.quantity,
                "filled_quantity": result.order.filled_quantity,
                "remaining_quantity": result.order.remaining_quantity,
                "order_limit_price": result.order.limit_price,
                "synthetic_executable_ask": result.order.executable_ask,
                "execution_events": result.execution_events,
                "final_cash": result.metrics.cash,
            }
        )
        return document
    if isinstance(result, PartialFillCanceledSettledVoidResult):
        document.update(
            {
                "campaign_policy": result.campaign.policy,
                "proposal": result.proposal,
                "allocation": result.allocation,
                "risk_decision": result.risk_decision,
                "reservation_lifecycle": result.reservation_lifecycle,
                "initial_reserved_maximum_loss": result.initial_reservation.maximum_loss,
                "reservation_after_partial_fill": result.reservation_after_partial_fill,
                "unfilled_reservation_before_cancellation": (
                    result.reservation_after_partial_fill.maximum_loss
                ),
                "final_reservation": result.final_reservation,
                "released_at_cancellation": result.released_at_cancellation,
                "risk_capacity_before_order": result.risk_capacity_before_order,
                "risk_capacity_while_fully_reserved": (result.risk_capacity_while_fully_reserved),
                "risk_capacity_after_partial_fill": result.risk_capacity_after_partial_fill,
                "risk_capacity_after_cancellation": result.risk_capacity_after_cancellation,
                "risk_capacity_after_settlement": result.risk_capacity_after_settlement,
                "order": result.order,
                "ordered_quantity": result.order.quantity,
                "filled_quantity": result.order.filled_quantity,
                "remaining_quantity": result.order.remaining_quantity,
                "canceled_quantity": result.order.remaining_quantity,
                "order_limit_price": result.order.limit_price,
                "synthetic_executable_ask": result.order.executable_ask,
                "execution_events": result.execution_events,
                "pre_settlement_position": result.pre_settlement_position,
                "position_risk_before_settlement": (
                    result.pre_settlement_position.maximum_remaining_loss
                ),
                "active_risk_after_partial_fill": result.active_risk_after_partial_fill,
                "active_risk_after_cancellation": result.active_risk_after_cancellation,
                "contract_rule_version": result.contract_rule,
                "settlement_file_hash": result.settlement_file_hash,
                "settlement_fact": result.settlement_fact,
                "outcome_classification": result.settlement_fact.outcome_classification,
                "settlement_result": result.settlement,
                "settlement_value": result.settlement_fact.settlement_value,
                "settlement_cash": result.settlement.settlement_cash,
                "realized_contract_pnl": result.settlement.realized_pnl,
                "execution_fees": result.metrics.fees,
                "position_risk_released_at_settlement": (
                    result.position_risk_released_at_settlement
                ),
                "settled_position": result.settled_position,
                "final_position_risk": result.settled_position.maximum_remaining_loss,
                "total_active_campaign_risk": result.final_active_campaign_risk,
                "final_cash": result.metrics.cash,
                "position_cost": result.metrics.position_cost,
                "position_valuation_basis": "FINAL_RULE_SETTLEMENT",
                "fee_accounting_basis": "EXPENSED_SEPARATELY",
                "mark_evidence": "NONE",
            }
        )
        return document
    if isinstance(result, PartialFillCanceledSettledNoResult):
        document.update(
            {
                "proposal": result.proposal,
                "allocation": result.allocation,
                "risk_decision": result.risk_decision,
                "reservation_lifecycle": result.reservation_lifecycle,
                "initial_reserved_maximum_loss": result.initial_reservation.maximum_loss,
                "reservation_after_partial_fill": result.reservation_after_partial_fill,
                "unfilled_reservation_before_cancellation": (
                    result.reservation_after_partial_fill.maximum_loss
                ),
                "final_reservation": result.final_reservation,
                "released_at_cancellation": result.released_at_cancellation,
                "risk_capacity_before_order": result.risk_capacity_before_order,
                "risk_capacity_while_fully_reserved": (result.risk_capacity_while_fully_reserved),
                "risk_capacity_after_partial_fill": result.risk_capacity_after_partial_fill,
                "risk_capacity_after_cancellation": result.risk_capacity_after_cancellation,
                "risk_capacity_after_settlement": result.risk_capacity_after_settlement,
                "order": result.order,
                "ordered_quantity": result.order.quantity,
                "filled_quantity": result.order.filled_quantity,
                "remaining_quantity": result.order.remaining_quantity,
                "order_limit_price": result.order.limit_price,
                "synthetic_executable_ask": result.order.executable_ask,
                "execution_events": result.execution_events,
                "pre_settlement_position": result.pre_settlement_position,
                "position_risk_before_settlement": (
                    result.pre_settlement_position.maximum_remaining_loss
                ),
                "active_risk_after_partial_fill": result.active_risk_after_partial_fill,
                "active_risk_after_cancellation": result.active_risk_after_cancellation,
                "contract_rule_version": result.contract_rule,
                "settlement_file_hash": result.settlement_file_hash,
                "settlement_fact": result.settlement_fact,
                "settlement_result": result.settlement,
                "settlement_value": result.settlement_fact.settlement_value,
                "settlement_cash": result.settlement.settlement_cash,
                "realized_settlement_pnl": result.settlement.realized_pnl,
                "position_risk_released_at_settlement": (
                    result.position_risk_released_at_settlement
                ),
                "settled_position": result.settled_position,
                "final_position_risk": result.settled_position.maximum_remaining_loss,
                "total_active_campaign_risk": result.final_active_campaign_risk,
                "final_cash": result.metrics.cash,
                "position_cost": result.metrics.position_cost,
                "position_valuation_basis": "FINAL_RULE_SETTLEMENT",
                "fee_accounting_basis": "EXPENSED_SEPARATELY",
                "mark_evidence": "NONE",
            }
        )
        return document
    if isinstance(result, PartialFillCanceledResult):
        document.update(
            {
                "proposal": result.proposal,
                "allocation": result.allocation,
                "risk_decision": result.risk_decision,
                "reservation_lifecycle": result.reservation_lifecycle,
                "initial_reserved_maximum_loss": result.initial_reservation.maximum_loss,
                "reservation_after_partial_fill": (result.reservation_after_partial_fill),
                "unfilled_reservation_before_cancellation": (
                    result.reservation_after_partial_fill.maximum_loss
                ),
                "final_reservation": result.final_reservation,
                "released_at_cancellation": result.released_at_cancellation,
                "risk_capacity_before_order": result.risk_capacity_before_order,
                "risk_capacity_while_fully_reserved": (result.risk_capacity_while_fully_reserved),
                "risk_capacity_after_partial_fill": (result.risk_capacity_after_partial_fill),
                "risk_capacity_after_cancellation": (result.risk_capacity_after_cancellation),
                "order": result.order,
                "ordered_quantity": result.order.quantity,
                "filled_quantity": result.order.filled_quantity,
                "remaining_quantity": result.order.remaining_quantity,
                "order_limit_price": result.order.limit_price,
                "synthetic_executable_ask": result.order.executable_ask,
                "execution_events": result.execution_events,
                "position": result.position,
                "position_maximum_remaining_loss": (result.position.maximum_remaining_loss),
                "active_risk_after_partial_fill": result.active_risk_after_partial_fill,
                "total_active_campaign_risk": result.final_active_campaign_risk,
                "final_cash": result.metrics.cash,
                "position_cost": result.metrics.position_cost,
                "realized_settlement_pnl": (result.metrics.realized_trading_pnl_before_fees),
                "position_valuation_basis": "CARRIED_AT_COST",
                "fee_accounting_basis": "EXPENSED_SEPARATELY",
                "mark_evidence": "NONE",
            }
        )
        return document

    proposal = result.strategy_evaluation.proposal
    if proposal is None:
        raise CanonicalReportError("a settled-trade result must contain its proposal")
    document.update(
        {
            "proposal": proposal,
            "allocation": result.allocation,
            "risk_decision": result.risk_decision,
            "reservation": result.reservation,
            "order": result.order,
            "execution_events": result.execution_events,
            "settlement": result.settlement,
            "position": result.position,
        }
    )
    return document


def report_sha256(report_bytes: bytes) -> str:
    """Return the stable digest printed alongside, never inside, a report."""

    return sha256(report_bytes).hexdigest()
