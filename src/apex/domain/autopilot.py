"""Immutable opportunity mathematics and shadow-portfolio rules.

This module deliberately describes a prospective bounded-loss opportunity.  It
does not create financial authority: allocation and risk outcomes retained by
the Autopilot are evidence of what a later paper-authorisation boundary would
have decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.values import (
    ContentHash,
    DecimalInput,
    Probability,
    Quantity,
    RiskFraction,
    ValueValidationError,
    canonical_timestamp,
    parse_decimal,
)


class OpportunityValidity(StrEnum):
    VALID = "VALID"
    REJECTED_MALFORMED = "REJECTED_MALFORMED"
    REJECTED_NONPOSITIVE_CONSERVATIVE_VALUE = "REJECTED_NONPOSITIVE_CONSERVATIVE_VALUE"
    REJECTED_EVIDENCE = "REJECTED_EVIDENCE"
    REJECTED_EXPIRED = "REJECTED_EXPIRED"
    REJECTED_DUPLICATE = "REJECTED_DUPLICATE"
    REJECTED_POLICY = "REJECTED_POLICY"
    REJECTED_PORTFOLIO = "REJECTED_PORTFOLIO"
    REJECTED_OPERATIONAL = "REJECTED_OPERATIONAL"


class EvidenceTier(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    RESEARCH = "RESEARCH"
    VERIFIED = "VERIFIED"
    STRONG = "STRONG"

    @property
    def rank(self) -> int:
        return {
            EvidenceTier.UNVERIFIED: 0,
            EvidenceTier.RESEARCH: 1,
            EvidenceTier.VERIFIED: 2,
            EvidenceTier.STRONG: 3,
        }[self]


class CampaignOperatingState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    KILLED = "KILLED"
    EXPIRED = "EXPIRED"


class ShadowDecisionState(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


def _required_text(value: str, *, label: str, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueValidationError(f"{label} must be non-empty and at most {maximum} characters")
    return value


def _timestamp(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True, init=False)
class OpportunityOutcome:
    """One finite state in a bounded prediction-market payout model."""

    outcome_id: str
    point_probability: Probability
    conservative_probability: Probability
    payout: Decimal

    def __init__(
        self,
        *,
        outcome_id: str,
        point_probability: Probability | DecimalInput,
        conservative_probability: Probability | DecimalInput,
        payout: DecimalInput,
    ) -> None:
        object.__setattr__(self, "outcome_id", _required_text(outcome_id, label="outcome ID"))
        object.__setattr__(
            self,
            "point_probability",
            point_probability
            if isinstance(point_probability, Probability)
            else Probability(point_probability),
        )
        object.__setattr__(
            self,
            "conservative_probability",
            conservative_probability
            if isinstance(conservative_probability, Probability)
            else Probability(conservative_probability),
        )
        object.__setattr__(self, "payout", parse_decimal(payout))


@dataclass(frozen=True, slots=True, init=False)
class OpportunityEconomics:
    """Validated prospective economics tied to an existing proposal identity."""

    proposal_id: str
    strategy_id: str
    strategy_version: str
    instrument_id: str
    decision_at: datetime
    capital_release_at: datetime
    outcomes: tuple[OpportunityOutcome, ...]
    acquisition_cost: Decimal
    expected_fee: Decimal
    slippage_reserve: Decimal
    execution_uncertainty_reserve: Decimal
    visible_quantity: Quantity | None
    requested_risk_fraction: RiskFraction
    evidence_tier: EvidenceTier
    evidence_references: tuple[str, ...]
    correlation_buckets: tuple[str, ...]
    configuration_version: str
    configuration_hash: ContentHash

    def __init__(
        self,
        *,
        proposal_id: str,
        strategy_id: str,
        strategy_version: str,
        instrument_id: str,
        decision_at: datetime,
        capital_release_at: datetime,
        outcomes: tuple[OpportunityOutcome, ...],
        acquisition_cost: DecimalInput,
        expected_fee: DecimalInput,
        slippage_reserve: DecimalInput,
        execution_uncertainty_reserve: DecimalInput,
        visible_quantity: Quantity | None,
        requested_risk_fraction: RiskFraction | DecimalInput,
        evidence_tier: EvidenceTier,
        evidence_references: tuple[str, ...],
        correlation_buckets: tuple[str, ...],
        configuration_version: str,
        configuration_hash: ContentHash,
    ) -> None:
        if not isinstance(evidence_tier, EvidenceTier):
            raise ValueValidationError("evidence tier is invalid")
        if not isinstance(configuration_hash, ContentHash):
            raise ValueValidationError("configuration hash is invalid")
        if len(outcomes) < 2 or len(outcomes) > 32:
            raise ValueValidationError("opportunity requires between two and 32 outcome states")
        outcome_ids = tuple(item.outcome_id for item in outcomes)
        if len(set(outcome_ids)) != len(outcome_ids):
            raise ValueValidationError("opportunity outcome IDs must be unique")
        if sum((item.point_probability.value for item in outcomes), Decimal(0)) != Decimal(1):
            raise ValueValidationError("point probabilities must sum exactly to one")
        if sum((item.conservative_probability.value for item in outcomes), Decimal(0)) != Decimal(
            1
        ):
            raise ValueValidationError("conservative probabilities must sum exactly to one")
        if not evidence_references or len(evidence_references) > 32:
            raise ValueValidationError("opportunity requires bounded non-empty evidence references")
        normalized_references = tuple(
            _required_text(value, label="evidence reference", maximum=300)
            for value in evidence_references
        )
        if len(set(normalized_references)) != len(normalized_references):
            raise ValueValidationError("evidence references must be unique")
        if not correlation_buckets or len(correlation_buckets) > 8:
            raise ValueValidationError(
                "opportunity requires between one and eight correlation buckets"
            )
        normalized_buckets = tuple(
            _required_text(value, label="correlation bucket", maximum=80)
            for value in correlation_buckets
        )
        if tuple(sorted(normalized_buckets)) != normalized_buckets:
            raise ValueValidationError("correlation buckets must be deterministic sorted order")
        if len(set(normalized_buckets)) != len(normalized_buckets):
            raise ValueValidationError("correlation buckets must be unique")
        parsed_decision_at = _timestamp(decision_at, label="decision timestamp")
        parsed_release_at = _timestamp(capital_release_at, label="capital-release timestamp")
        if parsed_release_at <= parsed_decision_at:
            raise ValueValidationError("capital release must occur after the decision timestamp")
        fraction = (
            requested_risk_fraction
            if isinstance(requested_risk_fraction, RiskFraction)
            else RiskFraction(requested_risk_fraction)
        )
        if fraction.value <= 0:
            raise ValueValidationError("requested risk fraction must be greater than zero")
        if visible_quantity is not None and visible_quantity.value <= 0:
            raise ValueValidationError("visible quantity must be positive when known")
        object.__setattr__(self, "proposal_id", _required_text(proposal_id, label="proposal ID"))
        object.__setattr__(self, "strategy_id", _required_text(strategy_id, label="strategy ID"))
        object.__setattr__(
            self,
            "strategy_version",
            _required_text(strategy_version, label="strategy version"),
        )
        object.__setattr__(
            self, "instrument_id", _required_text(instrument_id, label="instrument ID")
        )
        object.__setattr__(self, "decision_at", parsed_decision_at)
        object.__setattr__(self, "capital_release_at", parsed_release_at)
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(self, "acquisition_cost", parse_decimal(acquisition_cost))
        object.__setattr__(self, "expected_fee", parse_decimal(expected_fee))
        object.__setattr__(self, "slippage_reserve", parse_decimal(slippage_reserve))
        object.__setattr__(
            self,
            "execution_uncertainty_reserve",
            parse_decimal(execution_uncertainty_reserve),
        )
        object.__setattr__(self, "visible_quantity", visible_quantity)
        object.__setattr__(self, "requested_risk_fraction", fraction)
        object.__setattr__(self, "evidence_tier", evidence_tier)
        object.__setattr__(self, "evidence_references", normalized_references)
        object.__setattr__(self, "correlation_buckets", normalized_buckets)
        object.__setattr__(
            self,
            "configuration_version",
            _required_text(configuration_version, label="configuration version"),
        )
        object.__setattr__(self, "configuration_hash", configuration_hash)

    @property
    def point_expected_payout(self) -> Decimal:
        return sum(
            (item.point_probability.value * item.payout for item in self.outcomes),
            Decimal(0),
        )

    @property
    def conservative_expected_payout(self) -> Decimal:
        return sum(
            (item.conservative_probability.value * item.payout for item in self.outcomes),
            Decimal(0),
        )

    @property
    def total_modeled_acquisition_cost(self) -> Decimal:
        return (
            self.acquisition_cost
            + self.expected_fee
            + self.slippage_reserve
            + self.execution_uncertainty_reserve
        )

    @property
    def point_net_expected_value(self) -> Decimal:
        return self.point_expected_payout - self.total_modeled_acquisition_cost

    @property
    def conservative_net_expected_value(self) -> Decimal:
        return self.conservative_expected_payout - self.total_modeled_acquisition_cost

    @property
    def minimum_payout(self) -> Decimal:
        return min(item.payout for item in self.outcomes)

    @property
    def maximum_payout(self) -> Decimal:
        return max(item.payout for item in self.outcomes)

    @property
    def maximum_loss(self) -> Decimal:
        return max(self.total_modeled_acquisition_cost - self.minimum_payout, Decimal(0))

    @property
    def maximum_upside(self) -> Decimal:
        return max(self.maximum_payout - self.total_modeled_acquisition_cost, Decimal(0))

    @property
    def capital_efficiency(self) -> Decimal | None:
        if self.maximum_loss <= 0:
            return None
        return self.conservative_net_expected_value / self.maximum_loss

    @property
    def capital_lockup_seconds(self) -> int:
        elapsed = self.capital_release_at - self.decision_at
        return elapsed.days * 86_400 + elapsed.seconds

    @property
    def canonical_rank_inputs(self) -> tuple[object, ...]:
        """Retained inputs for transparent descending/ascending sort behavior."""

        return (
            self.evidence_tier.rank,
            self.conservative_net_expected_value > 0,
            self.capital_efficiency,
            self.conservative_net_expected_value,
            self.capital_lockup_seconds,
            self.visible_quantity.value if self.visible_quantity is not None else None,
            self.strategy_id,
            self.proposal_id,
            canonical_timestamp(self.decision_at),
        )


@dataclass(frozen=True, slots=True, init=False)
class CampaignPortfolioPolicy:
    """Versioned cross-opportunity limits; never a risk-kernel bypass."""

    policy_version_id: str
    campaign_id: str
    maximum_active_risk_fraction: RiskFraction
    maximum_per_opportunity_risk_fraction: RiskFraction
    maximum_per_strategy_risk_fraction: RiskFraction
    maximum_per_correlation_bucket_risk_fraction: RiskFraction
    maximum_simultaneous_reservations: int
    maximum_simultaneous_open_positions: int
    maximum_capital_lockup_seconds: int
    minimum_conservative_net_expected_value: Decimal
    minimum_evidence_tier: EvidenceTier
    allow_profit_compounding: bool
    configuration_hash: ContentHash

    def __init__(
        self,
        *,
        policy_version_id: str,
        campaign_id: str,
        maximum_active_risk_fraction: RiskFraction | DecimalInput,
        maximum_per_opportunity_risk_fraction: RiskFraction | DecimalInput,
        maximum_per_strategy_risk_fraction: RiskFraction | DecimalInput,
        maximum_per_correlation_bucket_risk_fraction: RiskFraction | DecimalInput,
        maximum_simultaneous_reservations: int,
        maximum_simultaneous_open_positions: int,
        maximum_capital_lockup_seconds: int,
        minimum_conservative_net_expected_value: DecimalInput,
        minimum_evidence_tier: EvidenceTier,
        allow_profit_compounding: bool,
        configuration_hash: ContentHash,
    ) -> None:
        for label, integer_value in (
            ("maximum simultaneous reservations", maximum_simultaneous_reservations),
            ("maximum simultaneous open positions", maximum_simultaneous_open_positions),
            ("maximum capital lockup", maximum_capital_lockup_seconds),
        ):
            if isinstance(integer_value, bool) or integer_value < 0:
                raise ValueValidationError(f"{label} must be a non-negative integer")
        if maximum_capital_lockup_seconds == 0:
            raise ValueValidationError("maximum capital lockup must be positive")
        if not isinstance(minimum_evidence_tier, EvidenceTier):
            raise ValueValidationError("minimum evidence tier is invalid")
        if not isinstance(configuration_hash, ContentHash):
            raise ValueValidationError("portfolio policy configuration hash is invalid")
        fractions: dict[str, RiskFraction | DecimalInput] = {
            "maximum_active_risk_fraction": maximum_active_risk_fraction,
            "maximum_per_opportunity_risk_fraction": maximum_per_opportunity_risk_fraction,
            "maximum_per_strategy_risk_fraction": maximum_per_strategy_risk_fraction,
            "maximum_per_correlation_bucket_risk_fraction": (
                maximum_per_correlation_bucket_risk_fraction
            ),
        }
        object.__setattr__(
            self,
            "policy_version_id",
            _required_text(policy_version_id, label="portfolio policy ID"),
        )
        object.__setattr__(self, "campaign_id", _required_text(campaign_id, label="campaign ID"))
        for name, fraction_value in fractions.items():
            parsed = (
                fraction_value
                if isinstance(fraction_value, RiskFraction)
                else RiskFraction(fraction_value)
            )
            object.__setattr__(self, name, parsed)
        object.__setattr__(
            self, "maximum_simultaneous_reservations", maximum_simultaneous_reservations
        )
        object.__setattr__(
            self, "maximum_simultaneous_open_positions", maximum_simultaneous_open_positions
        )
        object.__setattr__(self, "maximum_capital_lockup_seconds", maximum_capital_lockup_seconds)
        object.__setattr__(
            self,
            "minimum_conservative_net_expected_value",
            parse_decimal(minimum_conservative_net_expected_value, allow_negative=True),
        )
        object.__setattr__(self, "minimum_evidence_tier", minimum_evidence_tier)
        object.__setattr__(self, "allow_profit_compounding", allow_profit_compounding)
        object.__setattr__(self, "configuration_hash", configuration_hash)


@dataclass(frozen=True, slots=True)
class CampaignPortfolioSnapshot:
    campaign_id: str
    funded_capital: Decimal
    settled_cash: Decimal
    ledger_equity: Decimal
    realized_trading_pnl: Decimal
    fees: Decimal
    remaining_loss_capacity: Decimal
    current_reservation_risk: Decimal
    current_open_position_risk: Decimal
    active_risk_by_strategy: tuple[tuple[str, Decimal], ...]
    active_risk_by_correlation_bucket: tuple[tuple[str, Decimal], ...]
    reservation_count: int
    open_position_count: int
    earliest_capital_release_at: datetime | None
    latest_capital_release_at: datetime | None
    snapshot_hash: str

    @property
    def active_risk(self) -> Decimal:
        return self.current_reservation_risk + self.current_open_position_risk
