"""Deterministic no-network acceptance for cross-venue allocation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from apex.domain.cross_venue_allocation import (
    AllocationConstraintExplanation,
    CrossVenueAllocationDecision,
    CrossVenueAllocationLine,
    PaperTrialProposal,
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

ACCEPTANCE_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _projection(identity: str, venue: SharedVenue, score: str) -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=f"cross-venue-fixture-{identity}",
        venue=venue,
        source_record_type="FixtureOpportunity",
        source_record_id=f"fixture-{identity}",
        source_canonical_hash=content_digest((identity, "fixture-source")),
        strategy_or_method_id=f"fixture-{venue.value.lower()}-method",
        instrument_id=f"fixture-{identity}-instrument",
        product_or_market_id=f"fixture-{identity}-market",
        direction="BUY_YES" if venue is SharedVenue.KALSHI else "BUY",
        generated_at=ACCEPTANCE_AT,
        valid_until=ACCEPTANCE_AT + timedelta(days=2),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=30,
        freshness_limit_seconds=3600,
        comparable_value=score,
        comparable_value_identity="FIXTURE_COMPARABLE_VALUE",
        uncertainty="0.1",
        maximum_loss="5",
        modeled_fees="0.25",
        modeled_slippage="0.25",
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=("FIXTURE_ONLY",),
        required_hypothetical_capital="25",
        maximum_sensible_research_allocation="200",
        source_maximum_allocation_fraction="0.20",
        expected_holding_days=5,
        estimated_lockup_days=5,
        supporting_evidence_ids=(f"fixture-{identity}-evidence",),
        source_warnings=("ISOLATED_FIXTURE",),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=("NO_FILLABILITY_OR_PROFITABILITY_CLAIM",),
        source_details=(("fixture", identity),),
    )


def run_cross_venue_fixture_acceptance() -> dict[str, object]:
    projections = (
        _projection("crypto", SharedVenue.CRYPTO, "0.08"),
        _projection("kalshi", SharedVenue.KALSHI, "0.06"),
    )
    opportunity_set, eligibility, priorities, _ = build_opportunity_set(
        opportunity_set_id="cross-venue-fixture-shared-set-v1",
        created_at=ACCEPTANCE_AT,
        projections=projections,
    )
    component = ContextComponent.create(
        component_id="cross-venue-fixture-context-component",
        component_type=ContextComponentType.US_EQUITY_MARKET,
        source_observation_id="cross-venue-fixture-spy-bar",
        provider="FIXTURE_RETAINED",
        subject="SPY",
        observation_at=ACCEPTANCE_AT - timedelta(minutes=2),
        available_at=ACCEPTANCE_AT - timedelta(minutes=1),
        ingested_at=ACCEPTANCE_AT - timedelta(seconds=30),
        current_value="101",
        prior_comparable_value="100",
        freshness_seconds=60,
        freshness_limit_seconds=3600,
        quality_status="VALID",
        evidence_class="FIXTURE",
        limitations=("FIXTURE_ONLY",),
        interpretation_category="SPY_FIXTURE_CONTEXT",
        supporting_evidence_ids=("cross-venue-fixture-context-evidence",),
    )
    context = build_context_snapshot(
        snapshot_id="cross-venue-fixture-context-v1",
        created_at=ACCEPTANCE_AT,
        source_batch_id="cross-venue-fixture-context-batch",
        components=(component,),
        unavailable_components=tuple(
            UnavailableContextComponent(value, "FIXTURE_UNAVAILABLE")
            for value in ContextComponentType
            if value is not ContextComponentType.US_EQUITY_MARKET
        ),
    )
    context = replace(
        context,
        overall_state=OverallContextState.RISK_SUPPORTIVE,
        canonical_hash=content_digest((context.canonical_hash, "RISK_SUPPORTIVE")),
    )
    enriched = build_context_enriched_set(
        enriched_set_id="cross-venue-fixture-enriched-v1",
        original_opportunity_set_id=opportunity_set.opportunity_set_id,
        original_opportunity_set_hash=opportunity_set.canonical_hash,
        original_allocation_id=None,
        original_allocation_hash=None,
        context_snapshot_id=context.snapshot_id,
        attachments=(),
        eligibility_hash=content_digest(tuple(item.canonical_hash for item in eligibility)),
        priority_hash=content_digest(tuple(item.canonical_hash for item in priorities)),
        created_at=ACCEPTANCE_AT,
    )
    readiness = tuple(
        VenuePaperReadiness(
            venue=venue,
            available=True,
            capability_id=f"cross-venue-fixture-{venue.value.lower()}-capability",
            account_id=f"cross-venue-fixture-{venue.value.lower()}-account",
            reason_codes=(),
        )
        for venue in SharedVenue
    )

    def allocate_fixture() -> tuple[
        CrossVenueAllocationDecision,
        tuple[CrossVenueAllocationLine, ...],
        tuple[AllocationConstraintExplanation, ...],
        tuple[PaperTrialProposal, ...],
    ]:
        return allocate_cross_venue(
            decision_id="cross-venue-fixture-decision-v1",
            opportunity_set=opportunity_set,
            enriched_set=enriched,
            context=context,
            projections=projections,
            eligibility=eligibility,
            priorities=priorities,
            paper_readiness=readiness,
            decided_at=ACCEPTANCE_AT,
        )

    decision, lines, explanations, proposals = allocate_fixture()
    replay = allocate_fixture()
    return {
        "decision": decision,
        "lines": lines,
        "explanations": explanations,
        "paper_trial_proposals": proposals,
        "projections": projections,
        "eligibility": eligibility,
        "crypto_total": dict(decision.venue_totals)["CRYPTO"],
        "kalshi_total": dict(decision.venue_totals)["KALSHI"],
        "retained_cash": decision.retained_cash,
        "exact_replay": decision.canonical_hash == replay[0].canonical_hash,
        "paper_authorizations_created": 0,
        "orders_created": 0,
        "provider_requests": 0,
        "live_authority": "NONE",
    }
