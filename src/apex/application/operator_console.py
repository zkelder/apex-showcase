"""Local Operator Console commands, deliberately separate from financial authority.

The Console validates bounded operator intent and delegates persistence and every
financial transition to existing application services/adapters.  It never accepts
operator-entered settlement values or creates orders directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from apex.domain.autopilot import EvidenceTier
from apex.domain.values import Money, RiskFraction, ValueValidationError


class OperatorMode(StrEnum):
    SHADOW = "SHADOW"
    PAPER = "PAPER"


class OperatorActionType(StrEnum):
    CREATE_CAMPAIGN = "CREATE_CAMPAIGN"
    RUN_SHADOW_CYCLE = "RUN_SHADOW_CYCLE"
    RUN_PAPER_CYCLE = "RUN_PAPER_CYCLE"
    PAUSE_CAMPAIGN = "PAUSE_CAMPAIGN"
    RESUME_CAMPAIGN = "RESUME_CAMPAIGN"
    KILL_CAMPAIGN = "KILL_CAMPAIGN"
    MARK_POSITIONS = "MARK_POSITIONS"
    SETTLE_POSITION = "SETTLE_POSITION"
    SUBMIT_THESIS = "SUBMIT_THESIS"
    EVALUATE_PREDICTION = "EVALUATE_PREDICTION"
    CHANGE_STRATEGY_AUTHORITY = "CHANGE_STRATEGY_AUTHORITY"
    RUN_LIVE_SHADOW_SESSION = "RUN_LIVE_SHADOW_SESSION"
    BUILD_EVIDENCE_SNAPSHOT = "BUILD_EVIDENCE_SNAPSHOT"
    REQUEST_STRATEGY_EVIDENCE_REVIEW = "REQUEST_STRATEGY_EVIDENCE_REVIEW"
    START_STRATEGY_EVIDENCE_REVIEW = "START_STRATEGY_EVIDENCE_REVIEW"
    DECIDE_STRATEGY_EVIDENCE_REVIEW = "DECIDE_STRATEGY_EVIDENCE_REVIEW"
    DEFER_STRATEGY_EVIDENCE_REVIEW = "DEFER_STRATEGY_EVIDENCE_REVIEW"
    CANCEL_STRATEGY_EVIDENCE_REVIEW = "CANCEL_STRATEGY_EVIDENCE_REVIEW"
    EXPIRE_STRATEGY_EVIDENCE_REVIEWS = "EXPIRE_STRATEGY_EVIDENCE_REVIEWS"
    CREATE_PAPER_TRIAL_AUTHORIZATION = "CREATE_PAPER_TRIAL_AUTHORIZATION"
    PAUSE_PAPER_TRIAL_AUTHORIZATION = "PAUSE_PAPER_TRIAL_AUTHORIZATION"
    RESUME_PAPER_TRIAL_AUTHORIZATION = "RESUME_PAPER_TRIAL_AUTHORIZATION"
    REVOKE_PAPER_TRIAL_AUTHORIZATION = "REVOKE_PAPER_TRIAL_AUTHORIZATION"
    COMPLETE_PAPER_TRIAL_AUTHORIZATION = "COMPLETE_PAPER_TRIAL_AUTHORIZATION"
    RECONCILE_PAPER_TRIAL_LIMITS = "RECONCILE_PAPER_TRIAL_LIMITS"


@dataclass(frozen=True, slots=True)
class CreateMoonshotCampaignCommand:
    campaign_id: str
    name: str
    initial_capital: Money
    maximum_authorized_loss: Money
    starts_at: datetime
    expires_at: datetime
    operating_mode: OperatorMode
    maximum_active_risk_fraction: RiskFraction
    maximum_per_opportunity_risk_fraction: RiskFraction
    maximum_per_strategy_risk_fraction: RiskFraction
    maximum_per_correlation_bucket_risk_fraction: RiskFraction
    maximum_simultaneous_reservations: int
    maximum_simultaneous_positions: int
    maximum_capital_lockup_seconds: int
    minimum_conservative_expected_value: Money
    minimum_evidence_tier: EvidenceTier
    allow_profit_compounding: bool
    operator_note: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if not self.campaign_id or len(self.campaign_id) > 160:
            raise ValueValidationError("campaign ID must be non-empty and at most 160 characters")
        if not self.name or len(self.name) > 160:
            raise ValueValidationError("campaign name must be non-empty and at most 160 characters")
        if not self.idempotency_key or len(self.idempotency_key) > 160:
            raise ValueValidationError(
                "idempotency key must be non-empty and at most 160 characters"
            )
        if len(self.operator_note) > 500:
            raise ValueValidationError("operator note must be at most 500 characters")
        if self.initial_capital.amount <= 0:
            raise ValueValidationError("initial capital must be positive")
        if self.maximum_authorized_loss.amount <= 0:
            raise ValueValidationError("maximum authorized loss must be positive")
        if self.maximum_authorized_loss.amount > self.initial_capital.amount:
            raise ValueValidationError("maximum authorized loss cannot exceed initial capital")
        if self.starts_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueValidationError("campaign timestamps must be timezone-aware")
        if self.expires_at.astimezone(UTC) <= self.starts_at.astimezone(UTC):
            raise ValueValidationError("campaign expiration must follow campaign start")
        if not 0 <= self.maximum_simultaneous_reservations <= 100:
            raise ValueValidationError(
                "maximum simultaneous reservations must be between 0 and 100"
            )
        if not 0 <= self.maximum_simultaneous_positions <= 100:
            raise ValueValidationError("maximum simultaneous positions must be between 0 and 100")
        if not 1 <= self.maximum_capital_lockup_seconds <= 86_400:
            raise ValueValidationError("maximum capital lockup must be between 1 and 86400 seconds")
        if self.minimum_conservative_expected_value.amount < 0:
            raise ValueValidationError("minimum conservative expected value cannot be negative")


class MoonshotCampaignStore(Protocol):
    def create_moonshot_campaign(
        self, command: CreateMoonshotCampaignCommand, *, requested_at: datetime
    ) -> dict[str, object]: ...


class CreateMoonshotCampaign:
    """Bounded creation boundary; the adapter owns the one durable transaction."""

    def __init__(self, store: MoonshotCampaignStore) -> None:
        self._store = store

    def create(self, command: CreateMoonshotCampaignCommand) -> dict[str, object]:
        return self._store.create_moonshot_campaign(command, requested_at=datetime.now(tz=UTC))
