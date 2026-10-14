"""Pure, exact temporal market-price features for a forward SHADOW strategy.

This module deliberately does not know about orderbook depth, microprice,
campaigns, execution, or persistence.  It accepts only retained binary market
snapshots and exposes reproducible evidence-quality decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import cast

from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import (
    ContentHash,
    ValueValidationError,
    content_digest,
    deterministic_id,
    parse_decimal,
)

STRATEGY_ID = "kalshi-market-dynamics-v1"
STRATEGY_VERSION = "v1"
CONFIGURATION_VERSION = "market-dynamics-shadow-v1"
ONE = Decimal(1)


class MarketDynamicsFeatureState(StrEnum):
    VALID = "VALID"
    INELIGIBLE = "INELIGIBLE"
    INVALID = "INVALID"


class MarketDynamicsReasonCode(StrEnum):
    FEATURES_VALID = "FEATURES_VALID"
    MARKET_NOT_KALSHI = "MARKET_NOT_KALSHI"
    MARKET_NOT_ACTIVE = "MARKET_NOT_ACTIVE"
    MARKET_IDENTITY_MISSING = "MARKET_IDENTITY_MISSING"
    EVENT_OR_SERIES_LINEAGE_MISSING = "EVENT_OR_SERIES_LINEAGE_MISSING"
    RULE_EVIDENCE_MISSING = "RULE_EVIDENCE_MISSING"
    INSUFFICIENT_OBSERVATIONS = "INSUFFICIENT_OBSERVATIONS"
    HISTORY_DURATION_TOO_SHORT = "HISTORY_DURATION_TOO_SHORT"
    HISTORY_DURATION_TOO_LONG = "HISTORY_DURATION_TOO_LONG"
    OBSERVATION_ORDER_CONFLICT = "OBSERVATION_ORDER_CONFLICT"
    OBSERVATION_DUPLICATE = "OBSERVATION_DUPLICATE"
    OBSERVATION_GAP_EXCEEDED = "OBSERVATION_GAP_EXCEEDED"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    MARKET_LINEAGE_CHANGED = "MARKET_LINEAGE_CHANGED"
    RULE_CHANGED = "RULE_CHANGED"
    SERIES_CHANGED = "SERIES_CHANGED"
    LIFECYCLE_INCOMPATIBLE = "LIFECYCLE_INCOMPATIBLE"
    QUOTE_MISSING_SIDE = "QUOTE_MISSING_SIDE"
    QUOTE_MALFORMED = "QUOTE_MALFORMED"
    QUOTE_CROSSED = "QUOTE_CROSSED"
    SPREAD_EXCEEDS_MAXIMUM = "SPREAD_EXCEEDS_MAXIMUM"
    ZERO_USABLE_MOVEMENT = "ZERO_USABLE_MOVEMENT"


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _fixed(value: Decimal) -> Decimal:
    return parse_decimal(
        format(value.quantize(Decimal("0.00000001")).normalize(), "f"), allow_negative=True
    )


def _clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return max(lower, min(upper, value))


@dataclass(frozen=True, slots=True)
class MarketDynamicsStrategyConfig:
    """Fixed conservative coefficients, never fitted to retained outcomes."""

    strategy_id: str = STRATEGY_ID
    strategy_version: str = STRATEGY_VERSION
    maximum_markets_per_invocation: int = 20
    minimum_observations: int = 3
    maximum_observations: int = 20
    minimum_history_duration: timedelta = timedelta(seconds=20)
    maximum_history_duration: timedelta = timedelta(hours=4)
    maximum_observation_age: timedelta = timedelta(seconds=120)
    maximum_adjacent_observation_gap: timedelta = timedelta(seconds=300)
    maximum_latest_spread: Decimal = Decimal("0.20")
    minimum_nonzero_price_moves: int = 1
    window_momentum_scale: Decimal = Decimal("0.10")
    recent_momentum_scale: Decimal = Decimal("0.05")
    window_momentum_weight: Decimal = Decimal("0.025")
    recent_momentum_weight: Decimal = Decimal("0.025")
    directional_persistence_weight: Decimal = Decimal("0.02")
    maximum_probability_adjustment: Decimal = Decimal("0.05")
    conservative_shrinkage_factor: Decimal = Decimal("0.50")
    minimum_conservative_advantage: Decimal = Decimal("0.01")
    expected_fee_model: str = "kalshi-quadratic-taker-v1"
    slippage_reserve: Decimal = Decimal("0.01")
    execution_uncertainty_reserve: Decimal = Decimal("0.01")
    evidence_tier: str = "RESEARCH"
    configuration_version: str = CONFIGURATION_VERSION

    def __post_init__(self) -> None:
        if self.strategy_id != STRATEGY_ID or self.strategy_version != STRATEGY_VERSION:
            raise ValueValidationError("market dynamics strategy identity is fixed")
        if not 1 <= self.maximum_markets_per_invocation <= 100:
            raise ValueValidationError("maximum markets must be between one and 100")
        if not 3 <= self.minimum_observations <= self.maximum_observations <= 50:
            raise ValueValidationError("observation bounds must be between three and 50")
        if (
            self.minimum_history_duration <= timedelta()
            or self.maximum_history_duration < self.minimum_history_duration
        ):
            raise ValueValidationError("history duration bounds are invalid")
        if (
            self.maximum_observation_age <= timedelta()
            or self.maximum_adjacent_observation_gap <= timedelta()
        ):
            raise ValueValidationError("observation time bounds must be positive")
        if self.minimum_nonzero_price_moves < 1:
            raise ValueValidationError("minimum nonzero price moves must be positive")
        for name in (
            "maximum_latest_spread",
            "window_momentum_scale",
            "recent_momentum_scale",
            "window_momentum_weight",
            "recent_momentum_weight",
            "directional_persistence_weight",
            "maximum_probability_adjustment",
            "conservative_shrinkage_factor",
            "minimum_conservative_advantage",
            "slippage_reserve",
            "execution_uncertainty_reserve",
        ):
            value = parse_decimal(getattr(self, name))
            if value < 0:
                raise ValueValidationError(f"{name} must not be negative")
            object.__setattr__(self, name, value)
        if not Decimal(0) < self.maximum_latest_spread <= ONE:
            raise ValueValidationError("maximum latest spread must be in the binary range")
        if self.window_momentum_scale <= 0 or self.recent_momentum_scale <= 0:
            raise ValueValidationError("momentum scales must be positive")
        if self.maximum_probability_adjustment > ONE:
            raise ValueValidationError("maximum adjustment must be in the binary range")
        if not Decimal(0) <= self.conservative_shrinkage_factor <= Decimal("0.50"):
            raise ValueValidationError("conservative shrinkage must be between zero and 0.50")
        if not self.expected_fee_model or not self.configuration_version:
            raise ValueValidationError("fee model and configuration version are required")

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(
            content_digest(
                {
                    "strategy_id": self.strategy_id,
                    "strategy_version": self.strategy_version,
                    "maximum_markets_per_invocation": self.maximum_markets_per_invocation,
                    "minimum_observations": self.minimum_observations,
                    "maximum_observations": self.maximum_observations,
                    "minimum_history_duration_seconds": int(
                        self.minimum_history_duration.total_seconds()
                    ),
                    "maximum_history_duration_seconds": int(
                        self.maximum_history_duration.total_seconds()
                    ),
                    "maximum_observation_age_seconds": int(
                        self.maximum_observation_age.total_seconds()
                    ),
                    "maximum_adjacent_observation_gap_seconds": int(
                        self.maximum_adjacent_observation_gap.total_seconds()
                    ),
                    "maximum_latest_spread": self.maximum_latest_spread,
                    "minimum_nonzero_price_moves": self.minimum_nonzero_price_moves,
                    "window_momentum_scale": self.window_momentum_scale,
                    "recent_momentum_scale": self.recent_momentum_scale,
                    "window_momentum_weight": self.window_momentum_weight,
                    "recent_momentum_weight": self.recent_momentum_weight,
                    "directional_persistence_weight": self.directional_persistence_weight,
                    "maximum_probability_adjustment": self.maximum_probability_adjustment,
                    "conservative_shrinkage_factor": self.conservative_shrinkage_factor,
                    "minimum_conservative_advantage": self.minimum_conservative_advantage,
                    "expected_fee_model": self.expected_fee_model,
                    "slippage_reserve": self.slippage_reserve,
                    "execution_uncertainty_reserve": self.execution_uncertainty_reserve,
                    "evidence_tier": self.evidence_tier,
                    "configuration_version": self.configuration_version,
                }
            )
        )


@dataclass(frozen=True, slots=True)
class MarketDynamicsObservation:
    snapshot_id: str
    provider: PublicMarketProvider
    provider_market_ticker: str
    provider_event_ticker: str | None
    series_ticker: str | None
    observed_at: datetime
    available_at: datetime
    close_at: datetime | None
    status: str | None
    rule_content_hash: ContentHash | None
    yes_bid: Decimal | None
    yes_ask: Decimal | None

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.provider_market_ticker:
            raise ValueValidationError("market dynamics observation identity is required")
        for name in ("observed_at", "available_at"):
            object.__setattr__(self, name, _aware(getattr(self, name), name))
        if self.close_at is not None:
            object.__setattr__(self, "close_at", _aware(self.close_at, "close_at"))


@dataclass(frozen=True, slots=True)
class MarketDynamicsMarketEvidence:
    market_snapshot_id: str
    provider: PublicMarketProvider
    provider_market_ticker: str
    provider_event_ticker: str | None
    series_ticker: str | None
    active: bool
    observations: tuple[MarketDynamicsObservation, ...]


@dataclass(frozen=True, slots=True)
class MarketDynamicsFeatureSnapshot:
    feature_snapshot_id: str
    strategy_id: str
    strategy_version: str
    market_snapshot_id: str
    first_market_snapshot_id: str | None
    latest_market_snapshot_id: str | None
    provider_market_ticker: str
    provider_event_ticker: str | None
    series_ticker: str | None
    rule_content_hash: ContentHash | None
    decision_at: datetime
    observation_count: int
    first_observed_at: datetime | None
    latest_observed_at: datetime | None
    history_duration: timedelta | None
    maximum_adjacent_gap: timedelta | None
    minimum_adjacent_gap: timedelta | None
    earliest_midpoint: Decimal | None
    latest_midpoint: Decimal | None
    earliest_spread: Decimal | None
    latest_spread: Decimal | None
    window_momentum: Decimal | None
    recent_momentum: Decimal | None
    positive_move_count: int
    negative_move_count: int
    zero_move_count: int
    nonzero_move_count: int
    signed_directional_persistence: Decimal | None
    normalized_window_momentum: Decimal | None
    normalized_recent_momentum: Decimal | None
    window_momentum_contribution: Decimal | None
    recent_momentum_contribution: Decimal | None
    directional_persistence_contribution: Decimal | None
    raw_adjustment: Decimal | None
    clamped_adjustment: Decimal | None
    feature_state: MarketDynamicsFeatureState
    reason_codes: tuple[str, ...]
    configuration_hash: ContentHash
    canonical_input_hash: ContentHash
    canonical_feature_hash: ContentHash

    @property
    def valid(self) -> bool:
        return self.feature_state is MarketDynamicsFeatureState.VALID


@dataclass(frozen=True, slots=True)
class MarketDynamicsProbability:
    market_yes_probability: Decimal
    point_yes_probability: Decimal
    conservative_yes_probability: Decimal
    window_momentum_contribution: Decimal
    recent_momentum_contribution: Decimal
    directional_persistence_contribution: Decimal
    raw_adjustment: Decimal
    clamped_adjustment: Decimal


def _feature(
    market: MarketDynamicsMarketEvidence,
    config: MarketDynamicsStrategyConfig,
    decision_at: datetime,
    state: MarketDynamicsFeatureState,
    reasons: tuple[MarketDynamicsReasonCode, ...],
    observations: tuple[MarketDynamicsObservation, ...],
    values: dict[str, object] | None = None,
) -> MarketDynamicsFeatureSnapshot:
    first = observations[0] if observations else None
    latest = observations[-1] if observations else None
    value = values or {}
    reasons_text = tuple(sorted(item.value for item in reasons))
    canonical_values = {
        key: int(item.total_seconds()) if isinstance(item, timedelta) else item
        for key, item in value.items()
    }
    payload = {
        "market_snapshot_id": market.market_snapshot_id,
        "snapshots": tuple(item.snapshot_id for item in observations),
        "configuration_hash": config.canonical_hash.value,
        "decision_at": decision_at,
        "state": state.value,
        "reasons": reasons_text,
        "values": canonical_values,
    }
    input_hash = ContentHash(content_digest(payload))
    return MarketDynamicsFeatureSnapshot(
        feature_snapshot_id=deterministic_id("market_dynamics_feature", payload),
        strategy_id=config.strategy_id,
        strategy_version=config.strategy_version,
        market_snapshot_id=market.market_snapshot_id,
        first_market_snapshot_id=first.snapshot_id if first else None,
        latest_market_snapshot_id=latest.snapshot_id if latest else None,
        provider_market_ticker=market.provider_market_ticker,
        provider_event_ticker=market.provider_event_ticker,
        series_ticker=market.series_ticker,
        rule_content_hash=latest.rule_content_hash if latest else None,
        decision_at=decision_at,
        observation_count=len(observations),
        first_observed_at=first.observed_at if first else None,
        latest_observed_at=latest.observed_at if latest else None,
        history_duration=cast(timedelta | None, value.get("history_duration")),
        maximum_adjacent_gap=cast(timedelta | None, value.get("maximum_adjacent_gap")),
        minimum_adjacent_gap=cast(timedelta | None, value.get("minimum_adjacent_gap")),
        earliest_midpoint=cast(Decimal | None, value.get("earliest_midpoint")),
        latest_midpoint=cast(Decimal | None, value.get("latest_midpoint")),
        earliest_spread=cast(Decimal | None, value.get("earliest_spread")),
        latest_spread=cast(Decimal | None, value.get("latest_spread")),
        window_momentum=cast(Decimal | None, value.get("window_momentum")),
        recent_momentum=cast(Decimal | None, value.get("recent_momentum")),
        positive_move_count=cast(int, value.get("positive_move_count", 0)),
        negative_move_count=cast(int, value.get("negative_move_count", 0)),
        zero_move_count=cast(int, value.get("zero_move_count", 0)),
        nonzero_move_count=cast(int, value.get("nonzero_move_count", 0)),
        signed_directional_persistence=cast(
            Decimal | None, value.get("signed_directional_persistence")
        ),
        normalized_window_momentum=cast(Decimal | None, value.get("normalized_window_momentum")),
        normalized_recent_momentum=cast(Decimal | None, value.get("normalized_recent_momentum")),
        window_momentum_contribution=cast(
            Decimal | None, value.get("window_momentum_contribution")
        ),
        recent_momentum_contribution=cast(
            Decimal | None, value.get("recent_momentum_contribution")
        ),
        directional_persistence_contribution=cast(
            Decimal | None, value.get("directional_persistence_contribution")
        ),
        raw_adjustment=cast(Decimal | None, value.get("raw_adjustment")),
        clamped_adjustment=cast(Decimal | None, value.get("clamped_adjustment")),
        feature_state=state,
        reason_codes=reasons_text,
        configuration_hash=config.canonical_hash,
        canonical_input_hash=input_hash,
        canonical_feature_hash=ContentHash(
            content_digest({"input": input_hash.value, "values": canonical_values})
        ),
    )


def evaluate_market_dynamics_features(
    *,
    market: MarketDynamicsMarketEvidence,
    configuration: MarketDynamicsStrategyConfig,
    decision_at: datetime,
) -> MarketDynamicsFeatureSnapshot:
    """Evaluate all supplied observations; no incompatible row is silently dropped."""
    decision = _aware(decision_at, "decision_at")
    observations = tuple(
        sorted(market.observations, key=lambda item: (item.observed_at, item.snapshot_id))
    )
    ineligible: list[MarketDynamicsReasonCode] = []
    if market.provider is not PublicMarketProvider.KALSHI:
        ineligible.append(MarketDynamicsReasonCode.MARKET_NOT_KALSHI)
    if not market.active:
        ineligible.append(MarketDynamicsReasonCode.MARKET_NOT_ACTIVE)
    if not market.provider_market_ticker:
        ineligible.append(MarketDynamicsReasonCode.MARKET_IDENTITY_MISSING)
    if not market.provider_event_ticker or not market.series_ticker:
        ineligible.append(MarketDynamicsReasonCode.EVENT_OR_SERIES_LINEAGE_MISSING)
    if len(observations) < configuration.minimum_observations:
        ineligible.append(MarketDynamicsReasonCode.INSUFFICIENT_OBSERVATIONS)
    if ineligible:
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INELIGIBLE,
            tuple(ineligible),
            observations,
        )
    if len(observations) > configuration.maximum_observations:
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INVALID,
            (MarketDynamicsReasonCode.OBSERVATION_ORDER_CONFLICT,),
            observations,
        )
    times = tuple(item.observed_at for item in observations)
    if any(left > right for left, right in pairwise(times)):
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INVALID,
            (MarketDynamicsReasonCode.OBSERVATION_ORDER_CONFLICT,),
            observations,
        )
    if any(left == right for left, right in pairwise(times)) or len(
        {item.snapshot_id for item in observations}
    ) != len(observations):
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INVALID,
            (MarketDynamicsReasonCode.OBSERVATION_DUPLICATE,),
            observations,
        )
    duration = times[-1] - times[0]
    if duration < configuration.minimum_history_duration:
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INELIGIBLE,
            (MarketDynamicsReasonCode.HISTORY_DURATION_TOO_SHORT,),
            observations,
        )
    if duration > configuration.maximum_history_duration:
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INELIGIBLE,
            (MarketDynamicsReasonCode.HISTORY_DURATION_TOO_LONG,),
            observations,
        )
    gaps = tuple(right - left for left, right in pairwise(times))
    if any(gap > configuration.maximum_adjacent_observation_gap for gap in gaps):
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INELIGIBLE,
            (MarketDynamicsReasonCode.OBSERVATION_GAP_EXCEEDED,),
            observations,
        )
    if decision - times[-1] > configuration.maximum_observation_age:
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INELIGIBLE,
            (MarketDynamicsReasonCode.EVIDENCE_STALE,),
            observations,
        )
    first = observations[0]
    if any(
        item.provider is not PublicMarketProvider.KALSHI
        or item.provider_market_ticker != market.provider_market_ticker
        or item.provider_event_ticker != market.provider_event_ticker
        for item in observations
    ):
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INVALID,
            (MarketDynamicsReasonCode.MARKET_LINEAGE_CHANGED,),
            observations,
        )
    if any(item.series_ticker != market.series_ticker for item in observations):
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INVALID,
            (MarketDynamicsReasonCode.SERIES_CHANGED,),
            observations,
        )
    if first.rule_content_hash is None:
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INELIGIBLE,
            (MarketDynamicsReasonCode.RULE_EVIDENCE_MISSING,),
            observations,
        )
    if any(item.rule_content_hash != first.rule_content_hash for item in observations):
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INVALID,
            (MarketDynamicsReasonCode.RULE_CHANGED,),
            observations,
        )
    if any(
        item.status != "open" or item.close_at is None or item.close_at <= item.observed_at
        for item in observations
    ):
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INELIGIBLE,
            (MarketDynamicsReasonCode.LIFECYCLE_INCOMPATIBLE,),
            observations,
        )
    if any(item.yes_bid is None or item.yes_ask is None for item in observations):
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INELIGIBLE,
            (MarketDynamicsReasonCode.QUOTE_MISSING_SIDE,),
            observations,
        )
    bids = tuple(parse_decimal(item.yes_bid) for item in observations if item.yes_bid is not None)
    asks = tuple(parse_decimal(item.yes_ask) for item in observations if item.yes_ask is not None)
    if any(not Decimal(0) <= value <= ONE for value in (*bids, *asks)):
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INVALID,
            (MarketDynamicsReasonCode.QUOTE_MALFORMED,),
            observations,
        )
    if any(bid > ask for bid, ask in zip(bids, asks, strict=True)):
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INVALID,
            (MarketDynamicsReasonCode.QUOTE_CROSSED,),
            observations,
        )
    midpoints = tuple(_fixed((bid + ask) / Decimal(2)) for bid, ask in zip(bids, asks, strict=True))
    spreads = tuple(_fixed(ask - bid) for bid, ask in zip(bids, asks, strict=True))
    if spreads[-1] > configuration.maximum_latest_spread:
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INELIGIBLE,
            (MarketDynamicsReasonCode.SPREAD_EXCEEDS_MAXIMUM,),
            observations,
        )
    moves = tuple(_fixed(right - left) for left, right in pairwise(midpoints))
    positive = sum(move > 0 for move in moves)
    negative = sum(move < 0 for move in moves)
    zero = sum(move == 0 for move in moves)
    nonzero = positive + negative
    window = _fixed(midpoints[-1] - midpoints[0])
    recent = moves[-1]
    if nonzero < configuration.minimum_nonzero_price_moves or window == 0:
        return _feature(
            market,
            configuration,
            decision,
            MarketDynamicsFeatureState.INELIGIBLE,
            (MarketDynamicsReasonCode.ZERO_USABLE_MOVEMENT,),
            observations,
        )
    persistence = _fixed(Decimal(positive if window > 0 else -negative) / Decimal(nonzero))
    normalized_window = _fixed(
        _clamp(window / configuration.window_momentum_scale, Decimal(-1), Decimal(1))
    )
    normalized_recent = _fixed(
        _clamp(recent / configuration.recent_momentum_scale, Decimal(-1), Decimal(1))
    )
    window_contribution = _fixed(configuration.window_momentum_weight * normalized_window)
    recent_contribution = _fixed(configuration.recent_momentum_weight * normalized_recent)
    persistence_contribution = _fixed(configuration.directional_persistence_weight * persistence)
    raw = _fixed(window_contribution + recent_contribution + persistence_contribution)
    clamped = _fixed(
        _clamp(
            raw,
            -configuration.maximum_probability_adjustment,
            configuration.maximum_probability_adjustment,
        )
    )
    return _feature(
        market,
        configuration,
        decision,
        MarketDynamicsFeatureState.VALID,
        (MarketDynamicsReasonCode.FEATURES_VALID,),
        observations,
        {
            "history_duration": duration,
            "maximum_adjacent_gap": max(gaps),
            "minimum_adjacent_gap": min(gaps),
            "earliest_midpoint": midpoints[0],
            "latest_midpoint": midpoints[-1],
            "earliest_spread": spreads[0],
            "latest_spread": spreads[-1],
            "window_momentum": window,
            "recent_momentum": recent,
            "positive_move_count": positive,
            "negative_move_count": negative,
            "zero_move_count": zero,
            "nonzero_move_count": nonzero,
            "signed_directional_persistence": persistence,
            "normalized_window_momentum": normalized_window,
            "normalized_recent_momentum": normalized_recent,
            "window_momentum_contribution": window_contribution,
            "recent_momentum_contribution": recent_contribution,
            "directional_persistence_contribution": persistence_contribution,
            "raw_adjustment": raw,
            "clamped_adjustment": clamped,
        },
    )


def derive_market_dynamics_probability(
    *, feature: MarketDynamicsFeatureSnapshot, configuration: MarketDynamicsStrategyConfig
) -> MarketDynamicsProbability:
    if not feature.valid or feature.latest_midpoint is None or feature.clamped_adjustment is None:
        raise ValueValidationError("a valid market dynamics feature snapshot is required")
    assert feature.normalized_window_momentum is not None
    assert feature.normalized_recent_momentum is not None
    assert feature.signed_directional_persistence is not None
    baseline = feature.latest_midpoint
    window = _fixed(configuration.window_momentum_weight * feature.normalized_window_momentum)
    recent = _fixed(configuration.recent_momentum_weight * feature.normalized_recent_momentum)
    persistence = _fixed(
        configuration.directional_persistence_weight * feature.signed_directional_persistence
    )
    raw = _fixed(window + recent + persistence)
    clamped = _fixed(
        _clamp(
            raw,
            -configuration.maximum_probability_adjustment,
            configuration.maximum_probability_adjustment,
        )
    )
    point = _fixed(_clamp(baseline + clamped, Decimal("0.01"), Decimal("0.99")))
    conservative = _fixed(
        baseline + configuration.conservative_shrinkage_factor * (point - baseline)
    )
    return MarketDynamicsProbability(
        baseline, point, conservative, window, recent, persistence, raw, clamped
    )
