from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.domain.cross_venue_allocation import (
    CROSS_VENUE_ALLOCATION_POLICY_V1,
    CrossVenueAllocationPolicy,
    VenuePaperReadiness,
    allocate_cross_venue,
)
from apex.domain.shared_opportunity import (
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
)
from apex.domain.total_market_context import (
    ContextComponent,
    ContextComponentType,
    OverallContextState,
    UnavailableContextComponent,
    build_context_enriched_set,
    build_context_snapshot,
)
from apex.domain.values import content_digest

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _projection(
    identity: str,
    *,
    venue: SharedVenue,
    score: str = "0.05",
    risk: str = "10",
    required: str = "50",
    source_maximum: str = "400",
    lockup: int = 5,
    rejection: tuple[str, ...] = (),
    strategy: str | None = None,
    instrument: str | None = None,
) -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=f"cross-{identity}",
        venue=venue,
        source_record_type="FixtureOpportunity",
        source_record_id=identity,
        source_canonical_hash=content_digest((identity, "source")),
        strategy_or_method_id=strategy or f"strategy-{identity}",
        instrument_id=instrument or f"instrument-{identity}",
        product_or_market_id=f"market-{identity}",
        direction="BUY_YES" if venue is SharedVenue.KALSHI else "BUY",
        generated_at=NOW,
        valid_until=NOW + timedelta(days=10),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=30,
        freshness_limit_seconds=3600,
        comparable_value=score,
        comparable_value_identity="FIXTURE_SCORE",
        uncertainty="0.1",
        maximum_loss=risk,
        modeled_fees="1",
        modeled_slippage="1",
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital=required,
        maximum_sensible_research_allocation=source_maximum,
        source_maximum_allocation_fraction="0.40",
        expected_holding_days=lockup,
        estimated_lockup_days=lockup,
        supporting_evidence_ids=(f"evidence-{identity}",),
        source_warnings=(),
        source_rejection_reasons=rejection,
        source_authority="NONE",
        projection_limitations=("FIXTURE_ONLY",),
        source_details=(("fixture", identity),),
    )


def _context(state: OverallContextState):
    component = ContextComponent.create(
        component_id="cross-context-equity",
        component_type=ContextComponentType.US_EQUITY_MARKET,
        source_observation_id="cross-bar",
        provider="FIXTURE_RETAINED",
        subject="SPY",
        observation_at=NOW - timedelta(minutes=2),
        available_at=NOW - timedelta(minutes=1),
        ingested_at=NOW - timedelta(seconds=30),
        current_value="101",
        prior_comparable_value="100",
        freshness_seconds=60,
        freshness_limit_seconds=3600,
        quality_status="VALID",
        evidence_class="FIXTURE",
        limitations=("FIXTURE_ONLY",),
        interpretation_category="SPY_FIXTURE_CONTEXT",
        supporting_evidence_ids=("cross-context-evidence",),
    )
    snapshot = build_context_snapshot(
        snapshot_id=f"cross-context-{state.value.lower()}",
        created_at=NOW,
        source_batch_id="cross-context-batch",
        components=(component,),
        unavailable_components=tuple(
            UnavailableContextComponent(value, "FIXTURE_UNAVAILABLE")
            for value in ContextComponentType
            if value is not ContextComponentType.US_EQUITY_MARKET
        ),
    )
    return replace(
        snapshot,
        overall_state=state,
        canonical_hash=content_digest((snapshot.canonical_hash, state.value)),
    )


