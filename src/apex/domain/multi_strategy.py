"""Pure evidence rules for a bounded multi-strategy SHADOW campaign.

This module deliberately has no provider, persistence, campaign, allocator, or
execution dependency.  It labels agreement and hypothetical capital planning
evidence; it never combines probabilities or grants order authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from apex.domain.values import ContentHash, ValueValidationError, content_digest, parse_decimal


class MultiStrategyCycleState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class PredictionComparisonState(StrEnum):
    CONSENSUS_ONLY = "CONSENSUS_ONLY"
    SINGLE_STRATEGY = "SINGLE_STRATEGY"
    STRATEGIES_AGREE = "STRATEGIES_AGREE"
    STRATEGIES_DISAGREE = "STRATEGIES_DISAGREE"
    INCOMPATIBLE_EVIDENCE = "INCOMPATIBLE_EVIDENCE"
    INCOMPATIBLE_TIME = "INCOMPATIBLE_TIME"
    INVALID = "INVALID"


class StrategyConflictType(StrEnum):
    SAME_DIRECTION_DUPLICATE = "SAME_DIRECTION_DUPLICATE"
    OPPOSITE_DIRECTION = "OPPOSITE_DIRECTION"


class StrategyConflictResolution(StrEnum):
    GROUP_FOR_CAP = "GROUP_FOR_CAP"
    REJECT_BOTH_DIRECTIONAL_CONFLICT = "REJECT_BOTH_DIRECTIONAL_CONFLICT"


class ShadowCapitalAllocationState(StrEnum):
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"


def _utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _text(value: str, *, label: str, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueValidationError(f"{label} must be non-empty and bounded")
    return value


@dataclass(frozen=True, slots=True)
class ShadowMarketCohortConfiguration:
    """Bounded public-evidence collection settings shared by all strategies."""

    maximum_pages: int = 3
    maximum_inspected_markets: int = 300
    maximum_enrichments: int = 40
    selected_market_count: int = 20
    maximum_markets_per_event: int = 2
    maximum_markets_per_series: int = 4
    required_observations: int = 4
    maximum_observation_age: timedelta = timedelta(seconds=120)

    def __post_init__(self) -> None:
        if not 1 <= self.maximum_pages <= 5:
            raise ValueValidationError("cohort maximum pages must be between one and five")
        if not 20 <= self.maximum_inspected_markets <= 500:
            raise ValueValidationError("cohort inspected markets must be between 20 and 500")
        if not 3 <= self.maximum_enrichments <= 80:
            raise ValueValidationError("cohort enrichments must be between three and 80")
        if not 3 <= self.selected_market_count <= 50:
            raise ValueValidationError("cohort selected markets must be between three and 50")
        if self.selected_market_count > self.maximum_enrichments:
            raise ValueValidationError("cohort selection cannot exceed enrichments")
        if not 1 <= self.maximum_markets_per_event <= 5:
            raise ValueValidationError("cohort per-event cap must be between one and five")
        if not 1 <= self.maximum_markets_per_series <= 10:
            raise ValueValidationError("cohort per-series cap must be between one and ten")
        if not 3 <= self.required_observations <= 20:
            raise ValueValidationError("cohort observations must be between three and 20")
        if self.maximum_observation_age <= timedelta():
            raise ValueValidationError("cohort observation age must be positive")

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(
            content_digest(
                {
                    "maximum_pages": self.maximum_pages,
                    "maximum_inspected_markets": self.maximum_inspected_markets,
                    "maximum_enrichments": self.maximum_enrichments,
                    "selected_market_count": self.selected_market_count,
                    "maximum_markets_per_event": self.maximum_markets_per_event,
                    "maximum_markets_per_series": self.maximum_markets_per_series,
                    "required_observations": self.required_observations,
                    "maximum_observation_age_seconds": int(
                        self.maximum_observation_age.total_seconds()
                    ),
                }
            )
        )


@dataclass(frozen=True, slots=True)
class EnrichedShadowMarket:
    """One exact public market admitted to a shared non-financial cohort."""

    market_snapshot_id: str
    provider: str
    market_ticker: str
    event_ticker: str
    series_ticker: str
    rule_content_hash: ContentHash
    lifecycle_state: str
    observed_at: datetime
    available_at: datetime
    close_at: datetime
    market_lineage_hash: ContentHash
    event_lineage_hash: ContentHash
    series_lineage_hash: ContentHash
    fee_lineage_hash: ContentHash | None

    def __post_init__(self) -> None:
        for value, label in (
            (self.market_snapshot_id, "market snapshot ID"),
            (self.provider, "provider"),
            (self.market_ticker, "market ticker"),
            (self.event_ticker, "event ticker"),
            (self.series_ticker, "series ticker"),
            (self.lifecycle_state, "lifecycle state"),
        ):
            _text(value, label=label)
        observed = _utc(self.observed_at, label="market observation")
        available = _utc(self.available_at, label="market availability")
        close = _utc(self.close_at, label="market close")
        if available < observed or close <= available:
            raise ValueValidationError("enriched market timestamps are inconsistent")
        if self.lifecycle_state != "open":
            raise ValueValidationError("shared cohort supports only open markets")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "close_at", close)

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class CompatiblePredictionEvidence:
    """Minimal immutable prediction projection used for comparison only."""

    prediction_id: str
    strategy_id: str
    strategy_version: str
    market_ticker: str
    event_ticker: str | None
    decision_at: datetime
    market_yes_probability: Decimal
    point_yes_probability: Decimal
    conservative_yes_probability: Decimal
    market_evidence_id: str
    feature_snapshot_id: str | None
    configuration_version: str
    rule_content_hash: ContentHash | None

    def __post_init__(self) -> None:
        for value, label in (
            (self.prediction_id, "prediction ID"),
            (self.strategy_id, "strategy ID"),
            (self.strategy_version, "strategy version"),
            (self.market_ticker, "market ticker"),
            (self.market_evidence_id, "market evidence ID"),
            (self.configuration_version, "configuration version"),
        ):
            _text(value, label=label)
        object.__setattr__(self, "decision_at", _utc(self.decision_at, label="prediction time"))
        for name in (
            "market_yes_probability",
            "point_yes_probability",
            "conservative_yes_probability",
        ):
            probability = parse_decimal(getattr(self, name))
            if not Decimal(0) <= probability <= Decimal(1):
                raise ValueValidationError(f"{name} must be in the binary range")
            object.__setattr__(self, name, probability)


@dataclass(frozen=True, slots=True)
class CrossStrategyComparison:
    comparison_id: str
    cycle_id: str
    market_ticker: str
    event_ticker: str | None
    consensus_prediction_id: str | None
    microstructure_prediction_id: str | None
    dynamics_prediction_id: str | None
    state: PredictionComparisonState
    maximum_prediction_skew: timedelta | None
    consensus_yes_probability: Decimal | None
    microstructure_yes_probability: Decimal | None
    microstructure_conservative_yes_probability: Decimal | None
    dynamics_yes_probability: Decimal | None
    dynamics_conservative_yes_probability: Decimal | None
    microstructure_consensus_difference: Decimal | None
    dynamics_consensus_difference: Decimal | None
    microstructure_dynamics_difference: Decimal | None
    directional_agreement: bool | None
    conservative_directional_agreement: bool | None
    reason_codes: tuple[str, ...]
    canonical_hash: ContentHash


def compare_prediction_evidence(
    *,
    cycle_id: str,
    values: tuple[CompatiblePredictionEvidence, ...],
    maximum_prediction_skew: timedelta = timedelta(minutes=5),
) -> CrossStrategyComparison:
    """Compare exact-market forecasts without averaging or confidence uplift."""

    if not 1 <= int(maximum_prediction_skew.total_seconds() // 60) <= 30:
        raise ValueValidationError("prediction skew must be between one and 30 minutes")
    if not values:
        raise ValueValidationError("comparison requires at least one prediction")
    ordered = tuple(sorted(values, key=lambda value: (value.strategy_id, value.prediction_id)))
    market_tickers = {value.market_ticker for value in ordered}
    event_tickers = {value.event_ticker for value in ordered}
    rules = {
        value.rule_content_hash.value if value.rule_content_hash else None for value in ordered
    }
    state: PredictionComparisonState
    reasons: tuple[str, ...]
    if len(market_tickers) != 1 or len(event_tickers) != 1 or len(rules) != 1 or None in rules:
        state = PredictionComparisonState.INCOMPATIBLE_EVIDENCE
        reasons = ("EXACT_MARKET_EVENT_OR_RULE_LINEAGE_MISMATCH",)
    else:
        times = tuple(value.decision_at for value in ordered)
        skew = max(times) - min(times)
        if skew > maximum_prediction_skew:
            state = PredictionComparisonState.INCOMPATIBLE_TIME
            reasons = ("PREDICTION_TIME_SKEW_EXCEEDED",)
        elif len(ordered) == 1:
            state = (
                PredictionComparisonState.CONSENSUS_ONLY
                if ordered[0].strategy_id == "market-consensus-baseline-v1"
                else PredictionComparisonState.SINGLE_STRATEGY
            )
            reasons = ("ONE_COMPATIBLE_PREDICTION",)
        else:
            non_consensus = tuple(
                value for value in ordered if value.strategy_id != "market-consensus-baseline-v1"
            )
            signs = {
                (value.conservative_yes_probability > value.market_yes_probability)
                - (value.conservative_yes_probability < value.market_yes_probability)
                for value in non_consensus
            }
            state = (
                PredictionComparisonState.STRATEGIES_AGREE
                if len(signs) <= 1
                else PredictionComparisonState.STRATEGIES_DISAGREE
            )
            reasons = (
                "DIRECTIONAL_CONSENSERVATIVE_AGREEMENT"
                if state is PredictionComparisonState.STRATEGIES_AGREE
                else "DIRECTIONAL_CONSERVATIVE_DISAGREEMENT",
            )
    by_strategy = {value.strategy_id: value for value in ordered}
    consensus = by_strategy.get("market-consensus-baseline-v1")
    micro = by_strategy.get("kalshi-orderbook-microstructure-v1")
    dynamics = by_strategy.get("kalshi-market-dynamics-v1")
    consensus_value = consensus.market_yes_probability if consensus is not None else None
    micro_diff = (
        micro.point_yes_probability - consensus_value
        if micro is not None and consensus_value is not None
        else None
    )
    dynamics_diff = (
        dynamics.point_yes_probability - consensus_value
        if dynamics is not None and consensus_value is not None
        else None
    )
    peer_diff = (
        micro.point_yes_probability - dynamics.point_yes_probability
        if micro is not None and dynamics is not None
        else None
    )
    skew = (
        max(value.decision_at for value in ordered) - min(value.decision_at for value in ordered)
        if len(ordered) > 1
        else timedelta()
    )
    payload = {
        "cycle_id": cycle_id,
        "prediction_ids": tuple(value.prediction_id for value in ordered),
        "maximum_prediction_skew_seconds": int(maximum_prediction_skew.total_seconds()),
        "state": state,
        "reasons": reasons,
    }
    return CrossStrategyComparison(
        comparison_id="comparison_" + content_digest(payload)[:40],
        cycle_id=_text(cycle_id, label="cycle ID", maximum=160),
        market_ticker=ordered[0].market_ticker,
        event_ticker=ordered[0].event_ticker,
        consensus_prediction_id=consensus.prediction_id if consensus is not None else None,
        microstructure_prediction_id=micro.prediction_id if micro is not None else None,
        dynamics_prediction_id=dynamics.prediction_id if dynamics is not None else None,
        state=state,
        maximum_prediction_skew=skew,
        consensus_yes_probability=consensus_value,
        microstructure_yes_probability=micro.point_yes_probability if micro is not None else None,
        microstructure_conservative_yes_probability=(
            micro.conservative_yes_probability if micro is not None else None
        ),
        dynamics_yes_probability=dynamics.point_yes_probability if dynamics is not None else None,
        dynamics_conservative_yes_probability=(
            dynamics.conservative_yes_probability if dynamics is not None else None
        ),
        microstructure_consensus_difference=micro_diff,
        dynamics_consensus_difference=dynamics_diff,
        microstructure_dynamics_difference=peer_diff,
        directional_agreement=(state is PredictionComparisonState.STRATEGIES_AGREE),
        conservative_directional_agreement=(state is PredictionComparisonState.STRATEGIES_AGREE),
        reason_codes=reasons,
        canonical_hash=ContentHash(content_digest(payload)),
    )
