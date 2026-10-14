from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apex.adapters.securities_baseline_simulators import (
    ApexResearchSimulatorAdapter,
    DeterministicConformanceFixtureAdapter,
    LeanCanonicalSecuritiesAdapter,
    VectorbtSecuritiesAdapter,
)
from apex.adapters.securities_synthetic import synthetic_securities_dataset
from apex.application.securities_baselines import (
    CompareStrategyLabWithCanonicalBaseline,
    InMemorySecuritiesBaselineRepository,
    SecuritiesBaselineIdempotencyConflict,
    select_current_securities_conformance_reports,
)
from apex.domain.securities_baselines import (
    CanonicalBaselineFamily,
    CanonicalSecuritiesSimulationRequest,
    MultiSimulatorConformanceClassification,
    SecuritiesBaselineError,
    SecuritiesConformanceMaterialityPolicy,
    SecuritiesSimulationState,
    SecuritiesSimulatorIdentity,
    build_baseline_scorecard,
    canonical_securities_baselines,
    compare_simulator_runs,
    research_trial_summary,
    run_apex_research_portfolio,
    scorecard_conformance,
    simulation_run,
)
from apex.domain.values import content_digest

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _evidence() -> tuple[object, ...]:
    instruments, sessions, bars, actions = synthetic_securities_dataset()
    universe = tuple(item.instrument_id for item in instruments[:4])
    return instruments, sessions, bars, actions, universe


def _request(
    baseline_index: int = 0,
) -> tuple[object, ...]:
    instruments, sessions, bars, actions, universe = _evidence()
    baseline = canonical_securities_baselines(NOW)[baseline_index]
    request = CanonicalSecuritiesSimulationRequest.create(
        baseline=baseline,
        data_batch_id="batch-canonical-baselines",
        data_batch_hash=content_digest({"batch": "canonical-baselines"}),
        universe=universe,
        start_date=sessions[0].session_date,
        end_date=sessions[-2].session_date,
        cutoff=sessions[-1].closes_at + timedelta(days=1),
    )
    return baseline, request, bars, actions, instruments, sessions


def test_all_eight_canonical_baselines_are_transparent_and_deterministic() -> None:
    _instruments, sessions, bars, actions, universe = _evidence()
    baselines = canonical_securities_baselines(NOW)

    assert len(baselines) == 8
    assert {item.family for item in baselines} == set(CanonicalBaselineFamily)
    assert len({item.baseline_id for item in baselines}) == 8

    for baseline in baselines:
        request = CanonicalSecuritiesSimulationRequest.create(
            baseline=baseline,
            data_batch_id="batch-canonical-baselines",
            data_batch_hash=content_digest({"batch": "canonical-baselines"}),
            universe=universe,
            start_date=sessions[0].session_date,
            end_date=sessions[-2].session_date,
            cutoff=sessions[-1].closes_at + timedelta(days=1),
        )
        first = run_apex_research_portfolio(
            request=request,
            baseline=baseline,
            bars=bars,
            corporate_actions=actions,
        )
        second = run_apex_research_portfolio(
            request=request,
            baseline=baseline,
            bars=bars,
            corporate_actions=actions,
        )

        assert first == second
        assert first.state is SecuritiesSimulationState.COMPLETED
        assert first.canonical_result_hash is not None
        assert len(first.equity_curve) == 499
        assert all(
            transaction.transaction_at > transaction.decision_at
            for transaction in first.transactions
        )

    cash_request = CanonicalSecuritiesSimulationRequest.create(
        baseline=baselines[0],
        data_batch_id="batch-canonical-baselines",
        data_batch_hash=content_digest({"batch": "canonical-baselines"}),
        universe=universe,
        start_date=sessions[0].session_date,
        end_date=sessions[-2].session_date,
        cutoff=sessions[-1].closes_at + timedelta(days=1),
    )
    cash = run_apex_research_portfolio(
        request=cash_request,
        baseline=baselines[0],
        bars=bars,
        corporate_actions=actions,
    )
    assert cash.net_return == Decimal(0)
    assert cash.turnover == Decimal(0)
    assert cash.average_exposure == Decimal(0)


def test_delisting_without_terminal_value_fails_closed() -> None:
    baseline, _request_without_delisting, bars, actions, instruments, sessions = _request()
    universe = tuple(item.instrument_id for item in instruments[:6])
    request = CanonicalSecuritiesSimulationRequest.create(
        baseline=baseline,
        data_batch_id="batch-delisting",
        data_batch_hash=content_digest({"batch": "delisting"}),
        universe=universe,
        start_date=sessions[0].session_date,
        end_date=sessions[-1].session_date,
        cutoff=sessions[-1].closes_at + timedelta(days=1),
    )

    result = run_apex_research_portfolio(
        request=request,
        baseline=baseline,
        bars=bars,
        corporate_actions=actions,
    )

    assert result.state is SecuritiesSimulationState.FAILED
    assert "DELISTING_TERMINAL_VALUE_REQUIRED" in result.reason_codes


