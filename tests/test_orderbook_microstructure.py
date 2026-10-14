from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.application.orderbook_microstructure_shadow import evaluate_retained_microstructure
from apex.application.strategy_shadow import RetainedMarketEvidence
from apex.domain.orderbook_microstructure import (
    MicrostructureFeatureState,
    MicrostructureMarketEvidence,
    MicrostructureOrderbookEvidence,
    MicrostructureReasonCode,
    OrderbookMicrostructureStrategyConfig,
    derive_microstructure_probability,
    evaluate_microstructure_features,
    published_fee_microstructure_v2_configuration,
)
from apex.domain.public_complement import (
    FEE_MODEL_IDENTITY,
    MAKER_COEFFICIENT,
    ROUNDING_IDENTITY,
    SCHEDULE_EFFECTIVE_DATE,
    SCHEDULE_IDENTITY,
    TAKER_COEFFICIENT,
    OrderbookSide,
    PublicOrderbookLevel,
    PublicOrderbookSnapshot,
    SeriesFeeSnapshot,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)
HASH = ContentHash("a" * 64)


def _observation(
    ordinal: int, *, yes_bid: str = "0.50", no_bid: str = "0.46"
) -> MicrostructureOrderbookEvidence:
    observed = NOW - timedelta(seconds=(3 - ordinal) * 10)
    book_id = f"book-{ordinal}"
    book = PublicOrderbookSnapshot(
        orderbook_snapshot_id=book_id,
        ingestion_run_id=f"run-{ordinal}",
        raw_response_id=f"raw-{ordinal}",
        market_snapshot_id="market-1",
        provider=PublicMarketProvider.KALSHI,
        provider_market_ticker="KXMICRO",
        provider_event_ticker="KXEVENT",
        series_ticker="KXSERIES",
        observed_at=observed,
        maximum_depth=5,
        yes_bid_level_count=1,
        no_bid_level_count=1,
        data_quality_state="VALID",
        canonical_source_hash=HASH,
    )
    levels = (
        PublicOrderbookLevel(
            f"yes-{ordinal}",
            book_id,
            OrderbookSide.YES_BID,
            0,
            Decimal(yes_bid),
            Decimal("8"),
            1,
            HASH,
        ),
        PublicOrderbookLevel(
            f"no-{ordinal}",
            book_id,
            OrderbookSide.NO_BID,
            0,
            Decimal(no_bid),
            Decimal("4"),
            1,
            HASH,
        ),
    )
    fee = SeriesFeeSnapshot(
        fee_snapshot_id=f"fee-{ordinal}",
        ingestion_run_id=f"run-{ordinal}",
        raw_response_id=f"fee-raw-{ordinal}",
        provider=PublicMarketProvider.KALSHI,
        series_ticker="KXSERIES",
        observed_at=observed,
        fee_type="quadratic",
        fee_multiplier=Decimal("1"),
        effective_at=None,
        fee_model_identity=FEE_MODEL_IDENTITY,
        taker_coefficient=TAKER_COEFFICIENT,
        maker_coefficient=MAKER_COEFFICIENT,
        rounding_identity=ROUNDING_IDENTITY,
        schedule_identity=SCHEDULE_IDENTITY,
        schedule_effective_date=SCHEDULE_EFFECTIVE_DATE,
        source_hash=HASH,
    )
    return MicrostructureOrderbookEvidence(book, levels, fee, HASH)


def _market(
    observations: tuple[MicrostructureOrderbookEvidence, ...],
) -> MicrostructureMarketEvidence:
    return MicrostructureMarketEvidence(
        market_snapshot_id="market-1",
        provider=PublicMarketProvider.KALSHI,
        provider_market_ticker="KXMICRO",
        provider_event_ticker="KXEVENT",
        series_ticker="KXSERIES",
        observed_at=NOW - timedelta(seconds=30),
        available_at=NOW - timedelta(seconds=29),
        close_at=NOW + timedelta(hours=1),
        active=True,
        rule_content_hash=HASH,
        observations=observations,
    )


