from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.adapters.total_market_context_sources import _retained_decimal
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.total_market_context import (
    TOTAL_MARKET_CONTEXT_POLICY_V1,
    ApplicabilityState,
    ContextComponent,
    ContextComponentState,
    ContextComponentType,
    OverallContextState,
    TotalMarketContextError,
    UnavailableContextComponent,
    attach_context,
    build_context_enriched_set,
    build_context_snapshot,
)
from apex.domain.values import content_digest

NOW = datetime(2026, 7, 30, tzinfo=UTC)


def test_retained_decimal_removes_only_postgres_storage_scale() -> None:
    assert _retained_decimal(Decimal("738.850000000000000000")) == Decimal("738.85")
    assert _retained_decimal(Decimal("0.123456780000000000")) == Decimal("0.12345678")


def component(
    component_type: ContextComponentType,
    *,
    current: str,
    prior: str,
    freshness: int = 60,
    quality: str = "VALID",
) -> ContextComponent:
    subject = {
        ContextComponentType.US_EQUITY_MARKET: "SPY",
        ContextComponentType.TREASURY_RATE: "TLT",
        ContextComponentType.MARKET_VOLATILITY: "SPY",
        ContextComponentType.COMMODITY_OR_INFLATION: "USO",
    }[component_type]
    return ContextComponent.create(
        component_id=f"context-{component_type.value.lower()}",
        component_type=component_type,
        source_observation_id=f"bar-{component_type.value.lower()}",
        provider="FIXTURE_RETAINED",
        subject=subject,
        observation_at=NOW - timedelta(seconds=freshness + 60),
        available_at=NOW - timedelta(seconds=freshness),
        ingested_at=NOW - timedelta(seconds=30),
        current_value=current,
        prior_comparable_value=prior,
        freshness_seconds=freshness,
        freshness_limit_seconds=3600,
        quality_status=quality,
        evidence_class="FIXTURE",
        limitations=("FIXTURE_ONLY",),
        interpretation_category=f"{subject}_FIXTURE_CONTEXT",
        supporting_evidence_ids=(f"evidence-{component_type.value.lower()}",),
    )


def snapshot_components() -> tuple[ContextComponent, ...]:
    return (
        component(ContextComponentType.US_EQUITY_MARKET, current="105", prior="100"),
        component(ContextComponentType.TREASURY_RATE, current="96", prior="100"),
        component(ContextComponentType.MARKET_VOLATILITY, current="0.20", prior="0.18"),
        component(ContextComponentType.COMMODITY_OR_INFLATION, current="102", prior="100"),
    )


def unavailable() -> tuple[UnavailableContextComponent, ...]:
    return (
        UnavailableContextComponent(
            ContextComponentType.YIELD_CURVE,
            "NO_RETAINED_MULTI_MATURITY_YIELD_OBSERVATIONS",
        ),
        UnavailableContextComponent(
            ContextComponentType.US_DOLLAR,
            "NO_RETAINED_US_DOLLAR_INDEX_OBSERVATION",
        ),
        UnavailableContextComponent(
            ContextComponentType.MACRO_EVENT_PROXIMITY,
            "NO_RETAINED_MACRO_RELEASE_CALENDAR",
        ),
    )


def shared_projection(
    identity: str,
    *,
    venue: SharedVenue,
    title: str = "",
) -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=f"shared-{identity}",
        venue=venue,
        source_record_type="FixtureOpportunity",
        source_record_id=identity,
        source_canonical_hash=content_digest((identity, "source")),
        strategy_or_method_id="FIXTURE_METHOD_V1",
        instrument_id=f"{identity}-instrument",
        product_or_market_id=f"{identity}-market",
        direction="RESEARCH_ONLY",
        generated_at=NOW,
        valid_until=NOW + timedelta(days=10),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=60,
        freshness_limit_seconds=3600,
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital="100",
        maximum_sensible_research_allocation="100",
        source_maximum_allocation_fraction=None,
        expected_holding_days=1,
        estimated_lockup_days=1,
        supporting_evidence_ids=(f"{identity}-evidence",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(("market_title", title),),
        maximum_loss="100",
        modeled_fees="1",
        modeled_slippage="1",
    )


def test_component_normalization_and_deterministic_classification() -> None:
    equity = component(ContextComponentType.US_EQUITY_MARKET, current="105", prior="100")
    treasury = component(ContextComponentType.TREASURY_RATE, current="96", prior="100")
    volatility = component(
        ContextComponentType.MARKET_VOLATILITY,
        current="0.42",
        prior="0.20",
    )
    commodity = component(
        ContextComponentType.COMMODITY_OR_INFLATION,
        current="107",
        prior="100",
    )
    assert equity.normalized_change == Decimal("0.05000000")
    assert equity.state is ContextComponentState.SUPPORTIVE
    assert treasury.state is ContextComponentState.CAUTIONARY
    assert volatility.state is ContextComponentState.STRESSED
    assert commodity.state is ContextComponentState.CAUTIONARY
    assert equity.canonical_hash == replace(equity).canonical_hash


