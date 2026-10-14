"""Deterministic strategy, allocation, and pure risk authorization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from apex.domain.campaign import (
    OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS,
    CampaignPolicy,
    CampaignSnapshot,
    OperationalInvariants,
)
from apex.domain.market import InformationSet, ReferenceEstimate
from apex.domain.values import (
    ContentHash,
    Money,
    Price,
    Quantity,
    RiskFraction,
    ValueValidationError,
    canonical_timestamp,
    content_digest,
    deterministic_id,
    floor_to_increment,
    parse_decimal,
)


class Side(StrEnum):
    YES = "YES"
    NO = "NO"


class StrategyOutcome(StrEnum):
    PROPOSAL = "PROPOSAL"
    NO_PROPOSAL = "NO_PROPOSAL"


POLICY_MAX_CAPITAL_LOCKUP_EXCEEDED = "POLICY_MAX_CAPITAL_LOCKUP_EXCEEDED"
INVARIANT_MARKET_DATA_STALE = "INVARIANT_MARKET_DATA_STALE"


def capital_lockup_policy_explanation(
    capital_lockup_seconds: int,
    maximum_seconds: int,
) -> str:
    return (
        f"Expected capital lockup of {capital_lockup_seconds} seconds exceeds "
        f"campaign policy maximum of {maximum_seconds} seconds."
    )


def expected_capital_lockup_seconds(
    decision_at: datetime,
    expected_settlement_at: datetime,
) -> int:
    if (
        decision_at.tzinfo is None
        or decision_at.utcoffset() is None
        or expected_settlement_at.tzinfo is None
        or expected_settlement_at.utcoffset() is None
    ):
        raise ValueValidationError("capital-lockup timestamps must be timezone-aware")
    lockup = expected_settlement_at.astimezone(UTC) - decision_at.astimezone(UTC)
    if lockup.days < 0 or lockup.microseconds:
        raise ValueValidationError(
            "expected settlement must be a whole-second time after the decision"
        )
    return lockup.days * 86_400 + lockup.seconds


def market_data_stale_explanation(
    *,
    observation_source_id: str,
    observation_available_at: datetime,
    authorization_at: datetime,
    observed_age_seconds: int,
    maximum_age_seconds: int,
    information_set_id: str,
) -> str:
    return (
        f"Market observation {observation_source_id} available at "
        f"{canonical_timestamp(observation_available_at)} was {observed_age_seconds} seconds "
        f"old at authorization {canonical_timestamp(authorization_at)}, exceeding the sealed "
        f"operational maximum of {maximum_age_seconds} seconds for information set "
        f"{information_set_id}."
    )


@dataclass(frozen=True, slots=True)
class OpportunityProposal:
    proposal_id: str
    instrument_id: str
    rule_version_id: str
    information_set_id: str
    strategy_version: str
    side: Side
    executable_ask: Price
    fee_model_version: str
    fee_per_unit: Money
    expected_net_value_per_unit: Decimal
    maximum_loss_per_unit: Money
    requested_risk_fraction: RiskFraction
    expires_at: datetime
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RestingLimitProposal(OpportunityProposal):
    limit_price: Price
    order_expires_at: datetime

    def __post_init__(self) -> None:
        if self.side is not Side.YES:
            raise ValueValidationError("the resting-limit slice supports YES orders only")
        if self.limit_price.tick != self.executable_ask.tick:
            raise ValueValidationError("resting limit and executable ask ticks must match")
        if self.limit_price.value >= self.executable_ask.value:
            raise ValueValidationError("resting YES limit must be below the executable ask")
        if self.order_expires_at.tzinfo is None or self.order_expires_at.utcoffset() is None:
            raise ValueValidationError("resting order expiration must be timezone-aware")
        order_expires_at = self.order_expires_at.astimezone(UTC)
        if order_expires_at > self.expires_at:
            raise ValueValidationError("order expiration cannot exceed proposal expiration")
        object.__setattr__(self, "order_expires_at", order_expires_at)


def proposal_order_limit(proposal: OpportunityProposal) -> Price:
    if isinstance(proposal, RestingLimitProposal):
        return proposal.limit_price
    return proposal.executable_ask


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    evaluation_id: str
    outcome: StrategyOutcome
    information_set_id: str
    strategy_version: str
    reason_codes: tuple[str, ...]
    proposal: OpportunityProposal | None

    def __post_init__(self) -> None:
        if not self.reason_codes:
            raise ValueValidationError("strategy evaluation requires at least one reason code")
        if self.outcome is StrategyOutcome.PROPOSAL and self.proposal is None:
            raise ValueValidationError("PROPOSAL evaluation requires a proposal")
        if self.outcome is StrategyOutcome.NO_PROPOSAL and self.proposal is not None:
            raise ValueValidationError("NO_PROPOSAL evaluation cannot contain a proposal")
        if self.proposal is not None:
            if self.proposal.information_set_id != self.information_set_id:
                raise ValueValidationError("proposal information set does not match its evaluation")
            if self.proposal.strategy_version != self.strategy_version:
                raise ValueValidationError(
                    "proposal strategy version does not match its evaluation"
                )


class ReferenceEdgeStrategy:
    @staticmethod
    def evaluate(
        information_set: InformationSet,
        reference_estimate: ReferenceEstimate,
        policy: CampaignPolicy,
        *,
        strategy_version: str,
        fee_model_version: str,
        fee_per_unit: Money,
        decision_at: datetime,
        resting_limit_price: Price | None = None,
        order_expires_at: datetime | None = None,
    ) -> StrategyEvaluation:
        if decision_at.tzinfo is None or decision_at.utcoffset() is None:
            raise ValueValidationError("decision time must be timezone-aware")
        decision_at = decision_at.astimezone(UTC)
        reasons: list[str] = []
        if reference_estimate.information_set_id != information_set.information_set_id:
            reasons.append("ESTIMATE_INFORMATION_SET_MISMATCH")
        estimate_source_members = tuple(
            member
            for member in information_set.ordered_members
            if member.source_id == reference_estimate.source_id
        )
        if len(estimate_source_members) != 1:
            reasons.append("ESTIMATE_SOURCE_NOT_IN_INFORMATION_SET")
        if reference_estimate.published_at > information_set.cutoff.value:
            reasons.append("ESTIMATE_NOT_POINT_IN_TIME")
        if reference_estimate.expires_at <= decision_at:
            reasons.append("ESTIMATE_EXPIRED")
        if not information_set.valid:
            reasons.append("INVALID_INFORMATION_SET")
        if information_set.market.spread > policy.max_spread:
            reasons.append("SPREAD_TOO_WIDE")
        if (resting_limit_price is None) != (order_expires_at is None):
            raise ValueValidationError(
                "resting limit price and order expiration must be supplied together"
            )

        ask = information_set.market.yes_ask
        entry_price = ask
        if resting_limit_price is not None:
            if resting_limit_price.tick != ask.tick:
                raise ValueValidationError("resting limit tick does not match the market")
            if resting_limit_price.value >= ask.value:
                raise ValueValidationError("resting YES limit must be strictly below the ask")
            entry_price = resting_limit_price
        instrument = information_set.instrument
        fee = fee_per_unit.amount
        yes_cashflow = instrument.yes_settlement_value - entry_price.value - fee
        no_cashflow = instrument.no_settlement_value - entry_price.value - fee
        void_cashflow = instrument.void_settlement_value - entry_price.value - fee
        probabilities = reference_estimate.probability_vector
        expected_value = (
            probabilities.yes.value * yes_cashflow
            + probabilities.no.value * no_cashflow
            + probabilities.void.value * void_cashflow
        )
        maximum_loss = max(Decimal(0), -min(yes_cashflow, no_cashflow, void_cashflow))
        if expected_value < policy.min_synthetic_edge:
            reasons.append("SYNTHETIC_EDGE_BELOW_THRESHOLD")
        if maximum_loss <= 0:
            reasons.append("MAXIMUM_LOSS_NOT_BOUNDED")

        evaluation_payload = {
            "information_set_id": information_set.information_set_id,
            "estimate_id": reference_estimate.estimate_id,
            "strategy_version": strategy_version,
            "policy_version_id": policy.policy_version_id,
            "ask": ask,
            "fee": fee_per_unit,
            "expected_value": expected_value,
            "maximum_loss": maximum_loss,
        }
        if resting_limit_price is not None:
            evaluation_payload["resting_limit_price"] = resting_limit_price
            evaluation_payload["order_expires_at"] = order_expires_at
        if reasons:
            return StrategyEvaluation(
                evaluation_id=deterministic_id("evaluation", evaluation_payload),
                outcome=StrategyOutcome.NO_PROPOSAL,
                information_set_id=information_set.information_set_id,
                strategy_version=strategy_version,
                reason_codes=tuple(sorted(reasons)),
                proposal=None,
            )

        expiration = min(reference_estimate.expires_at, information_set.rule.close_at)
        if order_expires_at is not None:
            if order_expires_at.tzinfo is None or order_expires_at.utcoffset() is None:
                raise ValueValidationError("order expiration must be timezone-aware")
            order_expires_at = order_expires_at.astimezone(UTC)
            if not decision_at < order_expires_at <= expiration:
                raise ValueValidationError(
                    "resting order must expire after decision and by proposal expiration"
                )
        fingerprint_payload = {
            **evaluation_payload,
            "instrument_id": instrument.instrument_id,
            "rule_version_id": information_set.rule.rule_version_id,
            "side": Side.YES,
            "requested_risk_fraction": policy.requested_risk_fraction,
            "expires_at": expiration,
        }
        fingerprint = content_digest(fingerprint_payload)
        proposal_id = deterministic_id("proposal", fingerprint_payload)
        parsed_expected_value = parse_decimal(expected_value, allow_negative=True)
        bounded_maximum_loss = Money(maximum_loss)
        proposal: OpportunityProposal
        if resting_limit_price is not None and order_expires_at is not None:
            proposal = RestingLimitProposal(
                proposal_id=proposal_id,
                instrument_id=instrument.instrument_id,
                rule_version_id=information_set.rule.rule_version_id,
                information_set_id=information_set.information_set_id,
                strategy_version=strategy_version,
                side=Side.YES,
                executable_ask=ask,
                fee_model_version=fee_model_version,
                fee_per_unit=fee_per_unit,
                expected_net_value_per_unit=parsed_expected_value,
                maximum_loss_per_unit=bounded_maximum_loss,
                requested_risk_fraction=policy.requested_risk_fraction,
                expires_at=expiration,
                fingerprint=fingerprint,
                limit_price=resting_limit_price,
                order_expires_at=order_expires_at,
            )
        else:
            proposal = OpportunityProposal(
                proposal_id=proposal_id,
                instrument_id=instrument.instrument_id,
                rule_version_id=information_set.rule.rule_version_id,
                information_set_id=information_set.information_set_id,
                strategy_version=strategy_version,
                side=Side.YES,
                executable_ask=ask,
                fee_model_version=fee_model_version,
                fee_per_unit=fee_per_unit,
                expected_net_value_per_unit=parsed_expected_value,
                maximum_loss_per_unit=bounded_maximum_loss,
                requested_risk_fraction=policy.requested_risk_fraction,
                expires_at=expiration,
                fingerprint=fingerprint,
            )
        return StrategyEvaluation(
            evaluation_id=deterministic_id("evaluation", evaluation_payload),
            outcome=StrategyOutcome.PROPOSAL,
            information_set_id=information_set.information_set_id,
            strategy_version=strategy_version,
            reason_codes=("SYNTHETIC_THRESHOLD_MET",),
            proposal=proposal,
        )


@dataclass(frozen=True, slots=True)
class AllocationDecision:
    allocation_id: str
    accepted: bool
    proposal_id: str
    quantity: Quantity
    maximum_loss: Money
    snapshot_hash: str
    reason_codes: tuple[str, ...]


class CapitalAllocator:
    @staticmethod
    def allocate(
        proposal: OpportunityProposal,
        snapshot: CampaignSnapshot,
        visible_quantity: Quantity,
        policy: CampaignPolicy,
        *,
        decision_at: datetime,
        expected_settlement_at: datetime,
    ) -> AllocationDecision:
        if snapshot.campaign.policy != policy:
            raise ValueValidationError("allocation policy does not match campaign policy")
        capital_lockup_seconds = expected_capital_lockup_seconds(
            decision_at,
            expected_settlement_at,
        )
        per_unit_loss = proposal.maximum_loss_per_unit.amount
        if per_unit_loss <= 0:
            raise ValueValidationError("allocation requires a positive bounded per-unit loss")
        snapshot_payload = {
            "campaign_id": snapshot.campaign.campaign_id,
            "ledger_equity": snapshot.ledger_equity,
            "settled_cash": snapshot.settled_cash,
            "active_reservations": snapshot.active_reservations,
            "risk_capacity": snapshot.risk_capacity,
            "policy_version_id": policy.policy_version_id,
        }
        snapshot_hash = content_digest(snapshot_payload)
        if capital_lockup_seconds > policy.max_capital_lockup_seconds:
            quantity = Quantity(0, visible_quantity.increment)
            maximum_loss = Money(0)
            reason_codes = (POLICY_MAX_CAPITAL_LOCKUP_EXCEEDED,)
            payload = {
                "proposal_id": proposal.proposal_id,
                "quantity": quantity,
                "maximum_loss": maximum_loss,
                "snapshot_hash": snapshot_hash,
                "policy_reason_codes": reason_codes,
                "capital_lockup_seconds": capital_lockup_seconds,
                "maximum_capital_lockup_seconds": policy.max_capital_lockup_seconds,
            }
            return AllocationDecision(
                allocation_id=deterministic_id("allocation", payload),
                accepted=False,
                proposal_id=proposal.proposal_id,
                quantity=quantity,
                maximum_loss=maximum_loss,
                snapshot_hash=snapshot_hash,
                reason_codes=reason_codes,
            )
        equity = snapshot.ledger_equity.amount
        requested_capacity = equity * proposal.requested_risk_fraction.value
        trade_capacity = equity * policy.max_trade_loss_fraction.value
        open_capacity = max(
            Decimal(0),
            equity * policy.max_open_loss_fraction.value - snapshot.active_reservations.amount,
        )
        loss_capacity = min(
            requested_capacity,
            trade_capacity,
            open_capacity,
            snapshot.risk_capacity,
            snapshot.settled_cash.amount,
        )
        risk_quantity = floor_to_increment(
            loss_capacity / per_unit_loss,
            visible_quantity.increment,
        )
        quantity_value = min(risk_quantity, visible_quantity.value)
        quantity_value = floor_to_increment(quantity_value, visible_quantity.increment)
        quantity = Quantity(quantity_value, visible_quantity.increment)
        maximum_loss = Money(quantity.value * per_unit_loss)
        payload = {
            "proposal_id": proposal.proposal_id,
            "quantity": quantity,
            "maximum_loss": maximum_loss,
            "snapshot_hash": snapshot_hash,
        }
        accepted = quantity.value > 0
        return AllocationDecision(
            allocation_id=deterministic_id("allocation", payload),
            accepted=accepted,
            proposal_id=proposal.proposal_id,
            quantity=quantity,
            maximum_loss=maximum_loss,
            snapshot_hash=snapshot_hash,
            reason_codes=("ALLOCATED",) if accepted else ("ZERO_QUANTITY",),
        )


@dataclass(frozen=True, slots=True)
class RiskDecision:
    risk_decision_id: str
    accepted: bool
    allocation_id: str
    context_hash: str
    exact_order_fingerprint: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StaleMarketRiskDecision(RiskDecision):
    observation_source_id: str
    observation_available_at: datetime
    authorization_at: datetime
    observed_age_seconds: int
    maximum_age_seconds: int
    information_set_id: str
    information_set_hash: ContentHash
    invariant_explanation: str

    def __post_init__(self) -> None:
        if self.accepted:
            raise ValueValidationError("a stale-market risk decision cannot be accepted")
        if INVARIANT_MARKET_DATA_STALE not in self.reason_codes:
            raise ValueValidationError("stale-market evidence requires its invariant reason")
        if not self.observation_source_id or not self.information_set_id:
            raise ValueValidationError("stale-market evidence requires source and information IDs")
        if (
            self.observation_available_at.tzinfo is None
            or self.observation_available_at.utcoffset() is None
            or self.authorization_at.tzinfo is None
            or self.authorization_at.utcoffset() is None
        ):
            raise ValueValidationError("freshness evidence timestamps must be timezone-aware")
        observation_available_at = self.observation_available_at.astimezone(UTC)
        authorization_at = self.authorization_at.astimezone(UTC)
        age = authorization_at - observation_available_at
        if age < timedelta(0) or age.microseconds:
            raise ValueValidationError("freshness evidence requires a whole non-negative age")
        exact_age_seconds = age.days * 86_400 + age.seconds
        if (
            isinstance(self.observed_age_seconds, bool)
            or self.observed_age_seconds != exact_age_seconds
        ):
            raise ValueValidationError("observed market-data age does not match its timestamps")
        if isinstance(self.maximum_age_seconds, bool) or self.maximum_age_seconds <= 0:
            raise ValueValidationError("maximum market-data age must be a positive integer")
        if self.maximum_age_seconds != OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS:
            raise ValueValidationError("freshness evidence must use the sealed operational maximum")
        if self.observed_age_seconds <= self.maximum_age_seconds:
            raise ValueValidationError("stale-market evidence must exceed the operational maximum")
        expected_explanation = market_data_stale_explanation(
            observation_source_id=self.observation_source_id,
            observation_available_at=observation_available_at,
            authorization_at=authorization_at,
            observed_age_seconds=self.observed_age_seconds,
            maximum_age_seconds=self.maximum_age_seconds,
            information_set_id=self.information_set_id,
        )
        if self.invariant_explanation != expected_explanation:
            raise ValueValidationError(
                "stale-market explanation does not match its deterministic evidence"
            )
        object.__setattr__(self, "observation_available_at", observation_available_at)
        object.__setattr__(self, "authorization_at", authorization_at)


class RiskKernel:
    @staticmethod
    def evaluate(
        proposal: OpportunityProposal,
        allocation: AllocationDecision,
        snapshot: CampaignSnapshot,
        information_set: InformationSet,
        policy: CampaignPolicy,
        operational_invariants: OperationalInvariants,
        *,
        authorization_at: datetime,
        market_observation_source_id: str,
        existing_order_fingerprints: tuple[str, ...] = (),
    ) -> RiskDecision:
        if authorization_at.tzinfo is None or authorization_at.utcoffset() is None:
            raise ValueValidationError("risk authorization time must be timezone-aware")
        authorization_at = authorization_at.astimezone(UTC)
        context_payload = {
            "campaign_id": snapshot.campaign.campaign_id,
            "allocation_id": allocation.allocation_id,
            "information_set_hash": information_set.content_hash,
            "policy_hash": policy.content_hash,
            # Preserve the v1 identity key while binding the authorization instant.
            "decision_at": authorization_at,
            "runtime_capability": operational_invariants.runtime_capability,
        }
        context_hash = content_digest(context_payload)
        order_payload = {
            "proposal_id": proposal.proposal_id,
            "side": proposal.side,
            "limit_price": proposal_order_limit(proposal),
            "quantity": allocation.quantity,
            "rule_version_id": proposal.rule_version_id,
            "context_hash": context_hash,
        }
        if isinstance(proposal, RestingLimitProposal):
            order_payload["expires_at"] = proposal.order_expires_at
        order_fingerprint = content_digest(order_payload)
        reasons: list[str] = []
        if operational_invariants.runtime_capability != "SIMULATION_ONLY":
            reasons.append("INVALID_RUNTIME_CAPABILITY")
        if not allocation.accepted or allocation.quantity.value <= 0:
            reasons.append("INVALID_ALLOCATION")
        expected_maximum_loss = proposal.maximum_loss_per_unit.amount * allocation.quantity.value
        if allocation.maximum_loss.amount != expected_maximum_loss:
            reasons.append("ALLOCATION_MAXIMUM_LOSS_MISMATCH")
        if allocation.maximum_loss.amount <= 0:
            reasons.append("UNBOUNDED_MAXIMUM_LOSS")
        if allocation.maximum_loss.amount > snapshot.campaign.authorized_loss.amount:
            reasons.append("AUTHORIZED_LOSS_EXCEEDED")
        if allocation.maximum_loss.amount > snapshot.risk_capacity:
            reasons.append("RISK_CAPACITY_EXCEEDED")
        if allocation.maximum_loss.amount > snapshot.settled_cash.amount:
            reasons.append("SETTLED_CASH_EXCEEDED")
        if not information_set.valid or any(
            member.available_at > information_set.cutoff.value
            for member in information_set.ordered_members
        ):
            reasons.append("INVALID_POINT_IN_TIME_DATA")
        if proposal.rule_version_id != information_set.rule.rule_version_id:
            reasons.append("RULE_VERSION_MISMATCH")
        if proposal.information_set_id != information_set.information_set_id:
            reasons.append("INFORMATION_SET_MISMATCH")
        if authorization_at >= proposal.expires_at:
            reasons.append("PROPOSAL_EXPIRED")
        if (
            isinstance(proposal, RestingLimitProposal)
            and authorization_at >= proposal.order_expires_at
        ):
            reasons.append("ORDER_EXPIRED")
        market_members = tuple(
            member
            for member in information_set.ordered_members
            if member.source_id == market_observation_source_id
        )
        if len(market_members) != 1:
            raise ValueValidationError(
                "authoritative market observation must identify one information-set member"
            )
        market_member = market_members[0]
        observed_age = authorization_at - market_member.available_at
        if observed_age < timedelta(0):
            reasons.append("AUTHORIZATION_PRECEDES_MARKET_DATA")
        elif observed_age.microseconds:
            reasons.append("NON_WHOLE_SECOND_AUTHORIZATION_CONTEXT")
        observed_age_seconds = observed_age.days * 86_400 + observed_age.seconds
        stale = not observed_age.microseconds and observed_age > timedelta(
            seconds=operational_invariants.maximum_market_data_age_seconds
        )
        stale_explanation: str | None = None
        if stale:
            reasons.append(INVARIANT_MARKET_DATA_STALE)
            stale_explanation = market_data_stale_explanation(
                observation_source_id=market_member.source_id,
                observation_available_at=market_member.available_at,
                authorization_at=authorization_at,
                observed_age_seconds=observed_age_seconds,
                maximum_age_seconds=operational_invariants.maximum_market_data_age_seconds,
                information_set_id=information_set.information_set_id,
            )
        if information_set.instrument.category.upper() == "SPORTS":
            reasons.append("SPORTS_PROHIBITED")
        if order_fingerprint in existing_order_fingerprints:
            reasons.append("DUPLICATE_ORDER_FINGERPRINT")
        accepted = not reasons
        decision_payload = {
            "allocation_id": allocation.allocation_id,
            "context_hash": context_hash,
            "order_fingerprint": order_fingerprint,
            "accepted": accepted,
            "reason_codes": tuple(sorted(reasons)),
        }
        if stale:
            decision_payload["freshness_evidence"] = {
                "observation_source_id": market_member.source_id,
                "observation_available_at": market_member.available_at,
                "authorization_at": authorization_at,
                "observed_age_seconds": observed_age_seconds,
                "maximum_age_seconds": (operational_invariants.maximum_market_data_age_seconds),
                "information_set_id": information_set.information_set_id,
                "information_set_hash": information_set.content_hash,
            }
        risk_decision_id = deterministic_id("risk_decision", decision_payload)
        reason_codes = ("ALL_INVARIANTS_PASSED",) if accepted else tuple(sorted(reasons))
        if stale:
            if stale_explanation is None:
                raise RuntimeError("stale risk decision is missing deterministic evidence")
            return StaleMarketRiskDecision(
                risk_decision_id=risk_decision_id,
                accepted=accepted,
                allocation_id=allocation.allocation_id,
                context_hash=context_hash,
                exact_order_fingerprint=order_fingerprint,
                reason_codes=reason_codes,
                observation_source_id=market_member.source_id,
                observation_available_at=market_member.available_at,
                authorization_at=authorization_at,
                observed_age_seconds=observed_age_seconds,
                maximum_age_seconds=operational_invariants.maximum_market_data_age_seconds,
                information_set_id=information_set.information_set_id,
                information_set_hash=information_set.content_hash,
                invariant_explanation=stale_explanation,
            )
        return RiskDecision(
            risk_decision_id=risk_decision_id,
            accepted=accepted,
            allocation_id=allocation.allocation_id,
            context_hash=context_hash,
            exact_order_fingerprint=order_fingerprint,
            reason_codes=reason_codes,
        )
