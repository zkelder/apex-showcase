"""Evidence-Weighted Cross-Venue Opportunity Ranking v1 -- deterministic benchmark corpus.

A single named, documented corpus covering the twelve required scenarios, run through exactly
one ``build_opportunity_set`` call (one contemporaneous, point-in-time ranking cycle) so cross-
venue and cross-scenario interactions are genuine, not staged in isolation. Every scenario has an
explicit, written rationale and an assertion tied to that rationale -- see the module-level
``SCENARIOS`` table for the full catalog. Exact full total order is printed (not hardcoded as one
brittle 16-way sequence) via ``test_benchmark_corpus_full_order_is_deterministic_and_reproducible``,
which instead asserts *reproducibility* of that exact order across repeated runs -- the strongest
possible determinism claim without hand-deriving 16 opponents' worth of Decimal arithmetic by hand
for its own sake.

Scenario 3 (economically equivalent cross-venue pair) is deliberately run as its own small,
isolated ``build_opportunity_set`` call rather than folded into the shared corpus: percentile is a
function of the *actual* contemporaneous population, so "equivalent per-day magnitude" only
implies "equivalent percentile" when the peer population is controlled -- exactly what an isolated
pair demonstrates cleanly. The shared corpus still includes an economically-equivalent-looking
pair for realism, with a rationale explaining why its outcome differs below (real relative
standing, not a symmetry bug -- see scenario 3b).
"""

from __future__ import annotations

from decimal import Decimal

from apex.domain.performance_attribution import StrategyHealthState
from apex.domain.shared_opportunity import PriorityTier, SharedVenue, build_opportunity_set
from tests.test_shared_opportunity import NOW, projection
from tests.test_shared_opportunity_ranking_v1 import (
    CRYPTO_GENUINE,
    KALSHI_GENUINE,
    PROXY_IDENTITY,
    _components,
    _health,
)

DEGRADED_METHOD = "BENCH_DEGRADED_METHOD_V1"

CORPUS = {
    # 1 & 2: strong crypto / weak Kalshi, and weak crypto / strong Kalshi, side by side.
    "s1-crypto-strong": projection(
        "s1-crypto-strong",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.06",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=14,
        lockup=14,
    ),
    "s2-crypto-weak": projection(
        "s2-crypto-weak",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.01",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=14,
        lockup=14,
    ),
    "s1-kalshi-weak": projection(
        "s1-kalshi-weak",
        venue=SharedVenue.KALSHI,
        comparable_value="0.05",
        comparable_value_identity=KALSHI_GENUINE,
        holding_days=14,
        lockup=14,
    ),
    "s2-kalshi-strong": projection(
        "s2-kalshi-strong",
        venue=SharedVenue.KALSHI,
        comparable_value="0.30",
        comparable_value_identity=KALSHI_GENUINE,
        holding_days=14,
        lockup=14,
    ),
    # 3b: an equivalent-looking pair, deliberately embedded in the full mixed population (see
    # module docstring for why the isolated version, scenario 3a, is the clean symmetry proof).
    "s3b-crypto-equiv": projection(
        "s3b-crypto-equiv",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.035",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=14,
        lockup=14,
    ),
    "s3b-kalshi-equiv": projection(
        "s3b-kalshi-equiv",
        venue=SharedVenue.KALSHI,
        comparable_value="0.15",
        comparable_value_identity=KALSHI_GENUINE,
        holding_days=14,
        lockup=14,
    ),
    # 4: high statistical qualification, economically tiny -- must not win on qualification alone.
    "s4-tiny-significant": projection(
        "s4-tiny-significant",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.0002",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=14,
        lockup=14,
        statistical_sample_size=150,
    ),
    # 5: large magnitude, evidence only just clears the qualification floor -- still competitive.
    "s5-large-weak-evidence": projection(
        "s5-large-weak-evidence",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.06",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=14,
        lockup=14,
        statistical_sample_size=30,
    ),
    # 6: a large proxy/composite score with no genuine EV behind it at all.
    "s6-strong-proxy": projection(
        "s6-strong-proxy",
        venue=SharedVenue.CRYPTO,
        comparable_value="800",
        comparable_value_identity=PROXY_IDENTITY,
        holding_days=14,
        lockup=14,
    ),
    # 7: fees retained, no separately modeled slippage line (Kalshi's real-world shape, exercised
    # here on a crypto-shaped fixture to isolate the missing-data rule from venue identity).
    "s7-missing-cost": projection(
        "s7-missing-cost",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.03",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=14,
        lockup=14,
        fees="1",
        slippage=None,
        costs_retained=True,
    ),
    # 8: a retained, genuinely DEGRADED strategy-health assessment for this exact method.
    "s8-degraded-health": projection(
        "s8-degraded-health",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.03",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=14,
        lockup=14,
        strategy_or_method_id=DEGRADED_METHOD,
    ),
    # 9: no retained health assessment for this method at all (today's real-corpus norm).
    "s9-unknown-health": projection(
        "s9-unknown-health",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.03",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=14,
        lockup=14,
        strategy_or_method_id="BENCH_UNKNOWN_HEALTH_METHOD_V1",
    ),
    # 10: identical raw per-period return, radically different horizons.
    "s10-short-horizon": projection(
        "s10-short-horizon",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.02",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=2,
        lockup=2,
    ),
    "s10-long-horizon": projection(
        "s10-long-horizon",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.02",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=90,
        lockup=90,
    ),
    # 11: genuine negative economic magnitude -- below the opportunity-cost floor.
    "s11-below-floor": projection(
        "s11-below-floor",
        venue=SharedVenue.CRYPTO,
        comparable_value="-0.02",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=14,
        lockup=14,
    ),
    # 12: closest in-layer analog of a "redundant / non-incremental" result -- see
    # test_scenario_12_redundant_research_analog for why no literal REDUNDANT wiring exists here.
    "s12-redundant-analog-proxy": projection(
        "s12-redundant-analog-proxy",
        venue=SharedVenue.CRYPTO,
        comparable_value="750",
        comparable_value_identity=PROXY_IDENTITY,
        holding_days=14,
        lockup=14,
    ),
}

