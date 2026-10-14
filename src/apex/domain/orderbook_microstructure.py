"""Pure, exact feature calculations for retained public orderbooks.

The module is intentionally observation-only: no provider, campaign, order,
position, ledger, or execution interface is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from apex.domain.public_complement import (
    FEE_MODEL_IDENTITY,
    ONE,
    ROUNDING_IDENTITY,
    OrderbookSide,
    PublicOrderbookLevel,
    PublicOrderbookSnapshot,
    SeriesFeeSnapshot,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.published_fee_schedule import (
    KALSHI_PUBLISHED_FEE_SCHEDULE,
    PublishedProviderFeeSchedule,
    reconcile_kalshi_published_fee_schedule,
)
from apex.domain.values import (
    ContentHash,
    ValueValidationError,
    content_digest,
    deterministic_id,
    parse_decimal,
)

STRATEGY_ID = "kalshi-orderbook-microstructure-v1"
STRATEGY_VERSION = "v1"
CONFIGURATION_VERSION = "orderbook-microstructure-shadow-v1"
PUBLISHED_FEE_CONFIGURATION_VERSION = "orderbook-microstructure-shadow-v2-published-fee"


class MicrostructureFeatureState(StrEnum):
    VALID = "VALID"
    INELIGIBLE = "INELIGIBLE"
    INVALID = "INVALID"


class MicrostructureReasonCode(StrEnum):
    FEATURES_VALID = "FEATURES_VALID"
    MARKET_NOT_KALSHI = "MARKET_NOT_KALSHI"
    MARKET_NOT_ACTIVE = "MARKET_NOT_ACTIVE"
    MARKET_IDENTITY_MISSING = "MARKET_IDENTITY_MISSING"
    RULE_EVIDENCE_MISSING = "RULE_EVIDENCE_MISSING"
    SERIES_EVIDENCE_MISSING = "SERIES_EVIDENCE_MISSING"
    INSUFFICIENT_OBSERVATIONS = "INSUFFICIENT_OBSERVATIONS"
    OBSERVATION_ORDER_CONFLICT = "OBSERVATION_ORDER_CONFLICT"
    OBSERVATION_INTERVAL_EXCEEDED = "OBSERVATION_INTERVAL_EXCEEDED"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    ORDERBOOK_INVALID = "ORDERBOOK_INVALID"
    ORDERBOOK_MISSING_SIDE = "ORDERBOOK_MISSING_SIDE"
    ORDERBOOK_CROSSED_OR_CONTRADICTORY = "ORDERBOOK_CROSSED_OR_CONTRADICTORY"
    FEE_EVIDENCE_INCOMPATIBLE = "FEE_EVIDENCE_INCOMPATIBLE"
    PUBLISHED_FEE_TYPE_INCOMPATIBLE = "PUBLISHED_FEE_TYPE_INCOMPATIBLE"
    PUBLISHED_FEE_MULTIPLIER_MISSING = "PUBLISHED_FEE_MULTIPLIER_MISSING"
    PUBLISHED_FEE_EFFECTIVE_TIME_MISSING = "PUBLISHED_FEE_EFFECTIVE_TIME_MISSING"
    PUBLISHED_FEE_EFFECTIVE_TIME_FUTURE = "PUBLISHED_FEE_EFFECTIVE_TIME_FUTURE"
    PUBLISHED_FEE_ROUNDING_EVIDENCE_INCOMPATIBLE = "PUBLISHED_FEE_ROUNDING_EVIDENCE_INCOMPATIBLE"
    VISIBLE_LIQUIDITY_INSUFFICIENT = "VISIBLE_LIQUIDITY_INSUFFICIENT"
    SPREAD_EXCEEDS_MAXIMUM = "SPREAD_EXCEEDS_MAXIMUM"


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return max(lower, min(upper, value))


def _fixed(value: Decimal) -> Decimal:
    """Keep derived public features inside Apex's eight-place fixed-point boundary."""

    return parse_decimal(
        format(value.quantize(Decimal("0.00000001")).normalize(), "f"),
        allow_negative=True,
    )