def _published_observation(
    ordinal: int, *, yes_bid: str = "0.50", no_bid: str = "0.46"
) -> MicrostructureOrderbookEvidence:
    legacy = _observation(ordinal, yes_bid=yes_bid, no_bid=no_bid)
    assert legacy.fee is not None
    return MicrostructureOrderbookEvidence(
        legacy.orderbook,
        legacy.levels,
        replace(
            legacy.fee,
            fee_type="quadratic_with_maker_fees",
            effective_at=NOW - timedelta(days=3),
        ),
        legacy.rule_content_hash,
    )


def test_microstructure_feature_math_is_exact_and_probability_is_conservative() -> None:
    feature = evaluate_microstructure_features(
        market=_market(
            (
                _observation(0, yes_bid="0.48"),
                _observation(1, yes_bid="0.49"),
                _observation(2, yes_bid="0.50"),
            )
        ),
        configuration=OrderbookMicrostructureStrategyConfig(),
        decision_at=NOW,
    )

    assert feature.feature_state is MicrostructureFeatureState.VALID
    assert feature.best_bid == Decimal("0.50")
    assert feature.best_ask == Decimal("0.54")
    assert feature.midpoint == Decimal("0.52")
    assert feature.visible_bid_quantity == Decimal("8")
    assert feature.visible_ask_quantity == Decimal("4")
    assert feature.depth_imbalance == Decimal("0.33333333")
    assert feature.microprice == Decimal("0.52666667")
    assert feature.midpoint_momentum == Decimal("0.01")
    probability = derive_microstructure_probability(
        feature=feature, configuration=OrderbookMicrostructureStrategyConfig()
    )
    assert probability.point_yes_probability > probability.market_yes_probability
    assert (
        probability.market_yes_probability
        < probability.conservative_yes_probability
        < probability.point_yes_probability
    )


def test_stale_and_one_sided_evidence_are_retained_without_a_prediction() -> None:
    stale = evaluate_microstructure_features(
        market=_market((_observation(0), _observation(1), _observation(2))),
        configuration=OrderbookMicrostructureStrategyConfig(
            maximum_observation_age=timedelta(seconds=5)
        ),
        decision_at=NOW,
    )
    assert stale.feature_state is MicrostructureFeatureState.INELIGIBLE
    assert stale.reason_codes == (MicrostructureReasonCode.EVIDENCE_STALE.value,)

    one_sided = _observation(2)
    one_sided = MicrostructureOrderbookEvidence(
        one_sided.orderbook, one_sided.levels[:1], one_sided.fee, HASH
    )
    missing = evaluate_microstructure_features(
        market=_market((_observation(0), _observation(1), one_sided)),
        configuration=OrderbookMicrostructureStrategyConfig(),
        decision_at=NOW,
    )
    assert missing.feature_state is MicrostructureFeatureState.INELIGIBLE
    assert missing.reason_codes == (MicrostructureReasonCode.ORDERBOOK_MISSING_SIDE.value,)


def test_valid_features_create_a_forward_prediction_even_without_a_proposal() -> None:
    feature = evaluate_microstructure_features(
        market=_market((_observation(0), _observation(1), _observation(2))),
        configuration=OrderbookMicrostructureStrategyConfig(),
        decision_at=NOW,
    )
    market = RetainedMarketEvidence(
        snapshot_id="market-1",
        provider="KALSHI",
        provider_market_ticker="KXMICRO",
        provider_event_ticker="KXEVENT",
        observed_at=NOW - timedelta(seconds=30),
        available_at=NOW - timedelta(seconds=29),
        close_at=NOW + timedelta(hours=1),
        rule_text="Resolves by the retained public contract rule.",
        rule_content_hash=HASH,
        yes_bid=Decimal("0.50"),
        yes_ask=Decimal("0.54"),
        no_ask=Decimal("0.50"),
        visible_yes_quantity=Decimal("8"),
        fee_model_version="kalshi-quadratic-taker-v1",
        expected_fee=Decimal("0"),
    )
    result = evaluate_retained_microstructure(
        campaign_id="campaign-1",
        market=market,
        feature=feature,
        configuration=OrderbookMicrostructureStrategyConfig(),
        decision_at=NOW,
    )

    assert result.prediction is not None
    assert result.prediction.proposal_id is None
    assert result.opportunity is None
    assert result.probability is not None


