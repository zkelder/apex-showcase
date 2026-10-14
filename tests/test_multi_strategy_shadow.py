from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.application.multi_strategy_shadow import BuildEnrichedShadowMarketCohort
from apex.domain.multi_strategy import (
    CompatiblePredictionEvidence,
    EnrichedShadowMarket,
    PredictionComparisonState,
    ShadowMarketCohortConfiguration,
    compare_prediction_evidence,
)
from apex.domain.values import ContentHash

NOW = datetime(2031, 1, 1, tzinfo=UTC)
RULE_HASH = ContentHash("d" * 64)


def _market(ordinal: int, *, event: str = "EVENT", series: str = "SERIES") -> EnrichedShadowMarket:
    return EnrichedShadowMarket(
        market_snapshot_id=f"cohort-market-snapshot-{ordinal}",
        provider="KALSHI",
        market_ticker=f"KX-COHORT-{ordinal}",
        event_ticker=event,
        series_ticker=series,
        rule_content_hash=RULE_HASH,
        lifecycle_state="open",
        observed_at=NOW,
        available_at=NOW,
        close_at=NOW + timedelta(days=1),
        market_lineage_hash=ContentHash(f"{ordinal:064x}"),
        event_lineage_hash=ContentHash("e" * 64),
        series_lineage_hash=ContentHash("f" * 64),
        fee_lineage_hash=None,
    )


def _prediction(
    strategy_id: str,
    *,
    decided_at: datetime = NOW,
    rule_hash: ContentHash | None = RULE_HASH,
    conservative_yes: str = "0.54",
) -> CompatiblePredictionEvidence:
    return CompatiblePredictionEvidence(
        prediction_id=f"prediction-{strategy_id}",
        strategy_id=strategy_id,
        strategy_version="v1",
        market_ticker="KX-COHORT-1",
        event_ticker="EVENT",
        decision_at=decided_at,
        market_yes_probability=Decimal("0.50"),
        point_yes_probability=Decimal("0.55"),
        conservative_yes_probability=Decimal(conservative_yes),
        market_evidence_id="cohort-market-snapshot-1",
        feature_snapshot_id=None,
        configuration_version="test-v1",
        rule_content_hash=rule_hash,
    )


def test_shared_cohort_applies_deterministic_event_and_series_caps() -> None:
    configuration = ShadowMarketCohortConfiguration(
        selected_market_count=3,
        maximum_markets_per_event=1,
        maximum_markets_per_series=2,
    )
    result = BuildEnrichedShadowMarketCohort.build(
        campaign_id="campaign",
        cycle_key="cohort-v1",
        configuration=configuration,
        candidates=(
            _market(1, event="EVENT-A", series="SERIES-A"),
            _market(2, event="EVENT-A", series="SERIES-A"),
            _market(3, event="EVENT-B", series="SERIES-A"),
            _market(4, event="EVENT-C", series="SERIES-B"),
        ),
        decision_at=NOW,
    )

    assert tuple(item.market_ticker for item in result.selected) == (
        "KX-COHORT-1",
        "KX-COHORT-3",
        "KX-COHORT-4",
    )
    assert result.rejected_count == 1


def test_shared_cohort_rejects_duplicate_market_snapshot_evidence() -> None:
    configuration = ShadowMarketCohortConfiguration(
        selected_market_count=3,
        maximum_markets_per_event=3,
        maximum_markets_per_series=3,
    )
    first = _market(1)
    result = BuildEnrichedShadowMarketCohort.build(
        campaign_id="campaign",
        cycle_key="deduplicate-v1",
        configuration=configuration,
        candidates=(first, first, _market(2), _market(3)),
        decision_at=NOW,
    )

    assert tuple(item.market_snapshot_id for item in result.selected) == (
        "cohort-market-snapshot-1",
        "cohort-market-snapshot-2",
        "cohort-market-snapshot-3",
    )
    assert result.rejected_count == 1


def test_shared_cohort_enriches_before_applying_diversity_caps() -> None:
    configuration = ShadowMarketCohortConfiguration(
        maximum_enrichments=3,
        selected_market_count=3,
        maximum_markets_per_event=1,
        maximum_markets_per_series=2,
    )
    attempts: list[str] = []
    values = {
        "list-a": _market(1, event="EVENT-A", series="SERIES-A"),
        "list-b": None,
        "list-c": _market(3, event="EVENT-A", series="SERIES-B"),
    }

    def enrich(candidate: object) -> tuple[EnrichedShadowMarket | None, tuple[str, ...]]:
        assert isinstance(candidate, str)
        attempts.append(candidate)
        value = values[candidate]
        return (value, ()) if value is not None else (None, ("EVENT_MARKET_MEMBERSHIP_MISSING",))

    result = BuildEnrichedShadowMarketCohort.enrich(
        campaign_id="campaign",
        cycle_key="exact-enrichment-first",
        configuration=configuration,
        candidates=("list-a", "list-b", "list-c"),
        enrich_candidate=enrich,
        decision_at=NOW,
    )

    assert attempts == ["list-a", "list-b", "list-c"]
    assert result.attempted_count == 3
    assert result.successful_count == 2
    assert result.rejection_counts == (("EVENT_MARKET_MEMBERSHIP_MISSING", 1),)
    assert tuple(value.market_ticker for value in result.cohort.selected) == ("KX-COHORT-1",)


def test_comparison_keeps_probabilities_independent_and_rejects_time_skew() -> None:
    compatible = compare_prediction_evidence(
        cycle_id="cycle",
        values=(
            _prediction("market-consensus-baseline-v1", conservative_yes="0.50"),
            _prediction("kalshi-orderbook-microstructure-v1", conservative_yes="0.53"),
            _prediction("kalshi-market-dynamics-v1", conservative_yes="0.52"),
        ),
    )
    skewed = compare_prediction_evidence(
        cycle_id="cycle",
        values=(
            _prediction("market-consensus-baseline-v1", conservative_yes="0.50"),
            _prediction(
                "kalshi-orderbook-microstructure-v1",
                decided_at=NOW + timedelta(minutes=6),
            ),
        ),
    )

    assert compatible.state is PredictionComparisonState.STRATEGIES_AGREE
    assert compatible.microstructure_yes_probability == Decimal("0.55")
    assert compatible.dynamics_yes_probability == Decimal("0.55")
    assert skewed.state is PredictionComparisonState.INCOMPATIBLE_TIME
    assert skewed.reason_codes == ("PREDICTION_TIME_SKEW_EXCEEDED",)


def test_comparison_rejects_missing_exact_rule_lineage() -> None:
    value = compare_prediction_evidence(
        cycle_id="cycle",
        values=(
            _prediction("market-consensus-baseline-v1", conservative_yes="0.50"),
            _prediction("kalshi-market-dynamics-v1", rule_hash=None),
        ),
    )

    assert value.state is PredictionComparisonState.INCOMPATIBLE_EVIDENCE
    assert value.reason_codes == ("EXACT_MARKET_EVENT_OR_RULE_LINEAGE_MISMATCH",)


def test_comparison_marks_opposite_conservative_directions_as_disagreement() -> None:
    value = compare_prediction_evidence(
        cycle_id="cycle",
        values=(
            _prediction("market-consensus-baseline-v1", conservative_yes="0.50"),
            _prediction("kalshi-orderbook-microstructure-v1", conservative_yes="0.53"),
            _prediction("kalshi-market-dynamics-v1", conservative_yes="0.47"),
        ),
    )

    assert value.state is PredictionComparisonState.STRATEGIES_DISAGREE
    assert value.reason_codes == ("DIRECTIONAL_CONSERVATIVE_DISAGREEMENT",)
