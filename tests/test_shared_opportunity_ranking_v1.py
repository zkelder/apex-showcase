"""Evidence-Weighted Cross-Venue Opportunity Ranking v1 -- focused deterministic tests.

Covers, in order: EV sensitivity, statistical-evidence sensitivity, venue symmetry, percentile
normalization, proxy-vs-real magnitude, source-health behavior, missing-data conservatism,
horizon normalization, the opportunity-cost floor, and determinism. Reuses the ``projection()``
fixture helper from ``tests/test_shared_opportunity.py`` (extended, not replaced, for this
milestone) so both files stay in sync on construction defaults.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from apex.domain.performance_attribution import (
    STRATEGY_HEALTH_POLICY_V1,
    PaperEvidenceClass,
    StrategyHealthAssessment,
    StrategyHealthState,
)
from apex.domain.shared_opportunity import (
    GENUINE_ECONOMIC_MAGNITUDE_IDENTITIES,
    OPPORTUNITY_COST_FLOOR_V1,
    EligibilityDecision,
    EligibilityState,
    PriorityTier,
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
    evaluate_eligibility,
    prioritize,
)
from apex.domain.values import content_digest
from tests.test_shared_opportunity import NOW, projection

CRYPTO_GENUINE = "STRICTLY_PRIOR_WALK_FORWARD_EXPECTED_NET_RETURN"
KALSHI_GENUINE = "CONSERVATIVE_NET_LOWER_BOUND_OVER_MODELED_COST"
PROXY_IDENTITY = "CRYPTO_GROSS_RESEARCH_SCORE_NOT_EXPECTED_RETURN"


def _health(
    method_id: str, state: StrategyHealthState, *, version: str = "1"
) -> StrategyHealthAssessment:
    values = (
        f"{method_id}-health",
        STRATEGY_HEALTH_POLICY_V1,
        method_id,
        version,
        PaperEvidenceClass.GENUINE_PAPER,
        NOW - timedelta(days=7),
        NOW,
        5,
        4,
        4,
        Decimal("0.01"),
        Decimal("0.01"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0.9"),
        Decimal("1"),
        Decimal("0"),
        (),
        (),
        (),
        state,
        "TEST_FIXTURE",
        ("attr-1",),
    )
    return StrategyHealthAssessment(*values, content_digest(values))


def _force_eligible(item: SharedOpportunityProjection) -> EligibilityDecision:
    """Construct an ELIGIBLE decision directly, bypassing ``evaluate_eligibility``'s own
    (separate, unchanged-by-this-milestone) missing-data gate. ``maximum_loss``/costs-missing
    fixtures are unreachable via the standard ``build_opportunity_set`` -> ``evaluate_eligibility``
    path today (that gate already BLOCKED_MISSING_EVIDENCE's them) -- these tests instead exercise
    ``prioritize()``'s own defense-in-depth directly, since it does not (and must not) re-trust
    that every caller-supplied ELIGIBLE decision came from that one standard gate."""

    payload = (f"{item.projection_id}-forced-eligible", item.projection_id, "TEST", NOW, "ELIGIBLE")
    return EligibilityDecision(
        decision_id=payload[0],
        projection_id=item.projection_id,
        policy_id="TEST_FORCED_ELIGIBLE",
        evaluated_at=NOW,
        state=EligibilityState.ELIGIBLE,
        reasons=("FORCED_ELIGIBLE_FOR_TEST",),
        canonical_hash=content_digest(payload),
    )


def _components(priorities: tuple, projection_id: str, name: str):  # type: ignore[no-untyped-def]
    result = next(item for item in priorities if item.projection_id == projection_id)
    return next(item for item in result.components if item.name == name)


# ---------------------------------------------------------------------------
# 1. EV sensitivity
# ---------------------------------------------------------------------------


def test_ev_sensitivity_better_genuine_return_improves_rank_all_else_equal() -> None:
    weak = projection(
        "weak-ev",
        comparable_value="0.01",
        comparable_value_identity=CRYPTO_GENUINE,
        expected_net_return="0.01",
        holding_days=30,
        lockup=30,
    )
    strong = projection(
        "strong-ev",
        comparable_value="0.05",
        comparable_value_identity=CRYPTO_GENUINE,
        expected_net_return="0.05",
        holding_days=30,
        lockup=30,
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="ev-set", created_at=NOW, projections=(weak, strong)
    )
    assert priorities[0].projection_id == strong.projection_id
    assert priorities[0].tier is PriorityTier.ACTIONABLE
    assert priorities[1].tier is PriorityTier.ACTIONABLE
    strong_comparable = _components(priorities, strong.projection_id, "COMPARABLE_VALUE")
    weak_comparable = _components(priorities, weak.projection_id, "COMPARABLE_VALUE")
    assert strong_comparable.normalized_value > weak_comparable.normalized_value


def test_ev_sensitivity_never_worsens_rank_holding_everything_else_fixed() -> None:
    """Monotonicity check across three genuine-EV levels: strictly increasing comparable_value
    never produces a strictly worse rank."""

    low = projection("mono-low", comparable_value="0.01", comparable_value_identity=CRYPTO_GENUINE)
    mid = projection("mono-mid", comparable_value="0.03", comparable_value_identity=CRYPTO_GENUINE)
    high = projection(
        "mono-high", comparable_value="0.05", comparable_value_identity=CRYPTO_GENUINE
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="mono-set", created_at=NOW, projections=(low, mid, high)
    )
    ranks = {item.projection_id: item.rank for item in priorities}
    assert ranks[high.projection_id] < ranks[mid.projection_id] < ranks[low.projection_id]


# ---------------------------------------------------------------------------
# 2. Statistical-evidence sensitivity
# ---------------------------------------------------------------------------


def test_statistical_qualification_breaks_a_tie_in_economic_magnitude() -> None:
    low_n = projection(
        "low-n",
        comparable_value="0.03",
        comparable_value_identity=CRYPTO_GENUINE,
        statistical_sample_size=30,
    )
    high_n = projection(
        "high-n",
        comparable_value="0.03",
        comparable_value_identity=CRYPTO_GENUINE,
        statistical_sample_size=150,
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="stat-set", created_at=NOW, projections=(low_n, high_n)
    )
    comparable_low = _components(priorities, low_n.projection_id, "COMPARABLE_VALUE")
    comparable_high = _components(priorities, high_n.projection_id, "COMPARABLE_VALUE")
    assert comparable_low.normalized_value == comparable_high.normalized_value == Decimal("0.5")
    assert priorities[0].projection_id == high_n.projection_id
    stat_high = _components(priorities, high_n.projection_id, "STATISTICAL_QUALIFICATION")
    stat_low = _components(priorities, low_n.projection_id, "STATISTICAL_QUALIFICATION")
    assert stat_high.normalized_value == Decimal("1")
    assert stat_low.normalized_value == Decimal("0.5")


def test_statistical_qualification_does_not_dominate_a_large_economic_magnitude_gap() -> None:
    big_ev_low_n = projection(
        "big-ev-low-n",
        comparable_value="0.06",
        comparable_value_identity=CRYPTO_GENUINE,
        statistical_sample_size=30,
    )
    small_ev_high_n = projection(
        "small-ev-high-n",
        comparable_value="0.02",
        comparable_value_identity=CRYPTO_GENUINE,
        statistical_sample_size=150,
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="stat-dominance-set",
        created_at=NOW,
        projections=(big_ev_low_n, small_ev_high_n),
    )
    assert priorities[0].projection_id == big_ev_low_n.projection_id


def test_statistical_qualification_none_is_neutral_not_penalized_or_rewarded() -> None:
    """Kalshi's model-free guarantees and the crypto proxy method have no sample-size concept
    at all -- None must land exactly at the same neutral point real evidence would need to beat
    or fall short of, never fabricated as unqualified (0) nor as abundant (1)."""

    kalshi = projection(
        "kalshi-no-stat",
        venue=SharedVenue.KALSHI,
        comparable_value="0.03",
        comparable_value_identity=KALSHI_GENUINE,
        statistical_sample_size=None,
    )
    result = prioritize(
        (kalshi,),
        (evaluate_eligibility(kalshi, evaluated_at=NOW),),
        opportunity_set_id="stat-neutral-set",
    )
    component = next(
        item for item in result[0].components if item.name == "STATISTICAL_QUALIFICATION"
    )
    assert component.normalized_value == Decimal("0.5")
    assert component.missing_behavior == "NEUTRAL_0.5_NO_SAMPLE_SIZE_CONCEPT_FOR_THIS_METHOD"


# ---------------------------------------------------------------------------
# 3. Venue symmetry
# ---------------------------------------------------------------------------


def test_venue_symmetry_identical_missing_slippage_scores_identically() -> None:
    crypto = projection(
        "sym-crypto",
        venue=SharedVenue.CRYPTO,
        fees="2",
        slippage=None,
        capital="100",
        costs_retained=True,
    )
    kalshi = projection(
        "sym-kalshi",
        venue=SharedVenue.KALSHI,
        fees="2",
        slippage=None,
        capital="100",
        costs_retained=True,
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="sym-set", created_at=NOW, projections=(crypto, kalshi)
    )
    crypto_cost = _components(priorities, crypto.projection_id, "COST")
    kalshi_cost = _components(priorities, kalshi.projection_id, "COST")
    assert crypto_cost.normalized_value == kalshi_cost.normalized_value
    assert crypto_cost.weighted_value == kalshi_cost.weighted_value


def test_venue_symmetry_equivalent_relative_standing_scores_equally_regardless_of_venue() -> None:
    """Pure percentile normalization deliberately encodes *relative standing within your own
    venue*, not cross-venue absolute magnitude (that is the entire point of design improvement
    #1 -- it is what stops incompatible raw units from being compared directly). Its correct
    symmetry property is therefore: whichever opportunity is objectively top-of-its-own-venue
    reaches the exact same COMPARABLE_VALUE score as the other venue's top opportunity, with the
    formula never favoring one venue's identity over the other's. (An overall SCORE tie between
    two equally-top-percentile opportunities is then broken by the existing, unrelated,
    deterministic tie_break_key -- not a hidden venue preference; that tie-break is exercised
    and asserted separately by
    ``test_priority_is_decimal_deterministic_and_ties_use_exact_source_identity``.)"""

    crypto_low = projection(
        "flip-crypto-low",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.01",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=30,
    )
    crypto_high = projection(
        "flip-crypto-high",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.06",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=30,
    )
    kalshi_low = projection(
        "flip-kalshi-low",
        venue=SharedVenue.KALSHI,
        comparable_value="0.20",
        comparable_value_identity=KALSHI_GENUINE,
        holding_days=30,
    )
    kalshi_high = projection(
        "flip-kalshi-high",
        venue=SharedVenue.KALSHI,
        comparable_value="0.90",
        comparable_value_identity=KALSHI_GENUINE,
        holding_days=30,
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="flip-symmetry-set",
        created_at=NOW,
        projections=(crypto_low, crypto_high, kalshi_low, kalshi_high),
    )
    crypto_high_comparable = _components(priorities, crypto_high.projection_id, "COMPARABLE_VALUE")
    kalshi_high_comparable = _components(priorities, kalshi_high.projection_id, "COMPARABLE_VALUE")
    crypto_low_comparable = _components(priorities, crypto_low.projection_id, "COMPARABLE_VALUE")
    kalshi_low_comparable = _components(priorities, kalshi_low.projection_id, "COMPARABLE_VALUE")
    # Numerically very different raw magnitudes (0.06 vs 0.90), but identical *relative* standing
    # (sole top-of-two in each venue) -> identical normalized COMPARABLE_VALUE, proving the
    # formula itself carries no venue-identity bias.
    assert (
        crypto_high_comparable.normalized_value
        == kalshi_high_comparable.normalized_value
        == (Decimal("0.75"))
    )
    assert (
        crypto_low_comparable.normalized_value
        == kalshi_low_comparable.normalized_value
        == (Decimal("0.25"))
    )


def test_venue_symmetry_missing_maximum_loss_never_more_favorable_across_venues() -> None:
    """maximum_loss=None is BLOCKED_MISSING_EVIDENCE under the standard (unchanged-by-this-
    milestone) eligibility gate for both venues equally -- so this exercises prioritize()'s own
    defense-in-depth directly, the same way an eligibility-policy change or a non-standard caller
    could reach it, and confirms it treats both venues identically either way."""

    crypto = projection("nomax-crypto", venue=SharedVenue.CRYPTO, maximum_loss=None, capital="100")
    kalshi = projection("nomax-kalshi", venue=SharedVenue.KALSHI, maximum_loss=None, capital="100")
    priorities = prioritize(
        (crypto, kalshi),
        (_force_eligible(crypto), _force_eligible(kalshi)),
        opportunity_set_id="venue-symmetry-set",
    )
    crypto_downside = _components(priorities, crypto.projection_id, "DOWNSIDE")
    kalshi_downside = _components(priorities, kalshi.projection_id, "DOWNSIDE")
    assert crypto_downside.normalized_value == kalshi_downside.normalized_value == Decimal("0.5")
    assert crypto_downside.missing_behavior == kalshi_downside.missing_behavior


# ---------------------------------------------------------------------------
# 4. Percentile normalization
# ---------------------------------------------------------------------------


def test_percentile_same_venue_ordering_and_exact_tie_aware_values() -> None:
    low = projection("perc-low", comparable_value="0.01", comparable_value_identity=CRYPTO_GENUINE)
    mid = projection("perc-mid", comparable_value="0.03", comparable_value_identity=CRYPTO_GENUINE)
    high = projection(
        "perc-high", comparable_value="0.05", comparable_value_identity=CRYPTO_GENUINE
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="perc-set", created_at=NOW, projections=(low, mid, high)
    )
    assert _components(
        priorities, high.projection_id, "COMPARABLE_VALUE"
    ).normalized_value == Decimal("0.83333333")
    assert _components(
        priorities, mid.projection_id, "COMPARABLE_VALUE"
    ).normalized_value == Decimal("0.5")
    assert _components(
        priorities, low.projection_id, "COMPARABLE_VALUE"
    ).normalized_value == Decimal("0.16666667")


def test_percentile_cross_venue_pools_are_independent_per_venue() -> None:
    """A crypto opportunity's percentile must depend only on its crypto peers, never shift
    because of how many (or how strong) Kalshi opportunities are also in the same batch."""

    crypto_low = projection(
        "cv-crypto-low",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.01",
        comparable_value_identity=CRYPTO_GENUINE,
    )
    crypto_high = projection(
        "cv-crypto-high",
        venue=SharedVenue.CRYPTO,
        comparable_value="0.05",
        comparable_value_identity=CRYPTO_GENUINE,
    )
    kalshi_only = projection(
        "cv-kalshi-only",
        venue=SharedVenue.KALSHI,
        comparable_value="0.90",
        comparable_value_identity=KALSHI_GENUINE,
    )
    _, _, with_kalshi, _ = build_opportunity_set(
        opportunity_set_id="cv-with-kalshi",
        created_at=NOW,
        projections=(crypto_low, crypto_high, kalshi_only),
    )
    _, _, without_kalshi, _ = build_opportunity_set(
        opportunity_set_id="cv-without-kalshi",
        created_at=NOW,
        projections=(crypto_low, crypto_high),
    )
    assert _components(
        with_kalshi, crypto_high.projection_id, "COMPARABLE_VALUE"
    ).normalized_value == (
        _components(without_kalshi, crypto_high.projection_id, "COMPARABLE_VALUE").normalized_value
    )
    # And Kalshi's huge raw 0.90 does not automatically outrank crypto just by being numerically
    # larger -- it is alone in its own venue pool, so it gets the neutral no-peer treatment.
    kalshi_comparable = _components(with_kalshi, kalshi_only.projection_id, "COMPARABLE_VALUE")
    assert kalshi_comparable.normalized_value == Decimal("0.5")
    assert kalshi_comparable.missing_behavior == "INSUFFICIENT_VENUE_POPULATION_FOR_PERCENTILE"


def test_percentile_deterministic_ties() -> None:
    first = projection("tie-a", comparable_value="0.04", comparable_value_identity=CRYPTO_GENUINE)
    second = projection("tie-b", comparable_value="0.04", comparable_value_identity=CRYPTO_GENUINE)
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="perc-tie-set", created_at=NOW, projections=(first, second)
    )
    a = _components(priorities, first.projection_id, "COMPARABLE_VALUE")
    b = _components(priorities, second.projection_id, "COMPARABLE_VALUE")
    assert a.normalized_value == b.normalized_value == Decimal("0.5")
    # Overall rank still deterministically resolved via the existing source-identity tie break.
    assert priorities[0].projection_id == "shared-tie-a"
    assert priorities[1].projection_id == "shared-tie-b"


def test_percentile_sparse_population_of_two() -> None:
    low = projection(
        "sparse-low", comparable_value="0.01", comparable_value_identity=CRYPTO_GENUINE
    )
    high = projection(
        "sparse-high", comparable_value="0.02", comparable_value_identity=CRYPTO_GENUINE
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="sparse-set", created_at=NOW, projections=(low, high)
    )
    assert _components(
        priorities, high.projection_id, "COMPARABLE_VALUE"
    ).normalized_value == Decimal("0.75")
    assert _components(
        priorities, low.projection_id, "COMPARABLE_VALUE"
    ).normalized_value == Decimal("0.25")


def test_percentile_single_opportunity_population_is_neutral_not_maximal() -> None:
    lone = projection("lone", comparable_value="0.05", comparable_value_identity=CRYPTO_GENUINE)
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="lone-set", created_at=NOW, projections=(lone,)
    )
    component = _components(priorities, lone.projection_id, "COMPARABLE_VALUE")
    assert component.normalized_value == Decimal("0.5")
    assert component.missing_behavior == "INSUFFICIENT_VENUE_POPULATION_FOR_PERCENTILE"


def test_percentile_no_eligible_peer_case_excludes_proxy_neighbors_from_the_pool() -> None:
    """One genuine opportunity surrounded by several eligible PROXY opportunities in the same
    venue still has zero genuine peers -- proxies must never silently pad the percentile pool."""

    genuine = projection(
        "solo-genuine", comparable_value="0.05", comparable_value_identity=CRYPTO_GENUINE
    )
    proxies = tuple(
        projection(
            f"solo-proxy-{i}", comparable_value=str(i), comparable_value_identity=PROXY_IDENTITY
        )
        for i in range(3)
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="solo-set", created_at=NOW, projections=(genuine, *proxies)
    )
    component = _components(priorities, genuine.projection_id, "COMPARABLE_VALUE")
    assert component.normalized_value == Decimal("0.5")
    assert component.missing_behavior == "INSUFFICIENT_VENUE_POPULATION_FOR_PERCENTILE"


# ---------------------------------------------------------------------------
# 5. Proxy-vs-real magnitude
# ---------------------------------------------------------------------------


def test_proxy_magnitude_cannot_reach_actionable_tier_regardless_of_size() -> None:
    huge_proxy = projection(
        "huge-proxy", comparable_value="1000", comparable_value_identity=PROXY_IDENTITY
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="huge-proxy-set", created_at=NOW, projections=(huge_proxy,)
    )
    result = priorities[0]
    assert result.tier is PriorityTier.RESEARCH_ONLY
    assert any(reason.startswith("NO_GENUINE_ECONOMIC_MAGNITUDE") for reason in result.tier_reasons)
    comparable = next(item for item in result.components if item.name == "COMPARABLE_VALUE")
    assert comparable.normalized_value == Decimal("0.5")


def test_proxy_can_still_outscore_but_never_out_tier_a_genuine_opportunity() -> None:
    """Tier is orthogonal to score: construct a proxy that wins on raw score (favorable cost/
    liquidity/lockup elsewhere) while a genuine opportunity scores lower overall -- the genuine
    one must still be the only one ever eligible for ACTIONABLE."""

    strong_proxy = projection(
        "strong-proxy",
        comparable_value="500",
        comparable_value_identity=PROXY_IDENTITY,
        fees="0.1",
        slippage="0.1",
        lockup=1,
        holding_days=1,
    )
    weak_genuine = projection(
        "weak-genuine",
        comparable_value="0.001",
        comparable_value_identity=CRYPTO_GENUINE,
        fees="10",
        slippage="10",
        lockup=300,
        holding_days=300,
        capital="100",
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="proxy-vs-genuine-set",
        created_at=NOW,
        projections=(strong_proxy, weak_genuine),
    )
    by_id = {item.projection_id: item for item in priorities}
    assert by_id[strong_proxy.projection_id].tier is PriorityTier.RESEARCH_ONLY
    # The proxy's raw score is not artificially suppressed -- it can legitimately outrank the
    # genuine opportunity (favorable cost/lockup elsewhere is real signal, not a tier violation).
    assert priorities[0].projection_id == strong_proxy.projection_id
    # But even though it scores worse and ranks second, the genuine opportunity -- with any
    # positive genuine per-day economic magnitude at all -- is the only one ever ACTIONABLE.
    assert by_id[weak_genuine.projection_id].tier is PriorityTier.ACTIONABLE
    actionable = [item for item in priorities if item.tier is PriorityTier.ACTIONABLE]
    assert [item.projection_id for item in actionable] == [weak_genuine.projection_id]


# ---------------------------------------------------------------------------
# 6. Source-health behavior
# ---------------------------------------------------------------------------


def test_source_health_genuine_healthy_evidence_scores_maximally_at_base_weight() -> None:
    opportunity = projection("healthy-method", strategy_or_method_id="TEST_HEALTHY_METHOD_V1")
    health = {
        "TEST_HEALTHY_METHOD_V1": _health("TEST_HEALTHY_METHOD_V1", StrategyHealthState.HEALTHY)
    }
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="healthy-set",
        created_at=NOW,
        projections=(opportunity,),
        health_by_method=health,
    )
    component = _components(priorities, opportunity.projection_id, "SOURCE_HEALTH")
    assert component.normalized_value == Decimal("1")
    assert component.weight == Decimal("0.05")
    assert component.missing_behavior is None


def test_source_health_degraded_evidence_scores_low_but_is_not_excluded() -> None:
    opportunity = projection("degraded-method", strategy_or_method_id="TEST_DEGRADED_METHOD_V1")
    health = {
        "TEST_DEGRADED_METHOD_V1": _health("TEST_DEGRADED_METHOD_V1", StrategyHealthState.DEGRADED)
    }
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="degraded-set",
        created_at=NOW,
        projections=(opportunity,),
        health_by_method=health,
    )
    component = _components(priorities, opportunity.projection_id, "SOURCE_HEALTH")
    assert component.normalized_value == Decimal("0.15")
    assert component.weight == Decimal("0.05")


def test_source_health_no_meaningful_sample_excludes_and_redistributes_weight() -> None:
    opportunity = projection("thin-method", strategy_or_method_id="TEST_THIN_METHOD_V1")
    health = {
        "TEST_THIN_METHOD_V1": _health(
            "TEST_THIN_METHOD_V1", StrategyHealthState.INSUFFICIENT_SAMPLE
        )
    }
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="thin-set",
        created_at=NOW,
        projections=(opportunity,),
        health_by_method=health,
    )
    result = priorities[0]
    health_component = next(item for item in result.components if item.name == "SOURCE_HEALTH")
    assert health_component.weight == Decimal("0")
    assert health_component.weighted_value == Decimal("0")
    assert health_component.missing_behavior == "EXCLUDED_NO_MEANINGFUL_SAMPLE:INSUFFICIENT_SAMPLE"
    assert sum((item.weight for item in result.components), Decimal(0)) == Decimal(1)
    comparable_weight = next(
        item for item in result.components if item.name == "COMPARABLE_VALUE"
    ).weight
    assert comparable_weight == (Decimal("0.25") / Decimal("0.95")).quantize(Decimal("0.00000001"))


def test_source_health_no_retained_assessment_at_all_is_excluded_not_fabricated_perfect() -> None:
    """No-health-data must never equal fabricated perfect health: omitting a method from
    health_by_method entirely must produce the SAME exclusion behavior as an explicit
    NO_OPERATIONAL_EVIDENCE assessment, never a silent normalized_value=1."""

    opportunity = projection("unknown-method", strategy_or_method_id="TEST_UNKNOWN_METHOD_V1")
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="unknown-health-set", created_at=NOW, projections=(opportunity,)
    )
    component = next(item for item in priorities[0].components if item.name == "SOURCE_HEALTH")
    assert component.normalized_value == Decimal("0")
    assert component.weight == Decimal("0")
    assert component.missing_behavior == "EXCLUDED_NO_RETAINED_HEALTH_ASSESSMENT_FOR_METHOD"


# ---------------------------------------------------------------------------
# 7. Missing-data conservatism
# ---------------------------------------------------------------------------


def test_missing_slippage_never_scores_better_than_an_equal_explicit_value() -> None:
    explicit = projection("slip-explicit", fees="1", slippage="1", capital="100")
    missing = projection(
        "slip-missing", fees="1", slippage=None, capital="100", costs_retained=True
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="slip-set", created_at=NOW, projections=(explicit, missing)
    )
    explicit_cost = _components(priorities, explicit.projection_id, "COST")
    missing_cost = _components(priorities, missing.projection_id, "COST")
    assert missing_cost.normalized_value == explicit_cost.normalized_value


def test_missing_slippage_never_scores_better_than_optimistic_explicit_zero() -> None:
    optimistic = projection("slip-zero", fees="1", slippage="0", capital="100")
    missing = projection(
        "slip-missing-2", fees="1", slippage=None, capital="100", costs_retained=True
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="slip-optimistic-set", created_at=NOW, projections=(optimistic, missing)
    )
    optimistic_cost = _components(priorities, optimistic.projection_id, "COST")
    missing_cost = _components(priorities, missing.projection_id, "COST")
    assert missing_cost.normalized_value <= optimistic_cost.normalized_value


def test_missing_maximum_loss_falls_back_to_neutral_not_best_case() -> None:
    """maximum_loss=None is BLOCKED_MISSING_EVIDENCE under the standard eligibility gate (see
    the venue-symmetry variant of this test above) -- exercises prioritize()'s own defense in
    depth directly."""

    missing = projection("loss-missing", maximum_loss=None, capital="100")
    priorities = prioritize(
        (missing,), (_force_eligible(missing),), opportunity_set_id="loss-missing-set"
    )
    component = _components(priorities, missing.projection_id, "DOWNSIDE")
    assert component.normalized_value == Decimal("0.5")
    best_case = projection("loss-zero", maximum_loss="0", capital="100")
    _, _, best_priorities, _ = build_opportunity_set(
        opportunity_set_id="loss-best-set", created_at=NOW, projections=(best_case,)
    )
    best_component = _components(best_priorities, best_case.projection_id, "DOWNSIDE")
    assert best_component.normalized_value == Decimal("1")
    assert component.normalized_value < best_component.normalized_value


def test_missing_uncertainty_falls_back_to_neutral_not_best_case() -> None:
    missing = projection("unc-missing", uncertainty=None)
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="unc-set", created_at=NOW, projections=(missing,)
    )
    component = _components(priorities, missing.projection_id, "UNCERTAINTY")
    assert component.normalized_value == Decimal("0.5")
    best_case = projection("unc-zero", uncertainty="0")
    _, _, best_priorities, _ = build_opportunity_set(
        opportunity_set_id="unc-best-set", created_at=NOW, projections=(best_case,)
    )
    best_component = _components(best_priorities, best_case.projection_id, "UNCERTAINTY")
    assert best_component.normalized_value == Decimal("1")
    assert component.normalized_value < best_component.normalized_value


def test_missing_economic_magnitude_is_neutral_and_research_only() -> None:
    missing = projection("mag-missing", comparable_value=None)
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="mag-set", created_at=NOW, projections=(missing,)
    )
    result = priorities[0]
    assert result.tier is PriorityTier.RESEARCH_ONLY
    component = next(item for item in result.components if item.name == "COMPARABLE_VALUE")
    assert component.normalized_value == Decimal("0.5")
    assert component.missing_behavior == "NO_COMPARABLE_VALUE"


# ---------------------------------------------------------------------------
# 8. Horizon normalization
# ---------------------------------------------------------------------------


def test_horizon_normalization_shorter_horizon_ranks_above_identical_raw_return() -> None:
    fast = projection(
        "fast",
        comparable_value="0.02",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=1,
        lockup=1,
    )
    slow = projection(
        "slow",
        comparable_value="0.02",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=60,
        lockup=60,
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="horizon-set", created_at=NOW, projections=(fast, slow)
    )
    assert priorities[0].projection_id == fast.projection_id
    fast_comparable = _components(priorities, fast.projection_id, "COMPARABLE_VALUE")
    slow_comparable = _components(priorities, slow.projection_id, "COMPARABLE_VALUE")
    assert fast_comparable.normalized_value > slow_comparable.normalized_value
    assert "horizon_normalized_per_day=0.02" in fast_comparable.explanation
    assert "horizon_normalized_per_day=0.00033333" in slow_comparable.explanation


def test_horizon_normalization_zero_day_horizon_floors_safely_at_one_day() -> None:
    same_day = projection(
        "same-day",
        comparable_value="0.02",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=0,
        lockup=0,
    )
    one_day = projection(
        "one-day",
        comparable_value="0.02",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=1,
        lockup=1,
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="zero-horizon-set", created_at=NOW, projections=(same_day, one_day)
    )
    # Both floor to an effective 1-day horizon and therefore tie exactly -- no crash, no blow-up.
    assert _components(priorities, same_day.projection_id, "COMPARABLE_VALUE").normalized_value == (
        _components(priorities, one_day.projection_id, "COMPARABLE_VALUE").normalized_value
    )


def test_horizon_normalization_unknown_horizon_is_excluded_not_fabricated() -> None:
    unknown = projection(
        "unknown-horizon",
        comparable_value="0.05",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=None,
        lockup=None,
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="unknown-horizon-set", created_at=NOW, projections=(unknown,)
    )
    result = priorities[0]
    assert result.tier is PriorityTier.RESEARCH_ONLY
    component = next(item for item in result.components if item.name == "COMPARABLE_VALUE")
    assert component.normalized_value == Decimal("0.5")
    assert component.missing_behavior == "HORIZON_UNKNOWN_NOT_PERCENTILE_COMPARABLE"


def test_horizon_normalization_negative_holding_days_fails_closed_at_construction() -> None:
    import pytest

    from apex.domain.shared_opportunity import SharedOpportunityError

    with pytest.raises(SharedOpportunityError):
        projection("bad-horizon", holding_days=-1)


# ---------------------------------------------------------------------------
# 9. Opportunity-cost floor
# ---------------------------------------------------------------------------


def test_floor_below_zero_is_research_only() -> None:
    below = projection(
        "below-floor",
        comparable_value="-0.01",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=10,
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="below-floor-set", created_at=NOW, projections=(below,)
    )
    assert priorities[0].tier is PriorityTier.RESEARCH_ONLY
    assert any(
        "AT_OR_BELOW_OPPORTUNITY_COST_FLOOR_V1" in reason for reason in priorities[0].tier_reasons
    )


def test_floor_exactly_zero_is_research_only() -> None:
    exact = projection(
        "exact-floor",
        comparable_value="0",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=10,
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="exact-floor-set", created_at=NOW, projections=(exact,)
    )
    assert priorities[0].tier is PriorityTier.RESEARCH_ONLY
    assert Decimal("0") == OPPORTUNITY_COST_FLOOR_V1


def test_floor_just_above_zero_is_actionable() -> None:
    just_above = projection(
        "just-above-floor",
        comparable_value="0.00000001",
        comparable_value_identity=CRYPTO_GENUINE,
        holding_days=1,
    )
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="just-above-floor-set", created_at=NOW, projections=(just_above,)
    )
    assert priorities[0].tier is PriorityTier.ACTIONABLE


# ---------------------------------------------------------------------------
# 10. Determinism
# ---------------------------------------------------------------------------


def test_determinism_identical_inputs_produce_identical_output() -> None:
    projections = (
        projection("det-a", comparable_value="0.03", comparable_value_identity=CRYPTO_GENUINE),
        projection(
            "det-b",
            venue=SharedVenue.KALSHI,
            comparable_value="0.20",
            comparable_value_identity=KALSHI_GENUINE,
        ),
        projection("det-c", comparable_value="9", comparable_value_identity=PROXY_IDENTITY),
    )
    health = {"FIXTURE_METHOD_V1": _health("FIXTURE_METHOD_V1", StrategyHealthState.WATCH)}
    first = build_opportunity_set(
        opportunity_set_id="det-set",
        created_at=NOW,
        projections=projections,
        health_by_method=health,
    )
    second = build_opportunity_set(
        opportunity_set_id="det-set",
        created_at=NOW,
        projections=projections,
        health_by_method=health,
    )
    assert first[0].canonical_hash == second[0].canonical_hash
    assert tuple(item.canonical_hash for item in first[2]) == tuple(
        item.canonical_hash for item in second[2]
    )
    assert tuple(item.tier for item in first[2]) == tuple(item.tier for item in second[2])
    assert tuple(item.rank for item in first[2]) == tuple(item.rank for item in second[2])


def test_all_ten_components_present_and_weights_always_sum_to_one() -> None:
    opportunity = projection("weights-check")
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="weights-set", created_at=NOW, projections=(opportunity,)
    )
    names = {item.name for item in priorities[0].components}
    assert names == {
        "COMPARABLE_VALUE",
        "DOWNSIDE",
        "COST",
        "LIQUIDITY",
        "LOCKUP",
        "FRESHNESS",
        "EVIDENCE",
        "UNCERTAINTY",
        "SOURCE_HEALTH",
        "STATISTICAL_QUALIFICATION",
    }
    assert sum((item.weight for item in priorities[0].components), Decimal(0)) == Decimal(1)


# ---------------------------------------------------------------------------
# Genuine-identity allowlist sanity (guards the allowlist itself against silent drift)
# ---------------------------------------------------------------------------


def test_genuine_identity_allowlist_matches_the_five_real_source_tags() -> None:
    assert {
        "STRICTLY_PRIOR_WALK_FORWARD_EXPECTED_NET_RETURN",
        "STRICTLY_PRIOR_STATEFUL_WALK_FORWARD_EXPECTED_NET_RETURN",
        "GUARANTEED_SETTLEMENT_NET_GAP_OVER_MODELED_COST",
        "CONSERVATIVE_MODEL_FREE_NET_LOWER_BOUND_OVER_COST",
        "CONSERVATIVE_NET_LOWER_BOUND_OVER_MODELED_COST",
    } == GENUINE_ECONOMIC_MAGNITUDE_IDENTITIES