def test_three_layer_conformance_and_request_mismatch() -> None:
    baseline, request, bars, actions, _instruments, _sessions = _request(1)
    apex_result = ApexResearchSimulatorAdapter().run(
        request=request,
        baseline=baseline,
        bars=bars,
        corporate_actions=actions,
    )
    exact_result = DeterministicConformanceFixtureAdapter().run(
        request=request,
        baseline=baseline,
        bars=bars,
        corporate_actions=actions,
    )
    explained_result = DeterministicConformanceFixtureAdapter(difference="EXPLAINED").run(
        request=request,
        baseline=baseline,
        bars=bars,
        corporate_actions=actions,
    )
    material_result = DeterministicConformanceFixtureAdapter(difference="MATERIAL").run(
        request=request,
        baseline=baseline,
        bars=bars,
        corporate_actions=actions,
    )
    apex = simulation_run(
        request=request,
        result=apex_result,
        idempotency_key="apex",
        started_at=NOW,
        completed_at=NOW,
    )
    exact = simulation_run(
        request=request,
        result=exact_result,
        idempotency_key="exact",
        started_at=NOW,
        completed_at=NOW,
    )
    explained = simulation_run(
        request=request,
        result=explained_result,
        idempotency_key="explained",
        started_at=NOW,
        completed_at=NOW,
    )
    material = simulation_run(
        request=request,
        result=material_result,
        idempotency_key="material",
        started_at=NOW,
        completed_at=NOW,
    )
    policy = SecuritiesConformanceMaterialityPolicy.v1()

    conformant_report = compare_simulator_runs(
        reference=apex,
        comparison=exact,
        policy=policy,
    )
    assert conformant_report.classification is MultiSimulatorConformanceClassification.CONFORMANT
    assert (
        compare_simulator_runs(
            reference=apex,
            comparison=explained,
            policy=policy,
            explanation_codes=("ROUNDING_POLICY_DIFFERENCE",),
        ).classification
        is MultiSimulatorConformanceClassification.EXPLAINED_DIFFERENCE
    )
    material_report = compare_simulator_runs(
        reference=apex,
        comparison=material,
        policy=policy,
    )
    assert (
        material_report.classification is MultiSimulatorConformanceClassification.MATERIAL_MISMATCH
    )
    scorecard_state, warnings = scorecard_conformance((conformant_report, material_report))
    assert scorecard_state is MultiSimulatorConformanceClassification.MATERIAL_MISMATCH
    assert "MATERIAL_SIMULATOR_MISMATCH_BLOCKS_AUTHORITATIVE_COMPARISON" in warnings
    mismatched = replace(material, canonical_input_hash=content_digest({"other": True}))
    assert (
        compare_simulator_runs(
            reference=apex,
            comparison=mismatched,
            policy=policy,
        ).classification
        is MultiSimulatorConformanceClassification.INCOMPARABLE_CONFIGURATION
    )


def test_repository_retains_trials_and_strategy_lab_integrity_blocker() -> None:
    baseline, request, bars, actions, _instruments, _sessions = _request(1)
    result = ApexResearchSimulatorAdapter().run(
        request=request,
        baseline=baseline,
        bars=bars,
        corporate_actions=actions,
    )
    run = simulation_run(
        request=request,
        result=result,
        idempotency_key="retained-run",
        started_at=NOW,
        completed_at=NOW,
    )
    repository = InMemorySecuritiesBaselineRepository()
    repository.save_baseline(baseline)
    repository.save_run(request=request, run=run)
    repository.save_run(request=request, run=run)
    conflict = replace(run, canonical_input_hash=content_digest({"conflict": True}))
    with pytest.raises(SecuritiesBaselineIdempotencyConflict):
        repository.save_run(request=request, run=conflict)

    scorecard = build_baseline_scorecard(
        baseline=baseline,
        request=request,
        run=run,
        conformance_state=MultiSimulatorConformanceClassification.MATERIAL_MISMATCH,
    )
    repository.save_scorecard(scorecard)
    comparison = CompareStrategyLabWithCanonicalBaseline(repository).execute(
        candidate_id="candidate-aligned",
        candidate_result_hash=content_digest({"candidate": "aligned"}),
        candidate_data_batch_hash=request.data_batch_hash,
        candidate_universe_hash=request.universe_hash,
        candidate_start_date=request.start_date,
        candidate_end_date=request.end_date,
        candidate_net_return="0.1",
        scorecard=scorecard,
    )

    assert comparison.aligned is True
    assert comparison.integrity_blocked is True
    assert research_trial_summary(repository.runs(), repository.baselines(), ())["total_runs"] == 1


