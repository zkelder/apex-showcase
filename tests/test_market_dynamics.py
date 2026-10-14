from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.application.market_dynamics_shadow import (
    evaluate_retained_market_dynamics,
    market_dynamics_strategy_descriptor,
)
from apex.application.strategy_shadow import RetainedMarketEvidence
from apex.domain.market_dynamics import (
    MarketDynamicsFeatureState,
    MarketDynamicsMarketEvidence,
    MarketDynamicsObservation,
    MarketDynamicsStrategyConfig,
    evaluate_market_dynamics_features,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash

NOW = datetime(2030, 1, 1, tzinfo=UTC)
RULE_HASH = ContentHash("c" * 64)


def _evidence(
    values: tuple[tuple[str, str], ...] = (("0.39", "0.41"), ("0.44", "0.46"), ("0.49", "0.51")),
    *,
    seconds: tuple[int, ...] = (20, 10, 0),
) -> MarketDynamicsMarketEvidence:
    observations = tuple(
        MarketDynamicsObservation(
            snapshot_id=f"dynamics-snapshot-{ordinal}",
            provider=PublicMarketProvider.KALSHI,
            provider_market_ticker="KX-DYNAMICS",
            provider_event_ticker="KX-DYNAMICS-EVENT",
            series_ticker="KX-DYNAMICS-SERIES",
            observed_at=NOW - timedelta(seconds=seconds[ordinal]),
            available_at=NOW - timedelta(seconds=seconds[ordinal] - 1),
            close_at=NOW + timedelta(days=1),
            status="open",
            rule_content_hash=RULE_HASH,
            yes_bid=Decimal(bid),
            yes_ask=Decimal(ask),
        )
        for ordinal, (bid, ask) in enumerate(values)
    )
    return MarketDynamicsMarketEvidence(
        market_snapshot_id=observations[-1].snapshot_id,
        provider=PublicMarketProvider.KALSHI,
        provider_market_ticker="KX-DYNAMICS",
        provider_event_ticker="KX-DYNAMICS-EVENT",
        series_ticker="KX-DYNAMICS-SERIES",
        active=True,
        observations=observations,
    )


def _market() -> RetainedMarketEvidence:
    return RetainedMarketEvidence(
        snapshot_id="dynamics-snapshot-2",
        provider="KALSHI",
        provider_market_ticker="KX-DYNAMICS",
        provider_event_ticker="KX-DYNAMICS-EVENT",
        observed_at=NOW,
        available_at=NOW,
        close_at=NOW + timedelta(days=1),
        rule_text="Exact synthetic dynamics rule.",
        rule_content_hash=RULE_HASH,
        yes_bid=Decimal("0.49"),
        yes_ask=Decimal("0.51"),
        no_ask=Decimal("0.51"),
        visible_yes_quantity=Decimal("10"),
        fee_model_version="kalshi-quadratic-taker-v1",
        expected_fee=Decimal(0),
    )


def test_descriptor_is_independent_market_dynamics_shadow_strategy() -> None:
    descriptor = market_dynamics_strategy_descriptor()

    assert descriptor.strategy_id == "kalshi-market-dynamics-v1"
    assert descriptor.family.value == "MARKET_DYNAMICS"
    assert descriptor.authority_state.value == "SHADOW"
    assert descriptor.maximum_proposals_per_cycle == 20


def test_exact_temporal_features_and_probability_are_reproducible() -> None:
    config = MarketDynamicsStrategyConfig()
    feature = evaluate_market_dynamics_features(
        market=_evidence(), configuration=config, decision_at=NOW
    )

    assert feature.feature_state is MarketDynamicsFeatureState.VALID
    assert feature.earliest_midpoint == Decimal("0.40000000")
    assert feature.latest_midpoint == Decimal("0.50000000")
    assert feature.window_momentum == Decimal("0.10000000")
    assert feature.recent_momentum == Decimal("0.05000000")
    assert feature.positive_move_count == 2
    assert feature.negative_move_count == 0
    assert feature.signed_directional_persistence == Decimal("1.00000000")
    assert feature.clamped_adjustment == Decimal("0.05000000")


def test_invalid_history_is_retained_without_cleaning_gaps_or_zero_moves() -> None:
    gap = evaluate_market_dynamics_features(
        market=_evidence(seconds=(700, 10, 0)),
        configuration=MarketDynamicsStrategyConfig(),
        decision_at=NOW,
    )
    zero = evaluate_market_dynamics_features(
        market=_evidence((("0.49", "0.51"), ("0.49", "0.51"), ("0.49", "0.51"))),
        configuration=MarketDynamicsStrategyConfig(),
        decision_at=NOW,
    )

    assert gap.feature_state is MarketDynamicsFeatureState.INELIGIBLE
    assert gap.reason_codes == ("OBSERVATION_GAP_EXCEEDED",)
    assert zero.feature_state is MarketDynamicsFeatureState.INELIGIBLE
    assert zero.reason_codes == ("ZERO_USABLE_MOVEMENT",)


def test_each_observation_requires_its_own_enriched_event_and_series_lineage() -> None:
    base = _evidence()
    unenriched = MarketDynamicsMarketEvidence(
        market_snapshot_id=base.market_snapshot_id,
        provider=base.provider,
        provider_market_ticker=base.provider_market_ticker,
        provider_event_ticker=base.provider_event_ticker,
        series_ticker=base.series_ticker,
        active=True,
        observations=(
            base.observations[0],
            replace(base.observations[1], series_ticker=None),
            base.observations[2],
        ),
    )

    rejected = evaluate_market_dynamics_features(
        market=unenriched, configuration=MarketDynamicsStrategyConfig(), decision_at=NOW
    )
    accepted = evaluate_market_dynamics_features(
        market=base, configuration=MarketDynamicsStrategyConfig(), decision_at=NOW
    )

    assert rejected.feature_state is MarketDynamicsFeatureState.INVALID
    assert rejected.reason_codes == ("SERIES_CHANGED",)
    assert accepted.feature_state is MarketDynamicsFeatureState.VALID


def test_valid_prediction_can_remain_without_proposal_or_qualify() -> None:
    feature = evaluate_market_dynamics_features(
        market=_evidence(), configuration=MarketDynamicsStrategyConfig(), decision_at=NOW
    )
    default = evaluate_retained_market_dynamics(
        campaign_id="campaign",
        market=_market(),
        feature=feature,
        configuration=MarketDynamicsStrategyConfig(),
        decision_at=NOW,
    )
    qualifying_config = MarketDynamicsStrategyConfig(
        window_momentum_weight=Decimal("0.40"),
        recent_momentum_weight=Decimal(0),
        directional_persistence_weight=Decimal(0),
        maximum_probability_adjustment=Decimal("0.40"),
        slippage_reserve=Decimal(0),
        execution_uncertainty_reserve=Decimal(0),
    )
    qualified = evaluate_retained_market_dynamics(
        campaign_id="campaign",
        market=_market(),
        feature=feature,
        configuration=qualifying_config,
        decision_at=NOW,
    )

    assert default.prediction is not None
    assert default.opportunity is None
    assert qualified.prediction is not None
    assert qualified.opportunity is not None
    assert dict(qualified.prediction.point_probabilities)["YES"] == Decimal("0.90000000")
    assert sum(dict(qualified.prediction.conservative_probabilities).values()) == Decimal(1)