def test_v1_configuration_hash_is_preserved() -> None:
    assert (
        OrderbookMicrostructureStrategyConfig().canonical_hash.value
        == "8a6549802093b6ce1f157e78f8c3fc26e787bd92d137528557243880c38f40d7"
    )


def test_v2_requires_effective_fee_evidence_without_changing_retained_v1_features() -> None:
    v1_feature = evaluate_microstructure_features(
        market=_market((_observation(0), _observation(1), _observation(2))),
        configuration=OrderbookMicrostructureStrategyConfig(),
        decision_at=NOW,
    )
    v2_feature = evaluate_microstructure_features(
        market=_market((_observation(0), _observation(1), _observation(2))),
        configuration=published_fee_microstructure_v2_configuration(),
        decision_at=NOW,
    )

    assert v1_feature.feature_state is MicrostructureFeatureState.VALID
    assert v2_feature.feature_state is MicrostructureFeatureState.INELIGIBLE
    assert v2_feature.reason_codes == (
        MicrostructureReasonCode.PUBLISHED_FEE_EFFECTIVE_TIME_MISSING.value,
    )


def test_v2_immediate_cross_uses_published_taker_fee_only() -> None:
    observations = (
        _published_observation(0),
        _published_observation(1),
        _published_observation(2),
    )
    configuration = published_fee_microstructure_v2_configuration(
        imbalance_weight=Decimal("0.40"),
        maximum_probability_adjustment=Decimal("0.40"),
        slippage_reserve=Decimal(0),
        execution_uncertainty_reserve=Decimal(0),
        minimum_conservative_advantage=Decimal("0.01"),
    )
    feature = evaluate_microstructure_features(
        market=_market(observations), configuration=configuration, decision_at=NOW
    )
    retained_market = RetainedMarketEvidence(
        snapshot_id="market-1",
        provider="KALSHI",
        provider_market_ticker="KXMICRO",
        provider_event_ticker="KXEVENT",
        observed_at=NOW - timedelta(seconds=30),
        available_at=NOW - timedelta(seconds=29),
        close_at=NOW + timedelta(hours=1),
        rule_text="Resolves by the retained public contract rule.",
        rule_content_hash=HASH,
        yes_bid=Decimal("0.50"),
        yes_ask=Decimal("0.54"),
        no_ask=Decimal("0.50"),
        visible_yes_quantity=Decimal("8"),
        fee_model_version=configuration.expected_fee_model,
        expected_fee=Decimal("0"),
    )
    evaluation = evaluate_retained_microstructure(
        campaign_id="campaign-1",
        market=retained_market,
        feature=feature,
        configuration=configuration,
        fee_evidence=observations[-1].fee,
        decision_at=NOW,
    )

    assert feature.feature_state is MicrostructureFeatureState.VALID
    assert evaluation.opportunity is not None
    assert evaluation.prediction is not None
    proposal = evaluation.opportunity.proposal
    assert proposal.fee_model_version == configuration.expected_fee_model
    assert proposal.fee_per_unit.amount == Decimal("0.0174")
    assert proposal.fee_per_unit.amount != Decimal("0.0044")
    assert proposal.fee_per_unit.amount == evaluation.opportunity.economics.expected_fee
    assert observations[-1].fee is not None
    assert observations[-1].fee.fee_snapshot_id in evaluation.prediction.evidence_references


def test_v2_accepts_proven_quadratic_alias_with_exact_taker_terms() -> None:
    values: list[MicrostructureOrderbookEvidence] = []
    for value in (_published_observation(0), _published_observation(1), _published_observation(2)):
        assert value.fee is not None
        values.append(
            MicrostructureOrderbookEvidence(
                value.orderbook,
                value.levels,
                replace(value.fee, fee_type="quadratic"),
                value.rule_content_hash,
            )
        )
    observations = tuple(values)

    feature = evaluate_microstructure_features(
        market=_market(observations),
        configuration=published_fee_microstructure_v2_configuration(),
        decision_at=NOW,
    )

    assert feature.feature_state is MicrostructureFeatureState.VALID