def _allocate(
    projections: tuple[SharedOpportunityProjection, ...],
    *,
    state: OverallContextState = OverallContextState.RISK_SUPPORTIVE,
    unavailable: SharedVenue | None = None,
):
    opportunity_set, eligibility, priorities, _ = build_opportunity_set(
        opportunity_set_id=f"cross-set-{state.value.lower()}",
        created_at=NOW,
        projections=projections,
    )
    context = _context(state)
    enriched = build_context_enriched_set(
        enriched_set_id=f"cross-enriched-{state.value.lower()}",
        original_opportunity_set_id=opportunity_set.opportunity_set_id,
        original_opportunity_set_hash=opportunity_set.canonical_hash,
        original_allocation_id=None,
        original_allocation_hash=None,
        context_snapshot_id=context.snapshot_id,
        attachments=(),
        eligibility_hash=content_digest(tuple(item.canonical_hash for item in eligibility)),
        priority_hash=content_digest(tuple(item.canonical_hash for item in priorities)),
        created_at=NOW,
    )
    readiness = tuple(
        VenuePaperReadiness(
            venue=venue,
            available=venue is not unavailable,
            capability_id=None if venue is unavailable else f"{venue.value.lower()}-capability",
            account_id=None if venue is unavailable else f"{venue.value.lower()}-account",
            reason_codes=("FIXTURE_UNAVAILABLE",) if venue is unavailable else (),
        )
        for venue in SharedVenue
    )
    return allocate_cross_venue(
        decision_id=f"cross-decision-{state.value.lower()}",
        opportunity_set=opportunity_set,
        enriched_set=enriched,
        context=context,
        projections=projections,
        eligibility=eligibility,
        priorities=priorities,
        paper_readiness=readiness,
        decided_at=NOW,
    )


def test_policy_identity_and_zero_allocation_fail_closed() -> None:
    policy = CrossVenueAllocationPolicy.v1()
    rejected = _projection("rejected", venue=SharedVenue.CRYPTO, rejection=("SOURCE_REJECTED",))
    decision, lines, explanations, proposals = _allocate((rejected,))
    assert policy.policy_id == decision.policy_id == CROSS_VENUE_ALLOCATION_POLICY_V1
    assert decision.retained_cash == Decimal("1000.00000000")
    assert lines == proposals == ()
    assert explanations[0].allocated is False
    assert not decision.paper_authorization and not decision.order
    assert decision.live_authority == "NONE"


def test_rank_order_caps_downside_concentration_and_trial_proposals() -> None:
    projections = (
        _projection("crypto-a", venue=SharedVenue.CRYPTO, score="0.09"),
        _projection("crypto-b", venue=SharedVenue.CRYPTO, score="0.08"),
        _projection("crypto-c", venue=SharedVenue.CRYPTO, score="0.07"),
        _projection("kalshi-a", venue=SharedVenue.KALSHI, score="0.06"),
    )
    decision, lines, explanations, proposals = _allocate(projections)
    assert tuple(item.rank for item in lines) == (1, 2, 4)
    assert tuple(item.amount for item in lines) == (
        Decimal("200.00000000"),
        Decimal("200.00000000"),
        Decimal("100.00000000"),
    )
    # venue_totals always reports every known SharedVenue (Securities
    # Research -> Shared Opportunity Recurring Bridge v1 widened the enum to
    # three members) -- CRYPTO/KALSHI totals themselves are unchanged; only
    # a new, correctly-zero SECURITIES entry appears since this opportunity
    # set has no securities projections.
    assert dict(decision.venue_totals) == {
        "CRYPTO": Decimal("400.00000000"),
        "KALSHI": Decimal("100.00000000"),
        "SECURITIES": Decimal("0E-8"),
    }
    assert decision.modeled_downside == Decimal("100.00000000")
    assert decision.retained_cash == Decimal("500.00000000")
    assert any("VENUE_CAP" in item.binding_constraints for item in explanations)
    assert any("AGGREGATE_DOWNSIDE_CAP" in item.binding_constraints for item in explanations)
    assert dict(decision.concentration)["STRATEGY:strategy-crypto-a"] == Decimal("200.00000000")
    assert len(proposals) == len(lines)
    assert all(item.operator_authorization_required for item in proposals)
    assert all(
        not item.authorization and not item.reservation and not item.order for item in proposals
    )


