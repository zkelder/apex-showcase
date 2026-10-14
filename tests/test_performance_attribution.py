from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.adapters.performance_attribution_acceptance import (
    _source,
    run_performance_attribution_acceptance,
)
from apex.domain.performance_attribution import (
    AttributionCategory,
    PaperEvidenceClass,
    PaperVenue,
    PerformanceAttributionError,
    StrategyHealthPolicy,
    StrategyHealthState,
    assess_strategy_health,
    materialize_attribution,
    summarize_performance,
)

NOW = datetime(2026, 2, 10, tzinfo=UTC)


def test_fixture_acceptance_proves_venue_semantics_costs_partial_fill_and_health() -> None:
    result = run_performance_attribution_acceptance()
    assert result["attribution_count"] == 7
    assert result["profitable_crypto_net_return"] == Decimal("0.04000000")
    assert result["losing_crypto_net_return"] == Decimal("-0.02000000")
    assert result["partial_fill_rate"] == Decimal("0.40000000")
    assert result["kalshi_settlement_id"] == "kalshi-settlement-6-settlement"
    assert result["reconciliation_health"] == "RECONCILIATION_BLOCKED"
    assert result["health_transitions"] == (
        "INSUFFICIENT_SAMPLE",
        "HEALTHY",
        "DEGRADED",
    )
    assert {"FEES", "SLIPPAGE", "PARTIAL_OR_NO_FILL", "SETTLEMENT"}.issubset(
        result["component_categories"]
    )
    assert result["persisted_to_genuine_workspace"] is False
    assert result["provider_requests"] == result["broker_requests"] == 0
    assert result["financial_mutations"] == 0
    assert result["live_authority"] == "NONE"


def test_missing_lifecycle_stages_remain_explicit_and_no_performance_is_invented() -> None:
    source = replace(
        _source("missing-stage-8", realized=None, filled="0", settled=False),
        source_opportunity_id=None,
        shared_projection_id=None,
        allocation_decision_id=None,
        paper_trial_proposal_id=None,
        authorization_id=None,
        order_id=None,
        reservation_id=None,
        reservation_original_amount=None,
        reservation_current_amount=None,
        reservation_consumed_amount=None,
        context_at_consideration_id=None,
        considered_at=None,
        first_fill_at=None,
        filled_quantity=None,
        filled_notional=None,
        realized_fees=None,
        realized_slippage=None,
    )
    attribution, components = materialize_attribution(
        attribution_id="missing-stage-attribution", source=source, materialized_at=NOW
    )
    assert attribution.realized_gross_return is None
    assert attribution.realized_net_return is None
    assert attribution.fill_rate is None
    assert {
        "SOURCE_OPPORTUNITY",
        "SHARED_OPPORTUNITY",
        "ALLOCATION_DECISION",
        "PAPER_TRIAL_PROPOSAL",
        "AUTHORIZATION",
        "ORDER",
        "FILL",
        "SETTLEMENT",
        "CONTEXT_AT_CONSIDERATION",
    }.issubset(attribution.missing_stages)
    assert {item.category for item in components} >= {
        AttributionCategory.DATA_QUALITY,
        AttributionCategory.INSUFFICIENT_EVIDENCE,
    }


