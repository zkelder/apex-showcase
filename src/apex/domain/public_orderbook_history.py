"""Pure same-market history rules for retained public orderbook evidence.

Repeated point-in-time observations are research evidence only.  A candidate
streak never implies continuous liquidity, fillability, or trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from apex.domain.public_complement import (
    BinaryComplementEvaluation,
    PublicOrderbookSnapshot,
    SeriesFeeSnapshot,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash, ValueValidationError, content_digest, parse_decimal


class OrderbookHistoryAnalysisState(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"


class OrderbookHistoryObservationState(StrEnum):
    PRESENT_CANDIDATE = "PRESENT_CANDIDATE"
    PRESENT_NO_CANDIDATE = "PRESENT_NO_CANDIDATE"
    PRESENT_INELIGIBLE = "PRESENT_INELIGIBLE"
    PRESENT_INVALID = "PRESENT_INVALID"


class OrderbookHistoryContinuityState(StrEnum):
    FIRST_OBSERVATION = "FIRST_OBSERVATION"
    CONTINUOUS = "CONTINUOUS"
    EVIDENCE_BREAK = "EVIDENCE_BREAK"
    OBSERVATION_GAP = "OBSERVATION_GAP"
    NOT_APPLICABLE = "NOT_APPLICABLE"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _decimal(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return parse_decimal(format(value.normalize(), "f"), allow_negative=True)


def _duration_microseconds(value: timedelta | None) -> int | None:
    if value is None:
        return None
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


@dataclass(frozen=True, slots=True)
class OrderbookHistoryEvidence:
    """One persisted market/orderbook/fee/evaluation lineage tuple."""

    orderbook: PublicOrderbookSnapshot
    evaluation: BinaryComplementEvaluation
    fee: SeriesFeeSnapshot | None
    rule_evidence_hash: ContentHash | None

    def __post_init__(self) -> None:
        if self.orderbook.provider is not PublicMarketProvider.KALSHI:
            raise ValueValidationError("orderbook history supports Kalshi evidence only")
        if self.evaluation.orderbook_snapshot_id != self.orderbook.orderbook_snapshot_id:
            raise ValueValidationError("history evaluation must reference its exact orderbook")
        if self.evaluation.market_snapshot_id != self.orderbook.market_snapshot_id:
            raise ValueValidationError("history evaluation market lineage conflicts")
        if self.evaluation.provider_market_ticker != self.orderbook.provider_market_ticker:
            raise ValueValidationError("history evaluation ticker lineage conflicts")
        if self.fee is not None:
            if self.evaluation.fee_snapshot_id != self.fee.fee_snapshot_id:
                raise ValueValidationError("history evaluation fee lineage conflicts")
            if self.fee.series_ticker != self.orderbook.series_ticker:
                raise ValueValidationError("history fee series lineage conflicts")

    @property
    def observed_at(self) -> datetime:
        return self.orderbook.observed_at

    @property
    def market_lineage_hash(self) -> ContentHash:
        """Stable identity of the normalized market, rather than one scan run.

        Every observation has a fresh raw response and orderbook snapshot.  A
        run-specific identity here would falsely classify ordinary repeated
        observations as evidence breaks.
        """
        return ContentHash(
            content_digest(
                {
                    "provider": self.orderbook.provider,
                    "provider_market_ticker": self.orderbook.provider_market_ticker,
                    "provider_event_ticker": self.orderbook.provider_event_ticker,
                }
            )
        )

    @property
    def fee_evidence_hash(self) -> ContentHash | None:
        return self.fee.canonical_hash if self.fee else None


@dataclass(frozen=True, slots=True)
class OrderbookHistoryObservation:
    observation_id: str
    analysis_id: str
    ordinal: int
    orderbook_snapshot_id: str
    evaluation_id: str
    market_snapshot_id: str
    fee_snapshot_id: str | None
    observed_at: datetime
    state: OrderbookHistoryObservationState
    matched_quantity: Decimal | None
    weighted_yes_ask: Decimal | None
    weighted_no_ask: Decimal | None
    gross_acquisition_cost: Decimal | None
    gross_settlement_value: Decimal | None
    gross_complement_gap: Decimal | None
    yes_modeled_taker_fee: Decimal | None
    no_modeled_taker_fee: Decimal | None
    modeled_taker_fees: Decimal | None
    conservative_rounding_reserve: Decimal | None
    total_modeled_acquisition_cost: Decimal | None
    conservative_net_lower_bound: Decimal | None
    net_lower_bound_per_contract: Decimal | None
    minimum_visible_matched_size: Decimal | None
    rule_evidence_hash: ContentHash | None
    series_ticker: str
    fee_schedule_identity: str | None
    fee_multiplier: Decimal | None
    fee_evidence_hash: ContentHash | None
    fee_model_identity: str | None
    rounding_identity: str | None
    market_lineage_hash: ContentHash
    continuity_state: OrderbookHistoryContinuityState
    matched_quantity_delta: Decimal | None
    gross_complement_gap_delta: Decimal | None
    modeled_taker_fees_delta: Decimal | None
    conservative_net_lower_bound_delta: Decimal | None
    net_lower_bound_per_contract_delta: Decimal | None
    weighted_yes_ask_delta: Decimal | None
    weighted_no_ask_delta: Decimal | None
    reason_codes: tuple[str, ...]
    canonical_input_hash: ContentHash

    def __post_init__(self) -> None:
        if not self.observation_id or not self.analysis_id or not self.orderbook_snapshot_id:
            raise ValueValidationError("orderbook history observation identities must not be empty")
        if self.ordinal < 0 or not self.evaluation_id or not self.market_snapshot_id:
            raise ValueValidationError("orderbook history observation is incomplete")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        for name in (
            "matched_quantity",
            "weighted_yes_ask",
            "weighted_no_ask",
            "gross_acquisition_cost",
            "gross_settlement_value",
            "gross_complement_gap",
            "yes_modeled_taker_fee",
            "no_modeled_taker_fee",
            "modeled_taker_fees",
            "conservative_rounding_reserve",
            "total_modeled_acquisition_cost",
            "conservative_net_lower_bound",
            "net_lower_bound_per_contract",
            "minimum_visible_matched_size",
            "fee_multiplier",
            "matched_quantity_delta",
            "gross_complement_gap_delta",
            "modeled_taker_fees_delta",
            "conservative_net_lower_bound_delta",
            "net_lower_bound_per_contract_delta",
            "weighted_yes_ask_delta",
            "weighted_no_ask_delta",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name)))
        if self.matched_quantity is not None and self.matched_quantity < 0:
            raise ValueValidationError("history matched quantity must not be negative")
        if self.minimum_visible_matched_size is not None and self.minimum_visible_matched_size < 0:
            raise ValueValidationError("history visible size must not be negative")
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class OrderbookHistoryAnalysis:
    analysis_id: str
    provider: PublicMarketProvider
    provider_market_ticker: str
    history_evaluator_version: str
    first_orderbook_snapshot_id: str
    last_orderbook_snapshot_id: str
    requested_maximum_observation_count: int
    included_observation_count: int
    candidate_observation_count: int
    no_candidate_observation_count: int
    ineligible_observation_count: int
    invalid_observation_count: int
    continuity_segment_count: int
    evidence_break_count: int
    observation_gap_count: int
    current_candidate_streak_observation_count: int
    longest_candidate_streak_observation_count: int
    current_candidate_streak_duration: timedelta | None
    longest_candidate_streak_duration: timedelta | None
    first_candidate_observed_at: datetime | None
    latest_candidate_observed_at: datetime | None
    current_matched_quantity: Decimal | None
    maximum_compatible_matched_quantity: Decimal | None
    current_conservative_net_lower_bound: Decimal | None
    maximum_compatible_conservative_net_lower_bound: Decimal | None
    minimum_lower_bound_current_streak: Decimal | None
    current_net_lower_bound_per_contract: Decimal | None
    maximum_observation_gap: timedelta
    continuity_gap_threshold: timedelta
    state: OrderbookHistoryAnalysisState
    canonical_configuration_hash: ContentHash
    canonical_input_hash: ContentHash
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.analysis_id or not self.provider_market_ticker:
            raise ValueValidationError("orderbook history analysis identities must not be empty")
        if self.provider is not PublicMarketProvider.KALSHI:
            raise ValueValidationError("orderbook history provider must be Kalshi")
        if not 3 <= self.requested_maximum_observation_count <= 50:
            raise ValueValidationError("history observation bound must be between three and fifty")
        if not 3 <= self.included_observation_count <= self.requested_maximum_observation_count:
            raise ValueValidationError("history included observation count is invalid")
        if self.maximum_observation_gap < timedelta(
            0
        ) or self.continuity_gap_threshold <= timedelta(0):
            raise ValueValidationError("history gap values are invalid")
        for name in (
            "candidate_observation_count",
            "no_candidate_observation_count",
            "ineligible_observation_count",
            "invalid_observation_count",
            "continuity_segment_count",
            "evidence_break_count",
            "observation_gap_count",
            "current_candidate_streak_observation_count",
            "longest_candidate_streak_observation_count",
        ):
            if getattr(self, name) < 0:
                raise ValueValidationError("history counts must not be negative")
        for name in (
            "current_matched_quantity",
            "maximum_compatible_matched_quantity",
            "current_conservative_net_lower_bound",
            "maximum_compatible_conservative_net_lower_bound",
            "minimum_lower_bound_current_streak",
            "current_net_lower_bound_per_contract",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name)))
        if self.first_candidate_observed_at is not None:
            object.__setattr__(
                self,
                "first_candidate_observed_at",
                _aware(self.first_candidate_observed_at, "first_candidate_observed_at"),
            )
        if self.latest_candidate_observed_at is not None:
            object.__setattr__(
                self,
                "latest_candidate_observed_at",
                _aware(self.latest_candidate_observed_at, "latest_candidate_observed_at"),
            )
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))

    @property
    def canonical_result_hash(self) -> ContentHash:
        payload = {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name
            not in {
                "current_candidate_streak_duration",
                "longest_candidate_streak_duration",
                "maximum_observation_gap",
                "continuity_gap_threshold",
            }
        }
        payload["current_streak_duration_microseconds"] = _duration_microseconds(
            self.current_candidate_streak_duration
        )
        payload["longest_streak_duration_microseconds"] = _duration_microseconds(
            self.longest_candidate_streak_duration
        )
        payload["maximum_observation_gap_microseconds"] = _duration_microseconds(
            self.maximum_observation_gap
        )
        payload["continuity_gap_threshold_microseconds"] = _duration_microseconds(
            self.continuity_gap_threshold
        )
        return ContentHash(content_digest(payload))