@dataclass(frozen=True, slots=True)
class OrderbookMicrostructureStrategyConfig:
    """Fixed, inspectable values; every numerical field is an exact Decimal."""

    strategy_id: str = STRATEGY_ID
    strategy_version: str = STRATEGY_VERSION
    maximum_markets_per_invocation: int = 20
    orderbook_depth: int = 5
    minimum_visible_quantity: Decimal = Decimal("1")
    maximum_spread: Decimal = Decimal("0.20")
    maximum_observation_age: timedelta = timedelta(seconds=120)
    minimum_observations_per_market: int = 3
    maximum_observation_interval: timedelta = timedelta(seconds=300)
    imbalance_weight: Decimal = Decimal("0.03")
    microprice_weight: Decimal = Decimal("0.03")
    momentum_weight: Decimal = Decimal("0.02")
    maximum_probability_adjustment: Decimal = Decimal("0.05")
    conservative_shrinkage_factor: Decimal = Decimal("0.50")
    minimum_conservative_advantage: Decimal = Decimal("0.01")
    expected_fee_model: str = "kalshi-quadratic-taker-v1"
    slippage_reserve: Decimal = Decimal("0.01")
    execution_uncertainty_reserve: Decimal = Decimal("0.01")
    evidence_tier: str = "RESEARCH"
    configuration_version: str = CONFIGURATION_VERSION
    published_fee_schedule: PublishedProviderFeeSchedule | None = None

    def __post_init__(self) -> None:
        if self.strategy_id != STRATEGY_ID or self.strategy_version != STRATEGY_VERSION:
            raise ValueValidationError("microstructure strategy identity is fixed")
        if not 1 <= self.maximum_markets_per_invocation <= 100:
            raise ValueValidationError("maximum markets must be between one and 100")
        if not 1 <= self.orderbook_depth <= 20:
            raise ValueValidationError("orderbook depth must be between one and 20")
        if not 2 <= self.minimum_observations_per_market <= 20:
            raise ValueValidationError("minimum observations must be between two and 20")
        if not timedelta(seconds=1) <= self.maximum_observation_age <= timedelta(hours=1):
            raise ValueValidationError("maximum observation age must be bounded")
        if not timedelta(seconds=1) <= self.maximum_observation_interval <= timedelta(hours=1):
            raise ValueValidationError("maximum observation interval must be bounded")
        for name in (
            "minimum_visible_quantity",
            "maximum_spread",
            "imbalance_weight",
            "microprice_weight",
            "momentum_weight",
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
        if self.minimum_visible_quantity <= 0 or not Decimal(0) < self.maximum_spread <= ONE:
            raise ValueValidationError("visible quantity and spread bounds are invalid")
        if self.maximum_probability_adjustment > ONE:
            raise ValueValidationError("maximum adjustment must be in the binary range")
        if not Decimal(0) <= self.conservative_shrinkage_factor <= Decimal("0.50"):
            raise ValueValidationError("conservative shrinkage must be between zero and 0.50")
        if not self.expected_fee_model or not self.configuration_version:
            raise ValueValidationError("fee model and configuration version are required")
        if self.configuration_version == CONFIGURATION_VERSION:
            if self.published_fee_schedule is not None:
                raise ValueValidationError(
                    "v1 microstructure configuration cannot bind a published fee"
                )
        elif self.configuration_version == PUBLISHED_FEE_CONFIGURATION_VERSION:
            schedule = self.published_fee_schedule
            if schedule != KALSHI_PUBLISHED_FEE_SCHEDULE:
                raise ValueValidationError(
                    "v2 microstructure configuration requires the verified schedule"
                )
            if self.expected_fee_model != schedule.contract_identity:
                raise ValueValidationError("v2 microstructure fee contract identity is fixed")
        else:
            raise ValueValidationError("microstructure configuration version is unsupported")

    @property
    def uses_published_fee_schedule(self) -> bool:
        return self.configuration_version == PUBLISHED_FEE_CONFIGURATION_VERSION

    @property
    def canonical_hash(self) -> ContentHash:
        payload: dict[str, object] = {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "maximum_markets_per_invocation": self.maximum_markets_per_invocation,
            "orderbook_depth": self.orderbook_depth,
            "minimum_visible_quantity": self.minimum_visible_quantity,
            "maximum_spread": self.maximum_spread,
            "maximum_observation_age_seconds": int(self.maximum_observation_age.total_seconds()),
            "minimum_observations_per_market": self.minimum_observations_per_market,
            "maximum_observation_interval_seconds": int(
                self.maximum_observation_interval.total_seconds()
            ),
            "imbalance_weight": self.imbalance_weight,
            "microprice_weight": self.microprice_weight,
            "momentum_weight": self.momentum_weight,
            "maximum_probability_adjustment": self.maximum_probability_adjustment,
            "conservative_shrinkage_factor": self.conservative_shrinkage_factor,
            "minimum_conservative_advantage": self.minimum_conservative_advantage,
            "expected_fee_model": self.expected_fee_model,
            "slippage_reserve": self.slippage_reserve,
            "execution_uncertainty_reserve": self.execution_uncertainty_reserve,
            "evidence_tier": self.evidence_tier,
            "configuration_version": self.configuration_version,
        }
        if self.uses_published_fee_schedule:
            assert self.published_fee_schedule is not None
            payload["published_fee_schedule"] = self.published_fee_schedule.canonical_payload
        return ContentHash(content_digest(payload))


def published_fee_microstructure_v2_configuration(
    **overrides: object,
) -> OrderbookMicrostructureStrategyConfig:
    """Construct the sole verified v2 configuration without changing v1 defaults."""

    values: dict[str, object] = {
        "configuration_version": PUBLISHED_FEE_CONFIGURATION_VERSION,
        "expected_fee_model": KALSHI_PUBLISHED_FEE_SCHEDULE.contract_identity,
        "published_fee_schedule": KALSHI_PUBLISHED_FEE_SCHEDULE,
    }
    values.update(overrides)
    return OrderbookMicrostructureStrategyConfig(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class MicrostructureOrderbookEvidence:
    orderbook: PublicOrderbookSnapshot
    levels: tuple[PublicOrderbookLevel, ...]
    fee: SeriesFeeSnapshot | None
    rule_content_hash: ContentHash | None

    def __post_init__(self) -> None:
        if any(
            level.orderbook_snapshot_id != self.orderbook.orderbook_snapshot_id
            for level in self.levels
        ):
            raise ValueValidationError("orderbook levels must match the retained snapshot")


@dataclass(frozen=True, slots=True)
class MicrostructureMarketEvidence:
    market_snapshot_id: str
    provider: PublicMarketProvider
    provider_market_ticker: str
    provider_event_ticker: str | None
    series_ticker: str | None
    observed_at: datetime
    available_at: datetime
    close_at: datetime
    active: bool
    rule_content_hash: ContentHash | None
    observations: tuple[MicrostructureOrderbookEvidence, ...]

    def __post_init__(self) -> None:
        if not self.market_snapshot_id or not self.provider_market_ticker:
            raise ValueValidationError("retained market identity is required")
        for name in ("observed_at", "available_at", "close_at"):
            object.__setattr__(self, name, _aware(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class MicrostructureFeatureSnapshot:
    feature_snapshot_id: str
    strategy_id: str
    strategy_version: str
    market_snapshot_id: str
    first_orderbook_snapshot_id: str | None
    latest_orderbook_snapshot_id: str | None
    provider_market_ticker: str
    provider_event_ticker: str | None
    series_ticker: str | None
    decision_at: datetime
    observation_count: int
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    observation_interval: timedelta | None
    midpoint: Decimal | None
    spread: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    best_bid_quantity: Decimal | None
    best_ask_quantity: Decimal | None
    visible_bid_quantity: Decimal | None
    visible_ask_quantity: Decimal | None
    depth_imbalance: Decimal | None
    microprice: Decimal | None
    microprice_displacement: Decimal | None
    earliest_midpoint: Decimal | None
    latest_midpoint: Decimal | None
    midpoint_momentum: Decimal | None
    feature_state: MicrostructureFeatureState
    reason_codes: tuple[str, ...]
    configuration_hash: ContentHash
    canonical_input_hash: ContentHash
    canonical_feature_hash: ContentHash

    @property
    def valid(self) -> bool:
        return self.feature_state is MicrostructureFeatureState.VALID


@dataclass(frozen=True, slots=True)
class MicrostructureProbability:
    market_yes_probability: Decimal
    point_yes_probability: Decimal
    conservative_yes_probability: Decimal
    imbalance_contribution: Decimal
    microprice_contribution: Decimal
    momentum_contribution: Decimal
    raw_adjustment: Decimal
    bounded_adjustment: Decimal

    def __post_init__(self) -> None:
        for name in (
            "market_yes_probability",
            "point_yes_probability",
            "conservative_yes_probability",
        ):
            value = parse_decimal(getattr(self, name))
            if not Decimal(0) <= value <= ONE:
                raise ValueValidationError(f"{name} must be a probability")
            object.__setattr__(self, name, value)
        if (
            self.raw_adjustment
            != self.imbalance_contribution
            + self.microprice_contribution
            + self.momentum_contribution
        ):
            raise ValueValidationError("adjustment contributions must sum exactly")


def _feature(
    market: MicrostructureMarketEvidence,
    configuration: OrderbookMicrostructureStrategyConfig,
    decision_at: datetime,
    state: MicrostructureFeatureState,
    reasons: tuple[MicrostructureReasonCode, ...],
    observations: tuple[MicrostructureOrderbookEvidence, ...],
    values: dict[str, Decimal | None] | None = None,
) -> MicrostructureFeatureSnapshot:
    first = observations[0].orderbook if observations else None
    latest = observations[-1].orderbook if observations else None
    reasons_text = tuple(sorted(item.value for item in reasons))
    payload = {
        "market_snapshot_id": market.market_snapshot_id,
        "orderbooks": tuple(item.orderbook.orderbook_snapshot_id for item in observations),
        "configuration_hash": configuration.canonical_hash.value,
        "decision_at": decision_at,
        "state": state.value,
        "reasons": reasons_text,
        "values": values or {},
    }
    if configuration.uses_published_fee_schedule:
        payload["published_fee_evidence"] = tuple(
            {
                "fee_snapshot_id": item.fee.fee_snapshot_id if item.fee else None,
                "source_hash": item.fee.source_hash.value if item.fee else None,
                "fee_type": item.fee.fee_type if item.fee else None,
                "fee_multiplier": item.fee.fee_multiplier if item.fee else None,
                "effective_at": item.fee.effective_at if item.fee else None,
                "rounding_identity": item.fee.rounding_identity if item.fee else None,
            }
            for item in observations
        )
    input_hash = ContentHash(content_digest(payload))
    value = values or {}
    return MicrostructureFeatureSnapshot(
        feature_snapshot_id=deterministic_id("orderbook_microstructure_feature", payload),
        strategy_id=configuration.strategy_id,
        strategy_version=configuration.strategy_version,
        market_snapshot_id=market.market_snapshot_id,
        first_orderbook_snapshot_id=first.orderbook_snapshot_id if first else None,
        latest_orderbook_snapshot_id=latest.orderbook_snapshot_id if latest else None,
        provider_market_ticker=market.provider_market_ticker,
        provider_event_ticker=market.provider_event_ticker,
        series_ticker=market.series_ticker,
        decision_at=decision_at,
        observation_count=len(observations),
        first_observed_at=first.observed_at if first else None,
        last_observed_at=latest.observed_at if latest else None,
        observation_interval=(latest.observed_at - first.observed_at if first and latest else None),
        midpoint=value.get("midpoint"),
        spread=value.get("spread"),
        best_bid=value.get("best_bid"),
        best_ask=value.get("best_ask"),
        best_bid_quantity=value.get("best_bid_quantity"),
        best_ask_quantity=value.get("best_ask_quantity"),
        visible_bid_quantity=value.get("visible_bid_quantity"),
        visible_ask_quantity=value.get("visible_ask_quantity"),
        depth_imbalance=value.get("depth_imbalance"),
        microprice=value.get("microprice"),
        microprice_displacement=value.get("microprice_displacement"),
        earliest_midpoint=value.get("earliest_midpoint"),
        latest_midpoint=value.get("latest_midpoint"),
        midpoint_momentum=value.get("midpoint_momentum"),
        feature_state=state,
        reason_codes=reasons_text,
        configuration_hash=configuration.canonical_hash,
        canonical_input_hash=input_hash,
        canonical_feature_hash=ContentHash(
            content_digest({"input": input_hash.value, "values": value})
        ),
    )


def _held_book(
    evidence: MicrostructureOrderbookEvidence, depth: int
) -> tuple[tuple[PublicOrderbookLevel, ...], tuple[PublicOrderbookLevel, ...]]:
    bids = tuple(
        sorted(
            (item for item in evidence.levels if item.side is OrderbookSide.YES_BID),
            key=lambda item: (-item.bid_price, item.ordinal, item.level_id),
        )[:depth]
    )
    # A YES ask is derived only from an opposite NO bid; highest NO bid is cheapest.
    asks = tuple(
        sorted(
            (item for item in evidence.levels if item.side is OrderbookSide.NO_BID),
            key=lambda item: (-item.bid_price, item.ordinal, item.level_id),
        )[:depth]
    )
    return bids, asks


def _published_fee_reasons(
    *,
    observations: tuple[MicrostructureOrderbookEvidence, ...],
    market: MicrostructureMarketEvidence,
    decision_at: datetime,
) -> tuple[MicrostructureReasonCode, ...]:
    """Validate retained series facts against the source-bound v2 contract."""

    reasons: set[MicrostructureReasonCode] = set()
    for item in observations:
        fee = item.fee
        if fee is None or fee.series_ticker != market.series_ticker:
            reasons.add(MicrostructureReasonCode.FEE_EVIDENCE_INCOMPATIBLE)
            continue
        applicability = reconcile_kalshi_published_fee_schedule(
            provider_fee_type=fee.fee_type,
            series_ticker=fee.series_ticker,
            fee_multiplier=fee.fee_multiplier,
            fee_effective_at=fee.effective_at,
            decision_at=decision_at,
            fee_model_identity=fee.fee_model_identity,
            taker_coefficient=fee.taker_coefficient,
            maker_coefficient=fee.maker_coefficient,
            retained_rounding_identity=fee.rounding_identity,
            schedule_identity=fee.schedule_identity,
            schedule_effective_date=fee.schedule_effective_date,
            expected_fee_model_identity=FEE_MODEL_IDENTITY,
            expected_retained_rounding_identity=ROUNDING_IDENTITY,
        )
        if not applicability.is_exact_taker_compatible:
            if fee.effective_at is None:
                reasons.add(MicrostructureReasonCode.PUBLISHED_FEE_EFFECTIVE_TIME_MISSING)
            elif fee.effective_at > decision_at:
                reasons.add(MicrostructureReasonCode.PUBLISHED_FEE_EFFECTIVE_TIME_FUTURE)
            elif fee.fee_multiplier is None:
                reasons.add(MicrostructureReasonCode.PUBLISHED_FEE_MULTIPLIER_MISSING)
            elif fee.rounding_identity != ROUNDING_IDENTITY:
                reasons.add(MicrostructureReasonCode.PUBLISHED_FEE_ROUNDING_EVIDENCE_INCOMPATIBLE)
            else:
                reasons.add(MicrostructureReasonCode.PUBLISHED_FEE_TYPE_INCOMPATIBLE)
    return tuple(sorted(reasons, key=lambda item: item.value))


def evaluate_microstructure_features(
    *,
    market: MicrostructureMarketEvidence,
    configuration: OrderbookMicrostructureStrategyConfig,
    decision_at: datetime,
) -> MicrostructureFeatureSnapshot:
    """Calculate features from exact compatible evidence; retain reasons, never fill gaps."""
    decision = _aware(decision_at, "decision_at")
    observations = tuple(
        sorted(
            market.observations,
            key=lambda item: (item.orderbook.observed_at, item.orderbook.orderbook_snapshot_id),
        )
    )
    ineligible: list[MicrostructureReasonCode] = []
    if market.provider is not PublicMarketProvider.KALSHI:
        ineligible.append(MicrostructureReasonCode.MARKET_NOT_KALSHI)
    if not market.active or market.close_at <= decision:
        ineligible.append(MicrostructureReasonCode.MARKET_NOT_ACTIVE)
    if not market.provider_event_ticker:
        ineligible.append(MicrostructureReasonCode.MARKET_IDENTITY_MISSING)
    if not market.series_ticker:
        ineligible.append(MicrostructureReasonCode.SERIES_EVIDENCE_MISSING)
    if market.rule_content_hash is None:
        ineligible.append(MicrostructureReasonCode.RULE_EVIDENCE_MISSING)
    if len(observations) < configuration.minimum_observations_per_market:
        ineligible.append(MicrostructureReasonCode.INSUFFICIENT_OBSERVATIONS)
    if ineligible:
        return _feature(
            market,
            configuration,
            decision,
            MicrostructureFeatureState.INELIGIBLE,
            tuple(ineligible),
            observations,
        )
    times = tuple(item.orderbook.observed_at for item in observations)
    if any(left >= right for left, right in pairwise(times)):
        return _feature(
            market,
            configuration,
            decision,
            MicrostructureFeatureState.INVALID,
            (MicrostructureReasonCode.OBSERVATION_ORDER_CONFLICT,),
            observations,
        )
    if decision - times[-1] > configuration.maximum_observation_age:
        return _feature(
            market,
            configuration,
            decision,
            MicrostructureFeatureState.INELIGIBLE,
            (MicrostructureReasonCode.EVIDENCE_STALE,),
            observations,
        )
    if any(
        right - left > configuration.maximum_observation_interval for left, right in pairwise(times)
    ):
        return _feature(
            market,
            configuration,
            decision,
            MicrostructureFeatureState.INELIGIBLE,
            (MicrostructureReasonCode.OBSERVATION_INTERVAL_EXCEEDED,),
            observations,
        )
    if any(
        item.orderbook.data_quality_state != "VALID"
        or item.orderbook.provider is not PublicMarketProvider.KALSHI
        or item.orderbook.provider_market_ticker != market.provider_market_ticker
        or item.orderbook.provider_event_ticker != market.provider_event_ticker
        or item.orderbook.series_ticker != market.series_ticker
        or item.rule_content_hash != market.rule_content_hash
        for item in observations
    ):
        return _feature(
            market,
            configuration,
            decision,
            MicrostructureFeatureState.INVALID,
            (MicrostructureReasonCode.ORDERBOOK_INVALID,),
            observations,
        )
    if configuration.uses_published_fee_schedule:
        published_fee_reasons = _published_fee_reasons(
            observations=observations, market=market, decision_at=decision
        )
        if published_fee_reasons:
            return _feature(
                market,
                configuration,
                decision,
                MicrostructureFeatureState.INELIGIBLE,
                published_fee_reasons,
                observations,
            )
    elif any(
        item.fee is None
        or not item.fee.is_supported
        or item.fee.fee_model_identity != configuration.expected_fee_model
        or item.fee.series_ticker != market.series_ticker
        for item in observations
    ):
        return _feature(
            market,
            configuration,
            decision,
            MicrostructureFeatureState.INELIGIBLE,
            (MicrostructureReasonCode.FEE_EVIDENCE_INCOMPATIBLE,),
            observations,
        )
    books = tuple(_held_book(item, configuration.orderbook_depth) for item in observations)
    if any(not bids or not asks for bids, asks in books):
        return _feature(
            market,
            configuration,
            decision,
            MicrostructureFeatureState.INELIGIBLE,
            (MicrostructureReasonCode.ORDERBOOK_MISSING_SIDE,),
            observations,
        )
    bids, asks = books[-1]
    bid, ask = bids[0].bid_price, asks[0].derived_ask_price
    if bid > ask:
        return _feature(
            market,
            configuration,
            decision,
            MicrostructureFeatureState.INVALID,
            (MicrostructureReasonCode.ORDERBOOK_CROSSED_OR_CONTRADICTORY,),
            observations,
        )
    spread, visible_bid, visible_ask = (
        ask - bid,
        sum((item.quantity for item in bids), Decimal(0)),
        sum((item.quantity for item in asks), Decimal(0)),
    )
    if (
        min(visible_bid, visible_ask, bids[0].quantity, asks[0].quantity)
        < configuration.minimum_visible_quantity
    ):
        return _feature(
            market,
            configuration,
            decision,
            MicrostructureFeatureState.INELIGIBLE,
            (MicrostructureReasonCode.VISIBLE_LIQUIDITY_INSUFFICIENT,),
            observations,
        )
    if spread > configuration.maximum_spread:
        return _feature(
            market,
            configuration,
            decision,
            MicrostructureFeatureState.INELIGIBLE,
            (MicrostructureReasonCode.SPREAD_EXCEEDS_MAXIMUM,),
            observations,
        )
    midpoint = (bid + ask) / Decimal(2)
    imbalance = _fixed(
        _clamp((visible_bid - visible_ask) / (visible_bid + visible_ask), Decimal(-1), Decimal(1))
    )
    microprice = _fixed(
        (ask * bids[0].quantity + bid * asks[0].quantity) / (bids[0].quantity + asks[0].quantity)
    )
    midpoints = tuple(
        _fixed((book_bids[0].bid_price + book_asks[0].derived_ask_price) / Decimal(2))
        for book_bids, book_asks in books
    )
    values: dict[str, Decimal | None] = {
        "midpoint": midpoint,
        "spread": spread,
        "best_bid": bid,
        "best_ask": ask,
        "best_bid_quantity": bids[0].quantity,
        "best_ask_quantity": asks[0].quantity,
        "visible_bid_quantity": visible_bid,
        "visible_ask_quantity": visible_ask,
        "depth_imbalance": imbalance,
        "microprice": microprice,
        "microprice_displacement": _fixed(microprice - midpoint),
        "earliest_midpoint": midpoints[0],
        "latest_midpoint": midpoints[-1],
        "midpoint_momentum": _fixed(midpoints[-1] - midpoints[0]),
    }
    return _feature(
        market,
        configuration,
        decision,
        MicrostructureFeatureState.VALID,
        (MicrostructureReasonCode.FEATURES_VALID,),
        observations,
        values,
    )


def derive_microstructure_probability(
    *, feature: MicrostructureFeatureSnapshot, configuration: OrderbookMicrostructureStrategyConfig
) -> MicrostructureProbability:
    """Apply fixed scales: imbalance [-1,1], microprice/spread, momentum/max spread."""
    if not feature.valid or feature.midpoint is None or feature.spread is None:
        raise ValueValidationError("a valid feature snapshot is required")
    assert (
        feature.depth_imbalance is not None
        and feature.microprice_displacement is not None
        and feature.midpoint_momentum is not None
    )
    scale = feature.spread if feature.spread > 0 else configuration.maximum_spread
    microprice_normalized = _fixed(
        _clamp(feature.microprice_displacement / scale, Decimal(-1), Decimal(1))
    )
    momentum_normalized = _fixed(
        _clamp(feature.midpoint_momentum / configuration.maximum_spread, Decimal(-1), Decimal(1))
    )
    imbalance = _fixed(configuration.imbalance_weight * feature.depth_imbalance)
    microprice = _fixed(configuration.microprice_weight * microprice_normalized)
    momentum = _fixed(configuration.momentum_weight * momentum_normalized)
    raw = _fixed(imbalance + microprice + momentum)
    bounded = _fixed(
        _clamp(
            raw,
            -configuration.maximum_probability_adjustment,
            configuration.maximum_probability_adjustment,
        )
    )
    point = _fixed(_clamp(feature.midpoint + bounded, Decimal("0.01"), Decimal("0.99")))
    conservative = _fixed(
        feature.midpoint + configuration.conservative_shrinkage_factor * (point - feature.midpoint)
    )
    return MicrostructureProbability(
        feature.midpoint, point, conservative, imbalance, microprice, momentum, raw, bounded
    )