def test_lean_unavailability_is_honest() -> None:
    baseline, request, bars, actions, _instruments, _sessions = _request()
    adapter = LeanCanonicalSecuritiesAdapter()
    result = adapter.run(
        request=request,
        baseline=baseline,
        bars=bars,
        corporate_actions=actions,
    )

    assert result.state is SecuritiesSimulationState.RUNTIME_UNAVAILABLE
    assert any("LEAN_" in reason for reason in result.reason_codes)


def test_pairs_requires_even_training_test_universe() -> None:
    baseline, request, bars, actions, instruments, _sessions = _request(6)
    mismatched = replace(
        request,
        universe=tuple(item.instrument_id for item in instruments[:3]),
    )
    with pytest.raises(SecuritiesBaselineError, match="even explicit universe"):
        run_apex_research_portfolio(
            request=mismatched,
            baseline=baseline,
            bars=bars,
            corporate_actions=actions,
        )


def test_vectorbt_runs_only_retained_local_evidence(tmp_path: Path) -> None:
    baseline, request, bars, actions, _instruments, _sessions = _request(1)
    adapter = VectorbtSecuritiesAdapter(tmp_path)

    result = adapter.run(
        request=request,
        baseline=baseline,
        bars=bars,
        corporate_actions=actions,
    )

    assert adapter.availability() == (True, "1.1.0")
    assert result.state is SecuritiesSimulationState.COMPLETED
    assert "VECTORBT_PORTFOLIO_EXECUTED" in result.reason_codes
    assert result.managed_result_artifact is not None
    assert Path(result.managed_result_artifact).is_file()


def test_external_simulator_boundary_has_no_network_or_financial_authority() -> None:
    from apex.adapters import securities_baseline_simulators

    source = inspect.getsource(securities_baseline_simulators)
    for forbidden in (
        "requests.",
        "yfinance",
        ".download(",
        "subprocess",
        "socket",
        "broker",
        "credential",
        "api_key",
    ):
        assert forbidden not in source.lower()


def test_retained_request_and_current_selection_preserve_exact_scope() -> None:
    baseline, request, bars, actions, _instruments, _sessions = _request(1)
    request = CanonicalSecuritiesSimulationRequest.create(
        baseline=baseline,
        data_batch_id=request.data_batch_id,
        data_batch_hash=request.data_batch_hash,
        universe=request.universe,
        start_date=request.start_date,
        end_date=request.end_date,
        cutoff=request.cutoff,
        starting_capital="12345.67890123",
        execution_policy_hash="retained-policy-hash",
        spread_bps="1.23456789",
        slippage_bps="2.34567891",
        commission_bps="3.45678901",
    )
    apex_result = ApexResearchSimulatorAdapter().run(
        request=request, baseline=baseline, bars=bars, corporate_actions=actions
    )
    reference = simulation_run(
        request=request,
        result=apex_result,
        idempotency_key="retained-reference",
        started_at=NOW,
        completed_at=NOW,
    )
    previous = simulation_run(
        request=request,
        result=replace(
            apex_result,
            simulator_identity=SecuritiesSimulatorIdentity.VECTORBT,
            adapter_version="VECTORBT_CANONICAL_BASELINE_V1",
        ),
        idempotency_key="retained-vectorbt-v1",
        started_at=NOW,
        completed_at=NOW,
    )
    replacement = simulation_run(
        request=request,
        result=replace(
            apex_result,
            simulator_identity=SecuritiesSimulatorIdentity.VECTORBT,
            adapter_version="VECTORBT_CANONICAL_BASELINE_V2",
        ),
        idempotency_key="retained-vectorbt-v2",
        started_at=NOW,
        completed_at=NOW,
    )
    repository = InMemorySecuritiesBaselineRepository()
    repository.save_baseline(baseline)
    for run in (reference, previous, replacement):
        repository.save_run(request=request, run=run)
    previous_report = compare_simulator_runs(
        reference=reference,
        comparison=previous,
        policy=SecuritiesConformanceMaterialityPolicy.v1(),
    )
    replacement_report = compare_simulator_runs(
        reference=reference,
        comparison=replacement,
        policy=SecuritiesConformanceMaterialityPolicy.v1(),
    )
    repository.save_report(previous_report)
    repository.save_report(replacement_report)

    loaded_report, loaded_request = repository.retained_request(previous_report.report_id)
    selected = select_current_securities_conformance_reports(
        reports=repository.reports(), runs=repository.runs()
    )

    assert loaded_report.report_id == previous_report.report_id
    assert loaded_request == request
    assert loaded_request.starting_capital == Decimal("12345.67890123")
    assert loaded_request.commission_bps == Decimal("3.45678901")
    assert selected[0].current_report_id == replacement_report.report_id
    assert selected[0].historical_report_ids == tuple(
        sorted((previous_report.report_id, replacement_report.report_id))
    )