def test_external_alpaca_evidence_remains_external_and_does_not_invent_a_fill() -> None:
    source = replace(
        _source("external-alpaca-14"),
        venue=PaperVenue.ALPACA_EXTERNAL_PAPER,
        evidence_class=PaperEvidenceClass.GENUINE_PAPER,
        instrument_id="SPY",
        side="BUY",
        fill_ids=(),
        reservation_id=None,
        reservation_original_amount=None,
        reservation_current_amount=None,
        reservation_consumed_amount=None,
        position_id=None,
        settlement_id=None,
        resolution_fact_id=None,
        filled_quantity=Decimal(0),
        filled_notional=Decimal(0),
        realized_gross_return=None,
        realized_net_return=None,
        realized_fees=None,
        realized_slippage=None,
        realized_loss=None,
        open_exposure=Decimal(0),
        unrealized_paper_pnl=None,
        first_fill_at=None,
        completed_at=None,
        position_state=None,
        opportunity_expiration_behavior="SUBMITTED_NO_VALIDATED_FILL",
        limitations=("EXTERNAL_EVIDENCE_IS_NOT_APEX_FINANCIAL_TRUTH",),
    )
    source = replace(source, source_hash="a" * 64)
    attribution, _ = materialize_attribution(
        attribution_id="external-alpaca-attribution",
        source=source,
        materialized_at=NOW,
    )
    assert attribution.paper_state_boundary == "EXTERNAL_PAPER_EVIDENCE"
    assert attribution.fill_ids == ()
    assert attribution.realized_net_return is None
    assert {"FILL", "POSITION", "SETTLEMENT"}.issubset(attribution.missing_stages)
    health = assess_strategy_health(
        health_id="external-alpaca-health", attributions=(attribution,), as_of=NOW
    )
    assert health.fee_variance is None
    assert health.slippage_variance is None


def test_expected_realized_decimal_calculations_and_explanations_are_deterministic() -> None:
    source = _source(
        "decimal-9",
        expected="0.05",
        realized="0.01",
        estimated_fees="0.10",
        realized_fees="0.18",
        estimated_slippage="0.10",
        realized_slippage="0.22",
    )
    first = materialize_attribution(
        attribution_id="decimal-attribution", source=source, materialized_at=NOW
    )
    second = materialize_attribution(
        attribution_id="decimal-attribution", source=source, materialized_at=NOW
    )
    assert first == second
    by_category = {item.category: item for item in first[1]}
    assert by_category[AttributionCategory.FEES].value == Decimal("0.08000000")
    assert by_category[AttributionCategory.SLIPPAGE].value == Decimal("0.12000000")
    assert first[0].canonical_hash == second[0].canonical_hash


def test_health_fails_closed_and_never_carries_authority() -> None:
    records = tuple(
        materialize_attribution(
            attribution_id=f"health-{index}",
            source=_source(f"health-source-{index}"),
            materialized_at=NOW,
        )[0]
        for index in range(1, 4)
    )
    insufficient = assess_strategy_health(
        health_id="health-insufficient", attributions=records[:1], as_of=NOW
    )
    healthy = assess_strategy_health(health_id="health-healthy", attributions=records, as_of=NOW)
    assert insufficient.state is StrategyHealthState.INSUFFICIENT_SAMPLE
    assert healthy.state is StrategyHealthState.HEALTHY
    assert healthy.automatic_action is False
    assert healthy.authority == healthy.live_authority == "NONE"
    assert StrategyHealthPolicy.v1().minimum_settled_outcomes == 2

    no_operation_source = replace(
        _source("no-operation-15", realized=None, filled="0", settled=False),
        authorization_id=None,
        order_id=None,
        reservation_id=None,
        reservation_original_amount=None,
        reservation_current_amount=None,
        reservation_consumed_amount=None,
        fill_ids=(),
        position_id=None,
    )
    no_operation = materialize_attribution(
        attribution_id="no-operation-attribution",
        source=no_operation_source,
        materialized_at=NOW,
    )[0]
    assessment = assess_strategy_health(
        health_id="no-operation-health", attributions=(no_operation,), as_of=NOW
    )
    assert assessment.state is StrategyHealthState.NO_OPERATIONAL_EVIDENCE


def test_reconciliation_and_data_quality_block_before_performance_interpretation() -> None:
    blocked = materialize_attribution(
        attribution_id="recon-attribution",
        source=_source(
            "recon-source-10",
            reconciliation="MATERIAL_DISCREPANCY",
            issues=("BROKER_QUANTITY_MISMATCH",),
        ),
        materialized_at=NOW,
    )[0]
    assessment = assess_strategy_health(
        health_id="recon-health", attributions=(blocked,), as_of=NOW
    )
    assert assessment.state is StrategyHealthState.RECONCILIATION_BLOCKED

    missing = replace(
        _source("quality-source-11"),
        source_opportunity_id=None,
        context_at_consideration_id=None,
    )
    quality = materialize_attribution(
        attribution_id="quality-attribution", source=missing, materialized_at=NOW
    )[0]
    assessment = assess_strategy_health(
        health_id="quality-health", attributions=(quality,), as_of=NOW
    )
    assert assessment.state is StrategyHealthState.DATA_QUALITY_BLOCKED