def test_freshness_and_quality_block_before_directional_interpretation() -> None:
    stale = component(
        ContextComponentType.US_EQUITY_MARKET,
        current="110",
        prior="100",
        freshness=3601,
    )
    blocked = component(
        ContextComponentType.TREASURY_RATE,
        current="110",
        prior="100",
        quality="BLOCKED",
    )
    assert stale.state is ContextComponentState.STALE
    assert stale.contribution == 0
    assert blocked.state is ContextComponentState.QUALITY_BLOCKED
    assert blocked.contribution == 0


def test_snapshot_retains_unavailable_components_and_mixed_context() -> None:
    snapshot = build_context_snapshot(
        snapshot_id="context-snapshot",
        created_at=NOW,
        source_batch_id="exact-batch",
        components=snapshot_components(),
        unavailable_components=unavailable(),
    )
    assert snapshot.policy_id == TOTAL_MARKET_CONTEXT_POLICY_V1
    assert snapshot.overall_state is OverallContextState.MIXED
    assert snapshot.contribution_total == 0
    assert tuple(item.component_type for item in snapshot.unavailable_components) == (
        ContextComponentType.MACRO_EVENT_PROXIMITY,
        ContextComponentType.US_DOLLAR,
        ContextComponentType.YIELD_CURVE,
    )
    assert len(snapshot.manifest_hash) == len(snapshot.canonical_hash) == 64


def test_stressed_component_and_insufficient_context_are_explicit() -> None:
    stressed = component(
        ContextComponentType.MARKET_VOLATILITY,
        current="0.50",
        prior="0.20",
    )
    snapshot = build_context_snapshot(
        snapshot_id="stress-snapshot",
        created_at=NOW,
        source_batch_id="exact-batch",
        components=(
            component(ContextComponentType.US_EQUITY_MARKET, current="100", prior="100"),
            stressed,
        ),
        unavailable_components=tuple(
            UnavailableContextComponent(value, "NOT_RETAINED")
            for value in ContextComponentType
            if value
            not in {
                ContextComponentType.US_EQUITY_MARKET,
                ContextComponentType.MARKET_VOLATILITY,
            }
        ),
    )
    assert snapshot.overall_state is OverallContextState.MARKET_STRESS
    stale_equity = component(
        ContextComponentType.US_EQUITY_MARKET,
        current="100",
        prior="100",
        freshness=3601,
    )
    insufficient = build_context_snapshot(
        snapshot_id="insufficient-snapshot",
        created_at=NOW,
        source_batch_id="exact-batch",
        components=(stale_equity,),
        unavailable_components=tuple(
            UnavailableContextComponent(value, "NOT_RETAINED")
            for value in ContextComponentType
            if value is not ContextComponentType.US_EQUITY_MARKET
        ),
    )
    assert insufficient.overall_state is OverallContextState.INSUFFICIENT_CONTEXT


def test_snapshot_rejects_incompatible_component_capture_periods() -> None:
    components = snapshot_components()
    incompatible = replace(
        components[-1],
        observation_at=components[-1].observation_at - timedelta(days=2),
    )
    with pytest.raises(
        TotalMarketContextError,
        match="TOTAL_MARKET_COMPONENT_CAPTURE_PERIOD_INCOMPATIBLE",
    ):
        build_context_snapshot(
            snapshot_id="incompatible-snapshot",
            created_at=NOW,
            source_batch_id="exact-batch",
            components=(*components[:-1], incompatible),
            unavailable_components=unavailable(),
        )


def test_crypto_attachment_selects_relevant_and_unavailable_context() -> None:
    components = snapshot_components()
    snapshot = build_context_snapshot(
        snapshot_id="context-snapshot",
        created_at=NOW,
        source_batch_id="exact-batch",
        components=components,
        unavailable_components=unavailable(),
    )
    attachment = attach_context(
        enriched_set_id="context-enriched-crypto",
        projection=shared_projection("btc", venue=SharedVenue.CRYPTO),
        snapshot=snapshot,
        components=components,
        attached_at=NOW,
    )
    states = {item.component_type: item.state for item in attachment.applicability}
    assert states[ContextComponentType.US_EQUITY_MARKET] is ApplicabilityState.RELEVANT
    assert states[ContextComponentType.MARKET_VOLATILITY] is ApplicabilityState.RELEVANT
    assert states[ContextComponentType.US_DOLLAR] is ApplicabilityState.UNAVAILABLE
    assert states[ContextComponentType.COMMODITY_OR_INFLATION] is ApplicabilityState.IRRELEVANT
    assert "does not cause, predict, reject, rank, or allocate" in attachment.explanation