def test_context_cash_limits_lockup_source_and_unavailable_paper_boundary() -> None:
    long_a = _projection("long-a", venue=SharedVenue.CRYPTO, lockup=60, risk="1")
    long_b = _projection("long-b", venue=SharedVenue.KALSHI, lockup=60, risk="1")
    supportive = _allocate((long_a, long_b), state=OverallContextState.RISK_SUPPORTIVE)[0]
    stressed = _allocate((long_a, long_b), state=OverallContextState.MARKET_STRESS)[0]
    insufficient = _allocate((long_a, long_b), state=OverallContextState.INSUFFICIENT_CONTEXT)[0]
    assert supportive.long_lockup_capital == Decimal("250.00000000")
    assert supportive.retained_cash == Decimal("750.00000000")
    assert stressed.retained_cash == Decimal("750.00000000")
    assert insufficient.retained_cash == Decimal("1000.00000000")

    decision, lines, explanations, proposals = _allocate(
        (long_a, long_b), unavailable=SharedVenue.KALSHI
    )
    assert all(item.venue is SharedVenue.CRYPTO for item in lines)
    assert all(item.venue is SharedVenue.CRYPTO for item in proposals)
    assert "cross-long-b" in decision.excluded_projection_ids
    blocked = next(item for item in explanations if item.projection_id == "cross-long-b")
    assert "VENUE_PAPER_BOUNDARY_UNAVAILABLE" in blocked.excluded_reasons

    kalshi_only = _allocate((long_a, long_b), unavailable=SharedVenue.CRYPTO)
    assert all(item.venue is SharedVenue.KALSHI for item in kalshi_only[1])
    assert all(item.venue is SharedVenue.KALSHI for item in kalshi_only[3])


def test_market_stress_increases_cash_and_expired_or_stale_evidence_is_excluded() -> None:
    diversified = (
        _projection("crypto-a", venue=SharedVenue.CRYPTO, risk="1"),
        _projection("crypto-b", venue=SharedVenue.CRYPTO, risk="1"),
        _projection("kalshi-a", venue=SharedVenue.KALSHI, risk="1"),
        _projection("kalshi-b", venue=SharedVenue.KALSHI, risk="1"),
    )
    supportive = _allocate(diversified, state=OverallContextState.RISK_SUPPORTIVE)[0]
    stressed = _allocate(diversified, state=OverallContextState.MARKET_STRESS)[0]
    assert supportive.retained_cash == Decimal("250.00000000")
    assert stressed.retained_cash == Decimal("750.00000000")

    expired = replace(
        _projection("expired", venue=SharedVenue.CRYPTO),
        valid_until=NOW,
    )
    stale = replace(
        _projection("stale", venue=SharedVenue.KALSHI),
        generated_at=NOW - timedelta(hours=2),
    )
    decision, lines, explanations, proposals = _allocate((expired, stale))
    assert lines == proposals == ()
    assert set(decision.excluded_projection_ids) == {"cross-expired", "cross-stale"}
    reasons = {item.projection_id: item.excluded_reasons for item in explanations}
    assert "OPPORTUNITY_EXPIRED" in reasons["cross-expired"]
    assert "EVIDENCE_STALE" in reasons["cross-stale"]


def test_exact_replay_is_byte_identity_deterministic() -> None:
    projections = (
        _projection("crypto", venue=SharedVenue.CRYPTO),
        _projection("kalshi", venue=SharedVenue.KALSHI),
    )
    first = _allocate(projections)
    replay = _allocate(projections)
    assert first[0].canonical_hash == replay[0].canonical_hash
    assert tuple(item.canonical_hash for item in first[1]) == tuple(
        item.canonical_hash for item in replay[1]
    )
    assert tuple(item.canonical_hash for item in first[3]) == tuple(
        item.canonical_hash for item in replay[3]
    )


def test_exact_id_cli_and_read_only_api_routes_are_registered() -> None:
    from apex.cli import parser
    from apex.operator_api import create_app

    parsed = parser().parse_args(
        [
            "cross-venue-allocation-materialize",
            "--decision-id",
            "exact-decision",
            "--enriched-set-id",
            "exact-enriched-set",
            "--decided-at",
            NOW.isoformat(),
            "--idempotency-key",
            "exact-key",
        ]
    )
    assert parsed.decision_id == "exact-decision"
    assert parser().parse_args(["cross-venue-policy-inspect"]).command == (
        "cross-venue-policy-inspect"
    )
    routes = {
        (route.path, method)
        for route in create_app().routes
        for method in getattr(route, "methods", ())
    }
    assert ("/api/v1/cross-venue-allocation/workspace", "GET") in routes
    assert (
        "/api/v1/cross-venue-allocation/decisions/{decision_id}",
        "GET",
    ) in routes
    assert (
        "/api/v1/cross-venue-allocation/proposals/{proposal_id}",
        "GET",
    ) in routes