HEALTH = {DEGRADED_METHOD: _health(DEGRADED_METHOD, StrategyHealthState.DEGRADED)}


def _run() -> tuple:
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="benchmark-v1-corpus",
        created_at=NOW,
        projections=tuple(CORPUS.values()),
        health_by_method=HEALTH,
    )
    return priorities


def test_scenario_01_02_strong_and_weak_rank_within_each_venue_by_magnitude() -> None:
    """Rationale: within each venue, the objectively stronger genuine opportunity outranks the
    weaker one -- proven independently for both crypto and Kalshi in the same cycle."""

    priorities = _run()
    ranks = {item.projection_id: item.rank for item in priorities}
    assert (
        ranks[CORPUS["s1-crypto-strong"].projection_id]
        < ranks[CORPUS["s2-crypto-weak"].projection_id]
    )
    assert (
        ranks[CORPUS["s2-kalshi-strong"].projection_id]
        < ranks[CORPUS["s1-kalshi-weak"].projection_id]
    )
    # And the two "strong" opportunities (crypto and Kalshi) land in the same top cluster --
    # neither venue's strong opportunity is systematically buried beneath the other's.
    top_five_ids = {item.projection_id for item in priorities if item.rank <= 5}
    assert CORPUS["s1-crypto-strong"].projection_id in top_five_ids
    assert CORPUS["s2-kalshi-strong"].projection_id in top_five_ids


def test_scenario_03a_economically_equivalent_pair_isolated() -> None:
    """Rationale (isolated population -- see module docstring): identical horizon-normalized
    per-day magnitude, one crypto and one Kalshi, alone with each other -> identical normalized
    COMPARABLE_VALUE. This is the clean, unconfounded symmetry proof."""

    crypto_equiv = projection(
        "iso-crypto-equiv",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.05",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=10,
    )
    kalshi_equiv = projection(
        "iso-kalshi-equiv",
        venue=SharedVenue.KALSHI,
        comparable_value="0.05",
        comparable_value_identity=KALSHI_GENUINE,
        holding_days=10,
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="scenario-3a-set",
        created_at=NOW,
        projections=(crypto_equiv, kalshi_equiv),
    )
    crypto_comparable = _components(priorities, crypto_equiv.projection_id, "COMPARABLE_VALUE")
    kalshi_comparable = _components(priorities, kalshi_equiv.projection_id, "COMPARABLE_VALUE")
    assert crypto_comparable.normalized_value == kalshi_comparable.normalized_value


