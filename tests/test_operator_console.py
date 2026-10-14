"""Focused local Operator Console contract tests without a database fixture."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from apex.application.operator_console import (
    CreateMoonshotCampaignCommand,
    OperatorMode,
)
from apex.domain.autopilot import EvidenceTier
from apex.domain.values import Money, RiskFraction, ValueValidationError
from apex.operator_api import (
    LOCAL_HOSTS,
    AtomicShadowEvidencePlanRequest,
    CampaignCreateRequest,
    MultiStrategyShadowSessionRequest,
    SecuritiesBaselineSuiteRequest,
    SecuritiesDataBatchRequest,
    SecuritiesPaperConformanceRequest,
    SecuritiesPaperExitRequest,
    SecuritiesPaperRunRequest,
    SecuritiesResearchRequest,
    SettleRequest,
    ShadowEvidencePlanRequest,
    _jsonable,
    _mutation_guard,
    app,
)


def _request(*, origin: str | None, idempotency_key: str | None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if idempotency_key is not None:
        headers.append((b"idempotency-key", idempotency_key.encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def test_operator_api_keeps_authoritative_decimals_as_strings() -> None:
    request = CampaignCreateRequest(
        name="Local campaign",
        initial_capital="1000.00",
        maximum_authorized_loss="500",
        starts_at=datetime(2026, 7, 23, tzinfo=UTC),
        expires_at=datetime(2026, 7, 24, tzinfo=UTC),
        operating_mode=OperatorMode.PAPER,
        maximum_active_risk_fraction="0.5",
        maximum_per_opportunity_risk_fraction="0.1",
        maximum_per_strategy_risk_fraction="0.25",
        maximum_per_correlation_bucket_risk_fraction="0.25",
        maximum_simultaneous_reservations=4,
        maximum_simultaneous_positions=4,
        maximum_capital_lockup_seconds=3600,
        minimum_conservative_expected_value="0",
        minimum_evidence_tier=EvidenceTier.RESEARCH,
    )

    assert request.initial_capital == "1000.00"
    assert _jsonable({"capital": Decimal("1000.00")}) == {"capital": "1000"}


def test_mutating_operator_requests_require_local_origin_and_idempotency() -> None:
    assert "127.0.0.1" in LOCAL_HOSTS
    asyncio.run(_mutation_guard(_request(origin="http://127.0.0.1:5173", idempotency_key="x")))
    with pytest.raises(HTTPException) as external:
        asyncio.run(_mutation_guard(_request(origin="https://example.test", idempotency_key="x")))
    assert external.value.status_code == 403
    with pytest.raises(HTTPException) as missing_key:
        asyncio.run(_mutation_guard(_request(origin="http://localhost:5173", idempotency_key=None)))
    assert missing_key.value.status_code == 400


def test_campaign_command_rejects_unbounded_policy() -> None:
    now = datetime(2026, 7, 23, tzinfo=UTC)
    with pytest.raises(ValueValidationError, match="authorized loss"):
        CreateMoonshotCampaignCommand(
            campaign_id="campaign",
            name="Campaign",
            initial_capital=Money("100"),
            maximum_authorized_loss=Money("101"),
            starts_at=now,
            expires_at=now + timedelta(hours=1),
            operating_mode=OperatorMode.SHADOW,
            maximum_active_risk_fraction=RiskFraction("0.5"),
            maximum_per_opportunity_risk_fraction=RiskFraction("0.1"),
            maximum_per_strategy_risk_fraction=RiskFraction("0.25"),
            maximum_per_correlation_bucket_risk_fraction=RiskFraction("0.25"),
            maximum_simultaneous_reservations=4,
            maximum_simultaneous_positions=4,
            maximum_capital_lockup_seconds=3600,
            minimum_conservative_expected_value=Money("0"),
            minimum_evidence_tier=EvidenceTier.RESEARCH,
            allow_profit_compounding=False,
            operator_note="",
            idempotency_key="key",
        )


def test_settlement_request_accepts_only_a_retained_fact_identity() -> None:
    assert SettleRequest(settlement_fact_id="retained-fact", operator_note="local review")
    with pytest.raises(ValidationError):
        SettleRequest.model_validate(
            {
                "settlement_fact_id": "retained-fact",
                "payout": "1",
                "classification": "YES",
                "rule_text": "operator supplied text is forbidden",
            }
        )


def test_local_operator_api_exposes_no_generated_docs_or_live_surface() -> None:
    paths = {route.path for route in app.routes}

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    assert "/api/v1/campaigns/{campaign_id}/mark-evidence" in paths
    assert "/api/v1/positions/{position_id}/settle" in paths
    assert "/api/v1/campaigns/{campaign_id}/multi-strategy-shadow-session" in paths
    assert "/api/v1/multi-strategy-cycles/{cycle_id}" in paths
    assert "/api/v1/campaigns/{campaign_id}/shadow-plans" in paths
    assert "/api/v1/campaigns/{campaign_id}/shadow-plans/discover-and-create" in paths
    assert "/api/v1/shadow-plans/{plan_id}/run-due" in paths
    assert "/api/v1/campaigns/{campaign_id}/collect-shadow-outcomes" in paths
    assert "/api/v1/campaigns/{campaign_id}/strategy-performance" in paths
    assert "/api/v1/strategies/{strategy_id}/performance" in paths
    assert "/api/v1/securities/data-batches" in paths
    assert "/api/v1/securities/real-data/provider" in paths
    assert "/api/v1/securities/real-data/universes" in paths
    assert "/api/v1/securities/real-data/fetch-operations" in paths
    assert "/api/v1/securities/real-data/quality" in paths
    assert "/api/v1/securities/real-data/historical-validations" in paths
    assert "/api/v1/securities/real-data/forward-plans" in paths
    assert "/api/v1/securities/genuine-strategy-datasets" in paths
    assert "/api/v1/securities/genuine-strategy-datasets/{dataset_id}" in paths
    assert "/api/v1/securities/research-runs" in paths
    assert "/api/v1/securities/research-runs/{research_run_id}/signals" in paths
    assert "/api/v1/securities/conformance" in paths
    assert "/api/v1/securities/paper" in paths
    assert "/api/v1/securities/paper/runs" in paths
    assert "/api/v1/securities/paper/campaigns/{campaign_id}/marks" in paths
    assert "/api/v1/securities/paper/campaigns/{campaign_id}/corporate-actions" in paths
    assert "/api/v1/securities/paper/positions/{position_id}/exit" in paths
    assert "/api/v1/securities/paper/conformance" in paths
    assert "/api/v1/securities/baselines" in paths
    assert "/api/v1/securities/baseline-suites" in paths
    assert "/api/v1/securities/baseline-runs" in paths
    assert "/api/v1/securities/baseline-scorecards" in paths
    assert "/api/v1/securities/simulator-conformance" in paths
    assert "/api/v1/securities/simulator-conformance/{report_id}" in paths
    assert "/api/v1/securities/simulator-status" in paths
    assert "/api/v1/broad-equity/workspace" in paths
    assert "/api/v1/options-intelligence/workspace" in paths
    assert "/api/v1/research/hypotheses/{hypothesis_version_id}" in paths
    assert "/api/v1/research/captures/{capture_id}" in paths
    assert "/api/v1/research/snapshots/{snapshot_id}" in paths
    assert "/api/v1/research/runs/{run_id}" in paths
    assert "/api/v1/research/outcomes/{outcome_id}" in paths
    assert "/api/v1/external-paper/readiness" in paths
    assert "/api/v1/external-paper/alpaca/instructions/{instruction_id}/status" in paths
    assert "/api/v1/external-paper/alpaca/instructions/{instruction_id}/synchronize" in paths
    assert "/api/v1/external-paper/alpaca/instructions/{instruction_id}/validate-fills" in paths
    assert "/api/v1/external-paper/alpaca/instructions/{instruction_id}/apply-fills" in paths
    assert "/api/v1/external-paper/instructions" in paths
    assert "/api/v1/external-paper/instructions/{instruction_id}/prepare" in paths
    assert "/api/v1/external-paper/instructions/{instruction_id}/submit" in paths
    assert "/api/v1/external-paper/instructions/{instruction_id}/synchronize" in paths
    assert "/api/v1/external-paper/instructions/{instruction_id}/cancel" in paths
    assert "/api/v1/external-paper/instructions/{instruction_id}/reduce" in paths
    assert "/api/v1/external-paper/instructions/{instruction_id}/apply-fills" in paths
    assert "/api/v1/external-paper/instructions/{instruction_id}/validate-fills" in paths
    assert "/api/v1/external-paper/instructions/{instruction_id}/reconciliation" in paths
    assert "/api/v1/external-paper/instructions/{instruction_id}/fill-timeline" in paths
    assert "/api/v1/strategy-lab/automated-searches" in paths
    assert "/api/v1/strategy-lab/automated-searches/{search_plan_id}/runs" in paths
    assert "/api/v1/strategy-lab/automated-search-runs/{search_run_id}" in paths
    assert "/api/v1/strategy-lab/automated-search-runs/{search_run_id}/recover" in paths
    assert "/api/v1/strategy-lab/automated-search-runs/{search_run_id}/cancel" in paths
    assert "/api/v1/strategy-lab/mlflow-mirror/{experiment_id}/reconcile" in paths
    assert "/api/v1/strategy-lab/learning-plans" in paths
    assert "/api/v1/strategy-lab/learning-plans/{plan_id}/activate" in paths
    assert "/api/v1/strategy-lab/learning-plans/{plan_id}/trigger" in paths
    assert "/api/v1/strategy-lab/learning-cycles/{cycle_id}/run" in paths
    assert "/api/v1/strategy-lab/learning-cycles/{cycle_id}/recover" in paths
    assert "/api/v1/strategy-lab/learning-cycles/{cycle_id}/attach-challenger" in paths
    assert "/api/v1/strategy-lab/forward-windows/{window_id}/infer" in paths
    assert "/api/v1/strategy-lab/forward-windows/{window_id}/evaluate" in paths
    live_paths = {path for path in paths if "live" in path.lower()}
    assert live_paths
    assert all("live-readiness" in path or "live-preflight" in path for path in live_paths)
    assert not any(
        action in path.lower()
        for path in live_paths
        for action in ("submit", "cancel", "transfer", "withdraw")
    )
    assert all(
        next(route for route in app.routes if route.path == path).methods == {"GET"}
        for path in live_paths
        if "live-preflight" in path
    )
    safe_live_mutations = {
        "/api/v1/live-readiness/credential-references",
        "/api/v1/live-readiness/capabilities",
        "/api/v1/live-readiness/execution-controls/{control_id}",
        "/api/v1/live-readiness/assessments",
        "/api/v1/live-readiness/exports",
        "/api/v1/live-readiness/coinbase/read-approvals",
        "/api/v1/live-readiness/coinbase/read-inspections",
    }
    assert safe_live_mutations <= paths
    assert all(
        next(route for route in app.routes if route.path == path).methods == {"POST"}
        for path in safe_live_mutations
    )
    assert "/api/v1/live-readiness/coinbase/read-only-workspace" in paths
    assert "/api/v1/live-readiness/coinbase/read-approvals/{approval_id}" in paths
    assert "/api/v1/live-readiness/coinbase/captures/{capture_id}" in paths


def test_frontend_keeps_review_and_retained_evidence_controls_visible() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "src" / "main.tsx").read_text()

    assert "Review Campaign" in source
    assert "Mark with retained evidence" in source
    assert "Settle from fact" in source
    assert "SIMULATION / PAPER ONLY" in source
    assert "Multi-Strategy Evaluation" in source
    assert "SHADOW / HYPOTHETICAL — NO CAPITAL RESERVED" in source
    assert "Shadow Operations" in source
    assert "Create finite Shadow Plan" in source
    assert "Public outcomes are scoring facts, never financial settlement" in source
    assert "SECURITIES PAPER — SIMULATED CAPITAL ONLY" in source
    assert "NO BROKER OR LIVE MARKET AUTHORITY" in source
    assert "EXTERNAL ENGINE OUTPUT IS EVIDENCE, NOT APEX AUTHORITY" in source
    assert "Import Managed Dataset" in source
    assert "Run Strategy Lab" in source
    assert "Run Approved PAPER Trial" in source
    assert "Mark Positions" in source
    assert "Apply Corporate Actions" in source
    assert "Exit Position" in source
    assert "CANONICAL BASELINE — TRANSPARENT RESEARCH SPECIFICATION" in source
    assert "BASELINES ARE RESEARCH BENCHMARKS, NOT PERFORMANCE PROMISES" in source
    assert "Run Bounded Baseline Suite" in source
    assert "Baseline scorecards and simulator integrity" in source
    assert "Inspect evidence" in source
    assert "Canonical request hash" in source
    assert "Bounded automated candidate generation" in source
    assert "OPTUNA PROPOSES PARAMETERS — APEX OWNS THE TRIAL LEDGER AND DECISION" in source
    assert "MLFLOW IS A LOCAL RESEARCH MIRROR — IT GRANTS NO AUTHORITY" in source
    assert "Complete automated trial ledger" in source
    assert "Start Search" in source
    assert "Resume / Recover" in source
    assert "Cancel Search" in source
    assert "Reconcile MLflow" in source
    assert "intentionally has no Deploy, Champion, PAPER, or LIVE control" in source
    assert "Automated batch learning" in source
    assert "AUTOMATED LEARNING DOES NOT AUTHORIZE MODEL DEPLOYMENT" in source
    assert "CHAMPION REPLACEMENT REQUIRES EXPLICIT OPERATOR ACTION" in source
    assert "Create Draft Learning Plan" in source
    assert "Attach Existing Approved Challenger" in source


def test_securities_api_accepts_only_managed_ids_and_shadow_research_terms() -> None:
    assert SecuritiesDataBatchRequest(
        managed_source="incoming/evidence.jsonl",
        dataset_name="Retained evidence",
    )
    for unsafe in (
        "/tmp/evidence.jsonl",
        "../evidence.jsonl",
        "incoming\\evidence.jsonl",
    ):
        with pytest.raises(ValidationError):
            SecuritiesDataBatchRequest(
                managed_source=unsafe,
                dataset_name="unsafe",
            )
    request = SecuritiesResearchRequest(
        campaign_id="campaign",
        strategy_id="securities-cash-baseline-v1",
        data_batch_id="batch",
        universe=["instrument"],
        start="2026-01-01",
        end="2026-01-31",
        decision_cutoff=datetime(2026, 2, 1, tzinfo=UTC),
        benchmark_instrument_id="instrument",
    )
    assert request.universe == ["instrument"]
    with pytest.raises(ValidationError):
        SecuritiesResearchRequest.model_validate({**request.model_dump(), "capital": "100"})


def test_securities_paper_api_accepts_only_governed_simulation_terms() -> None:
    request = SecuritiesPaperRunRequest(
        campaign_id="campaign",
        trial_id="trial",
        strategy_id="governed-securities-strategy",
        data_batch_id="batch",
        start="2026-01-01",
        end="2026-01-31",
    )
    assert request.trial_id == "trial"
    assert SecuritiesPaperExitRequest(
        as_of=datetime(2026, 1, 31, tzinfo=UTC),
        reason="OPERATOR_EXIT",
    )
    assert SecuritiesPaperConformanceRequest(
        paper_run_id="paper-run",
        external_engine="LEAN",
    )
    with pytest.raises(ValidationError):
        SecuritiesPaperConformanceRequest(
            paper_run_id="paper-run",
            external_engine="BROKER",
        )
    with pytest.raises(ValidationError):
        SecuritiesPaperRunRequest.model_validate(
            {**request.model_dump(), "broker_account": "forbidden"}
        )


def test_securities_baseline_suite_accepts_research_simulators_only() -> None:
    request = SecuritiesBaselineSuiteRequest(
        campaign_id="campaign",
        data_batch_id="batch",
        universe=["instrument-a", "instrument-b"],
        start="2026-01-01",
        end="2026-12-31",
        decision_cutoff=datetime(2027, 1, 1, tzinfo=UTC),
        simulators=["APEX_RESEARCH", "VECTORBT", "LEAN"],
    )
    assert len(request.simulators) == 3
    with pytest.raises(ValidationError, match="research simulators only"):
        SecuritiesBaselineSuiteRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "simulators": ["APEX_PAPER"],
            }
        )


def test_atomic_shadow_plan_request_has_only_bounded_public_controls() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    request = AtomicShadowEvidencePlanRequest(
        name="Atomic rolling evidence",
        window_start=now + timedelta(hours=1),
        window_end=now + timedelta(days=7),
        iterations=24,
        interval_seconds=300,
        target_markets=30,
        minimum_markets=5,
        minimum_events=5,
        maximum_pages=5,
        maximum_inspected_markets=500,
        maximum_enrichments=80,
        maximum_markets_per_event=1,
        maximum_markets_per_series=6,
        orderbook_depth=5,
    )

    assert request.target_markets == 30
    assert request.exclude_plan_ids == []
    assert request.exclude_prior_plan_markets is True
    assert request.exclude_prior_plan_events is False
    assert request.exclude_active_plan_markets is True
    assert request.maximum_orderbook_preflights == 80
    assert request.microstructure_configuration_version == "orderbook-microstructure-shadow-v1"
    assert AtomicShadowEvidencePlanRequest(
        **{
            **request.model_dump(),
            "exclude_plan_ids": ["prior-plan"],
            "exclude_prior_plan_events": True,
        }
    ).exclude_plan_ids == ["prior-plan"]
    with pytest.raises(ValidationError):
        AtomicShadowEvidencePlanRequest.model_validate(
            {**request.model_dump(), "order": {"side": "YES"}}
        )
    with pytest.raises(ValidationError):
        AtomicShadowEvidencePlanRequest(
            **{**request.model_dump(), "exclude_plan_ids": ["prior"] * 21}
        )
    with pytest.raises(ValidationError):
        AtomicShadowEvidencePlanRequest(
            **{**request.model_dump(), "maximum_orderbook_preflights": 81}
        )


def test_multi_strategy_session_request_uses_only_bounded_public_controls() -> None:
    request = MultiStrategyShadowSessionRequest(
        iterations=4,
        interval_seconds=30,
        market_limit=20,
        maximum_pages=3,
        maximum_enrichments=40,
        orderbook_depth=5,
    )

    assert request.market_limit == 20
    with pytest.raises(ValidationError):
        MultiStrategyShadowSessionRequest(iterations=2)


def test_shadow_evidence_plan_request_is_finite_and_has_no_trade_terms() -> None:
    request = ShadowEvidencePlanRequest(name="Finite forward evidence")

    assert request.iterations == 12
    assert request.interval_seconds == 300
    assert request.market_count == 10
    assert ShadowEvidencePlanRequest(
        name="Exact forward evidence", market_count=3, market_tickers=["KX-A", "KX-B", "KX-C"]
    ).market_tickers == ["KX-A", "KX-B", "KX-C"]
    with pytest.raises(ValidationError):
        ShadowEvidencePlanRequest(interval_seconds=4)
    with pytest.raises(ValidationError):
        ShadowEvidencePlanRequest(name="too few tickers", market_tickers=["KX-A", "KX-B"])
    with pytest.raises(ValidationError):
        ShadowEvidencePlanRequest.model_validate({"name": "plan", "limit_price": "0.50"})
