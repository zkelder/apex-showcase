"""Campaign policy, capital boundary, and non-configurable invariants."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from apex.domain.values import (
    ContentHash,
    DecimalInput,
    Money,
    RiskFraction,
    ValueValidationError,
    parse_decimal,
)

OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS = 300


@dataclass(frozen=True, slots=True, init=False)
class CampaignPolicy:
    policy_version_id: str
    profile: str
    requested_risk_fraction: RiskFraction
    max_trade_loss_fraction: RiskFraction
    max_open_loss_fraction: RiskFraction
    min_synthetic_edge: Decimal
    max_spread: Decimal
    max_capital_lockup_seconds: int
    content_hash: ContentHash

    def __init__(
        self,
        *,
        policy_version_id: str,
        profile: str,
        requested_risk_fraction: RiskFraction | DecimalInput,
        max_trade_loss_fraction: RiskFraction | DecimalInput,
        max_open_loss_fraction: RiskFraction | DecimalInput,
        min_synthetic_edge: DecimalInput,
        max_spread: DecimalInput,
        max_capital_lockup_seconds: int,
        content_hash: ContentHash,
    ) -> None:
        if not policy_version_id or not profile:
            raise ValueValidationError("policy identifiers must not be empty")
        if isinstance(max_capital_lockup_seconds, bool) or max_capital_lockup_seconds <= 0:
            raise ValueValidationError("maximum lockup must be a positive integer")
        parsed_edge = parse_decimal(min_synthetic_edge)
        parsed_spread = parse_decimal(max_spread)
        if parsed_spread > 1:
            raise ValueValidationError("maximum spread cannot exceed one")
        object.__setattr__(self, "policy_version_id", policy_version_id)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(
            self,
            "requested_risk_fraction",
            requested_risk_fraction
            if isinstance(requested_risk_fraction, RiskFraction)
            else RiskFraction(requested_risk_fraction),
        )
        object.__setattr__(
            self,
            "max_trade_loss_fraction",
            max_trade_loss_fraction
            if isinstance(max_trade_loss_fraction, RiskFraction)
            else RiskFraction(max_trade_loss_fraction),
        )
        object.__setattr__(
            self,
            "max_open_loss_fraction",
            max_open_loss_fraction
            if isinstance(max_open_loss_fraction, RiskFraction)
            else RiskFraction(max_open_loss_fraction),
        )
        object.__setattr__(self, "min_synthetic_edge", parsed_edge)
        object.__setattr__(self, "max_spread", parsed_spread)
        object.__setattr__(self, "max_capital_lockup_seconds", max_capital_lockup_seconds)
        object.__setattr__(self, "content_hash", content_hash)


@dataclass(frozen=True, slots=True)
class OperationalInvariants:
    """Sealed invariants: callers cannot configure or disable these values."""

    runtime_capability: str = "SIMULATION_ONLY"
    execution_evidence: str = "SYNTHETIC"
    sports_allowed: bool = False
    bounded_loss_required: bool = True
    balanced_journals_required: bool = True
    maximum_market_data_age_seconds: int = OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS

    def __init__(self) -> None:
        object.__setattr__(self, "runtime_capability", "SIMULATION_ONLY")
        object.__setattr__(self, "execution_evidence", "SYNTHETIC")
        object.__setattr__(self, "sports_allowed", False)
        object.__setattr__(self, "bounded_loss_required", True)
        object.__setattr__(self, "balanced_journals_required", True)
        object.__setattr__(
            self,
            "maximum_market_data_age_seconds",
            OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS,
        )


@dataclass(frozen=True, slots=True)
class Campaign:
    campaign_id: str
    contribution: Money
    authorized_loss: Money
    policy: CampaignPolicy

    def __post_init__(self) -> None:
        if not self.campaign_id:
            raise ValueValidationError("campaign identifier must not be empty")
        if self.authorized_loss.amount > self.contribution.amount:
            raise ValueValidationError("authorized campaign loss cannot exceed contributed capital")


@dataclass(frozen=True, slots=True)
class CampaignSnapshot:
    campaign: Campaign
    ledger_equity: Money
    settled_cash: Money
    active_reservations: Money

    @property
    def protected_equity_floor(self) -> Decimal:
        return max(
            Decimal(0),
            self.campaign.contribution.amount - self.campaign.authorized_loss.amount,
        )

    @property
    def risk_capacity(self) -> Decimal:
        return max(
            Decimal(0),
            self.ledger_equity.amount
            - self.protected_equity_floor
            - self.active_reservations.amount,
        )


def active_campaign_risk(
    position_maximum_loss: Money,
    active_order_reservations: Money,
) -> Money:
    """Combine executed-position risk and unfilled-order risk exactly once."""

    return Money(position_maximum_loss.amount + active_order_reservations.amount)