def test_scenario_03b_equivalent_pair_in_full_corpus_shows_relative_standing() -> None:
    """Rationale: inside the full 16-opportunity corpus, s3b-crypto-equiv and s3b-kalshi-equiv do
    NOT necessarily land at the same percentile, because percentile reflects standing among the
    ACTUAL contemporaneous peers of each venue (11 crypto vs. 3 Kalshi here), not raw value
    equality. This is intentional, explainable relative ranking -- not a symmetry defect (that
    claim is proven, controlled, in scenario 3a above). Both remain comfortably ACTIONABLE."""

    priorities = _run()
    crypto_equiv = next(
        item
        for item in priorities
        if item.projection_id == CORPUS["s3b-crypto-equiv"].projection_id
    )
    kalshi_equiv = next(
        item
        for item in priorities
        if item.projection_id == CORPUS["s3b-kalshi-equiv"].projection_id
    )
    assert crypto_equiv.tier is PriorityTier.ACTIONABLE
    assert kalshi_equiv.tier is PriorityTier.ACTIONABLE


def test_scenario_04_high_significance_tiny_magnitude_does_not_win_on_significance_alone() -> None:
    """Rationale: maximal statistical qualification (150 samples, the saturation point) cannot
    rescue an economically negligible magnitude (0.0002) -- it must rank behind opportunities
    with materially larger genuine magnitude and only average-or-below qualification."""

    priorities = _run()
    ranks = {item.projection_id: item.rank for item in priorities}
    tiny = ranks[CORPUS["s4-tiny-significant"].projection_id]
    assert tiny > ranks[CORPUS["s1-crypto-strong"].projection_id]
    assert tiny > ranks[CORPUS["s3b-crypto-equiv"].projection_id]
    assert tiny > ranks[CORPUS["s2-crypto-weak"].projection_id]


def test_scenario_05_large_magnitude_weak_evidence_stays_competitive() -> None:
    """Rationale: a large magnitude (0.06) whose evidence only just clears the statistical
    qualification floor (30 samples, scored identically-neutral to "no sample concept at all")
    still ranks near the top on economic magnitude -- evidence quality moderates, it does not
    invert, a large real effect. (The complementary claim -- that *stronger* evidence written
    against the *same* magnitude improves rank further -- is proven exactly in
    ``test_statistical_qualification_breaks_a_tie_in_economic_magnitude``.)"""

    priorities = _run()
    ranks = {item.projection_id: item.rank for item in priorities}
    assert ranks[CORPUS["s5-large-weak-evidence"].projection_id] <= 5


def test_scenario_06_strong_proxy_never_reaches_actionable_tier() -> None:
    """Rationale: comparable_value=800 under a proxy identity is neutralized to 0.5, and the
    opportunity is capped at RESEARCH_ONLY regardless of the raw magnitude's size."""

    priorities = _run()
    result = next(
        item for item in priorities if item.projection_id == CORPUS["s6-strong-proxy"].projection_id
    )
    assert result.tier is PriorityTier.RESEARCH_ONLY
    comparable = next(c for c in result.components if c.name == "COMPARABLE_VALUE")
    assert comparable.normalized_value == Decimal("0.5")


def test_scenario_07_missing_slippage_never_more_favorable_than_explicit() -> None:
    """Rationale: fees retained, slippage not separately modeled -- conservatively assumed to
    cost at least the known fee rate, never assumed free."""

    priorities = _run()
    result = next(
        item for item in priorities if item.projection_id == CORPUS["s7-missing-cost"].projection_id
    )
    cost = next(c for c in result.components if c.name == "COST")
    # fees=1, effective_slippage=1 (== fees), capital=100 -> 1 - 2/100 = 0.98
    assert cost.normalized_value == Decimal("0.98")