def test_fixture_and_genuine_evidence_cannot_mix_in_health_or_summary() -> None:
    fixture = materialize_attribution(
        attribution_id="fixture-attribution",
        source=_source("mixed-source-12"),
        materialized_at=NOW,
    )[0]
    genuine_source = replace(
        _source("mixed-source-13"), evidence_class=PaperEvidenceClass.GENUINE_PAPER
    )
    genuine = materialize_attribution(
        attribution_id="genuine-attribution", source=genuine_source, materialized_at=NOW
    )[0]
    with pytest.raises(PerformanceAttributionError, match="EVIDENCE_MIXED"):
        assess_strategy_health(health_id="mixed-health", attributions=(fixture, genuine), as_of=NOW)
    with pytest.raises(PerformanceAttributionError, match="EVIDENCE_MIXED"):
        summarize_performance(
            summary_id="mixed-summary",
            scope_type="PORTFOLIO",
            scope_id="MIXED",
            attributions=(fixture, genuine),
            evidence_class=PaperEvidenceClass.GENUINE_PAPER,
            as_of=NOW,
        )


def test_operator_api_exposes_read_only_attribution_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    class Engine:
        def dispose(self) -> None:
            return None

    monkeypatch.setattr("apex.operator_api.create_postgres_engine", lambda _settings: Engine())
    monkeypatch.setattr(
        "apex.operator_api.postgres_operator_console_store", lambda _engine: object()
    )
    monkeypatch.setattr(
        "apex.operator_api.postgres_shadow_operations_store", lambda _engine: object()
    )
    monkeypatch.setattr(
        "apex.operator_api.postgres_evidence_review_store", lambda _engine: object()
    )
    from apex.operator_api import create_app

    routes = {
        (route.path, next(iter(route.methods)))
        for route in create_app().routes
        if getattr(route, "methods", None)
    }
    assert ("/api/v1/performance-attribution/workspace", "GET") in routes
    assert (
        "/api/v1/performance-attribution/attributions/{attribution_id}",
        "GET",
    ) in routes
    assert ("/api/v1/performance-attribution/health/{health_id}", "GET") in routes


@pytest.mark.parametrize("attribution_count", [0, 1])
def test_operator_api_attribution_workspace_handles_empty_and_populated_states(
    monkeypatch: pytest.MonkeyPatch, attribution_count: int
) -> None:
    class Engine:
        def dispose(self) -> None:
            return None

    class Repository:
        def __init__(self, _engine: object) -> None:
            pass

        def workspace(self) -> dict[str, object]:
            return {
                "attributions": tuple(
                    {"attribution_id": f"item-{index}"} for index in range(attribution_count)
                ),
                "health": (),
                "paper_evidence_counts": {"attributions": attribution_count},
                "authority": "NONE",
                "live_authority": "NONE",
            }

    monkeypatch.setattr("apex.operator_api.create_postgres_engine", lambda _settings: Engine())
    monkeypatch.setattr("apex.operator_api.PostgresPerformanceAttributionRepository", Repository)
    monkeypatch.setattr(
        "apex.operator_api.postgres_operator_console_store", lambda _engine: object()
    )
    monkeypatch.setattr(
        "apex.operator_api.postgres_shadow_operations_store", lambda _engine: object()
    )
    monkeypatch.setattr(
        "apex.operator_api.postgres_evidence_review_store", lambda _engine: object()
    )
    from apex.operator_api import create_app

    app = create_app()
    route = next(
        item for item in app.routes if item.path == "/api/v1/performance-attribution/workspace"
    )
    payload = route.endpoint()
    assert payload["capability"] == "PAPER_ATTRIBUTION_READ_ONLY"
    assert payload["data"]["paper_evidence_counts"]["attributions"] == attribution_count
    assert len(payload["data"]["attributions"]) == attribution_count