def test_kalshi_applicability_is_contract_specific_and_excludes_irrelevant_components() -> None:
    components = snapshot_components()
    snapshot = build_context_snapshot(
        snapshot_id="context-snapshot",
        created_at=NOW,
        source_batch_id="exact-batch",
        components=components,
        unavailable_components=unavailable(),
    )
    inflation = attach_context(
        enriched_set_id="context-enriched-inflation",
        projection=shared_projection(
            "cpi",
            venue=SharedVenue.KALSHI,
            title="Will CPI inflation exceed 3 percent?",
        ),
        snapshot=snapshot,
        components=components,
        attached_at=NOW,
    )
    unrelated = attach_context(
        enriched_set_id="context-enriched-unrelated",
        projection=shared_projection(
            "unrelated",
            venue=SharedVenue.KALSHI,
            title="Will an unrelated event occur?",
        ),
        snapshot=snapshot,
        components=components,
        attached_at=NOW,
    )
    inflation_states = {item.component_type: item.state for item in inflation.applicability}
    assert (
        inflation_states[ContextComponentType.COMMODITY_OR_INFLATION] is ApplicabilityState.RELEVANT
    )
    assert inflation_states[ContextComponentType.US_EQUITY_MARKET] is ApplicabilityState.IRRELEVANT
    assert unrelated.relevant_component_ids == ()
    assert all(item.state is ApplicabilityState.IRRELEVANT for item in unrelated.applicability)


def test_context_enriched_set_proves_shared_decisions_unchanged_and_replays() -> None:
    components = snapshot_components()
    snapshot = build_context_snapshot(
        snapshot_id="context-snapshot",
        created_at=NOW,
        source_batch_id="exact-batch",
        components=components,
        unavailable_components=unavailable(),
    )
    attachment = attach_context(
        enriched_set_id="enriched-set",
        projection=shared_projection("btc", venue=SharedVenue.CRYPTO),
        snapshot=snapshot,
        components=components,
        attached_at=NOW,
    )
    inputs = {
        "enriched_set_id": "enriched-set",
        "original_opportunity_set_id": "original-set",
        "original_opportunity_set_hash": content_digest(("original",)),
        "original_allocation_id": "original-allocation",
        "original_allocation_hash": content_digest(("allocation",)),
        "context_snapshot_id": snapshot.snapshot_id,
        "attachments": (attachment,),
        "eligibility_hash": content_digest(("eligibility",)),
        "priority_hash": content_digest(("priority",)),
        "created_at": NOW,
    }
    first = build_context_enriched_set(**inputs)
    replay = build_context_enriched_set(**inputs)
    changed_attachment = replace(
        attachment,
        canonical_hash=content_digest(("changed-attachment",)),
    )
    changed = build_context_enriched_set(**{**inputs, "attachments": (changed_attachment,)})
    assert first.canonical_hash == replay.canonical_hash
    assert first.attachment_manifest_hash != changed.attachment_manifest_hash
    assert first.canonical_hash != changed.canonical_hash
    assert first.eligibility_hash_before == first.eligibility_hash_after
    assert first.priority_hash_before == first.priority_hash_after
    assert first.allocation_hash_before == first.allocation_hash_after
    assert first.eligibility_unchanged
    assert first.priority_unchanged
    assert first.allocation_unchanged
    assert first.authority == first.capital_authority == first.order_authority == "NONE"


def test_exact_id_cli_and_read_only_api_routes_are_registered() -> None:
    from apex.cli import parser
    from apex.operator_api import create_app

    mutation = parser().parse_args(
        [
            "context-snapshot-materialize",
            "--snapshot-id",
            "exact-context",
            "--source-batch-id",
            "exact-batch",
            "--as-of",
            NOW.isoformat(),
            "--idempotency-key",
            "exact-key",
        ]
    )
    assert mutation.snapshot_id == "exact-context"
    assert mutation.source_batch_id == "exact-batch"
    attach = parser().parse_args(
        [
            "opportunity-context-attach",
            "--enriched-set-id",
            "exact-enriched",
            "--opportunity-set-id",
            "exact-shared",
            "--context-snapshot-id",
            "exact-context",
            "--attached-at",
            NOW.isoformat(),
            "--idempotency-key",
            "attach-key",
        ]
    )
    assert attach.enriched_set_id == "exact-enriched"
    routes = {
        (route.path, method)
        for route in create_app().routes
        for method in getattr(route, "methods", ())
    }
    assert ("/api/v1/total-market-context/workspace", "GET") in routes
    assert (
        "/api/v1/total-market-context/components/{component_id}",
        "GET",
    ) in routes
    assert (
        "/api/v1/total-market-context/snapshots/{snapshot_id}",
        "GET",
    ) in routes
    assert (
        "/api/v1/total-market-context/enriched-sets/{enriched_set_id}",
        "GET",
    ) in routes