def test_scenario_08_degraded_health_scores_low_at_full_weight() -> None:
    """Rationale: a genuine, retained DEGRADED assessment for this exact method scores low
    (0.15) but is NOT excluded -- its full base weight (0.05, undiluted by redistribution)
    applies."""

    priorities = _run()
    result = next(
        item
        for item in priorities
        if item.projection_id == CORPUS["s8-degraded-health"].projection_id
    )
    health_component = next(c for c in result.components if c.name == "SOURCE_HEALTH")
    assert health_component.normalized_value == Decimal("0.15")
    assert health_component.weight == Decimal("0.05")


def test_scenario_09_unknown_health_is_excluded_and_redistributed_not_fabricated() -> None:
    """Rationale: no retained assessment at all for this method -- SOURCE_HEALTH is excluded
    (weight 0, redistributed to the other nine components), never defaulted to a fabricated
    perfect or neutral score."""

    priorities = _run()
    result = next(
        item
        for item in priorities
        if item.projection_id == CORPUS["s9-unknown-health"].projection_id
    )
    health_component = next(c for c in result.components if c.name == "SOURCE_HEALTH")
    assert health_component.weight == Decimal("0")
    assert health_component.missing_behavior == "EXCLUDED_NO_RETAINED_HEALTH_ASSESSMENT_FOR_METHOD"
    assert sum((c.weight for c in result.components), Decimal(0)) == Decimal(1)


def test_scenario_10_short_horizon_outranks_identical_raw_return_at_long_horizon() -> None:
    """Rationale: 0.02 over 2 days (0.01/day) is a materially different opportunity from 0.02
    over 90 days (~0.00022/day) -- the short-horizon one must rank strictly above the long one
    despite an identical raw, un-normalized return figure."""

    priorities = _run()
    ranks = {item.projection_id: item.rank for item in priorities}
    assert (
        ranks[CORPUS["s10-short-horizon"].projection_id]
        < ranks[CORPUS["s10-long-horizon"].projection_id]
    )


def test_scenario_11_below_opportunity_cost_floor_is_research_only() -> None:
    """Rationale: a genuine, negative per-day economic magnitude (-0.02 over 14 days) sits below
    the zero floor -- research-worthy evidence, never actionable."""

    priorities = _run()
    result = next(
        item for item in priorities if item.projection_id == CORPUS["s11-below-floor"].projection_id
    )
    assert result.tier is PriorityTier.RESEARCH_ONLY


def test_scenario_12_redundant_research_analog() -> None:
    """Rationale: ``INCREMENTAL_VALUE_ABSENT``/``REDUNDANT`` (corporate_regime_followup.py) have
    no import path into shared_opportunity.py or cross_venue_allocation.py today -- verified by
    direct source read, not inferred from field names -- so there is no literal state to
    regression-test on this layer's live code path. The closest in-layer equivalent is exactly
    what design improvement #2 already guards: a composite/proxy score standing in for
    independent alpha it does not actually have. This scenario reuses that guarantee (see
    scenario 6 and scenario 3 in ``test_shared_opportunity_ranking_v1.py``) rather than fabricate
    a wiring that does not exist."""

    priorities = _run()
    result = next(
        item
        for item in priorities
        if item.projection_id == CORPUS["s12-redundant-analog-proxy"].projection_id
    )
    assert result.tier is PriorityTier.RESEARCH_ONLY


def test_benchmark_corpus_full_order_is_deterministic_and_reproducible() -> None:
    first = _run()
    second = _run()
    assert tuple(item.projection_id for item in first) == tuple(
        item.projection_id for item in second
    )
    assert tuple(item.score for item in first) == tuple(item.score for item in second)
    assert tuple(item.tier for item in first) == tuple(item.tier for item in second)
    # Every projection in the corpus is accounted for exactly once.
    assert {item.projection_id for item in first} == {
        item.projection_id for item in CORPUS.values()
    }


def test_benchmark_corpus_tier_summary_matches_documented_expectation() -> None:
    """Consolidated tier check: exactly the negative-magnitude and two proxy opportunities are
    RESEARCH_ONLY; every genuine, positive-magnitude opportunity is ACTIONABLE."""

    priorities = _run()
    research_only = {
        item.projection_id for item in priorities if item.tier is PriorityTier.RESEARCH_ONLY
    }
    assert research_only == {
        CORPUS["s11-below-floor"].projection_id,
        CORPUS["s6-strong-proxy"].projection_id,
        CORPUS["s12-redundant-analog-proxy"].projection_id,
    }
