"""Pure reconstruction helpers for durable unfilled-expiration rows."""

from __future__ import annotations

from datetime import datetime

from apex.domain.decisions import OpportunityProposal, RestingLimitProposal
from apex.domain.execution import ExpiringLimitOrder, Order
from apex.domain.values import Price


def restore_resting_limit_proposal(
    base: OpportunityProposal,
    *,
    limit_price: Price,
    order_expires_at: datetime,
) -> RestingLimitProposal:
    return RestingLimitProposal(
        proposal_id=base.proposal_id,
        instrument_id=base.instrument_id,
        rule_version_id=base.rule_version_id,
        information_set_id=base.information_set_id,
        strategy_version=base.strategy_version,
        side=base.side,
        executable_ask=base.executable_ask,
        fee_model_version=base.fee_model_version,
        fee_per_unit=base.fee_per_unit,
        expected_net_value_per_unit=base.expected_net_value_per_unit,
        maximum_loss_per_unit=base.maximum_loss_per_unit,
        requested_risk_fraction=base.requested_risk_fraction,
        expires_at=base.expires_at,
        fingerprint=base.fingerprint,
        limit_price=limit_price,
        order_expires_at=order_expires_at,
    )


def restore_expiring_limit_order(
    base: Order,
    *,
    executable_ask: Price,
    expires_at: datetime,
) -> ExpiringLimitOrder:
    return ExpiringLimitOrder(
        order_id=base.order_id,
        client_order_id=base.client_order_id,
        risk_decision_id=base.risk_decision_id,
        proposal_id=base.proposal_id,
        exact_order_fingerprint=base.exact_order_fingerprint,
        side=base.side,
        limit_price=base.limit_price,
        quantity=base.quantity,
        filled_quantity=base.filled_quantity,
        reserved_maximum_loss=base.reserved_maximum_loss,
        state=base.state,
        last_execution_sequence=base.last_execution_sequence,
        executable_ask=executable_ask,
        expires_at=expires_at,
    )
