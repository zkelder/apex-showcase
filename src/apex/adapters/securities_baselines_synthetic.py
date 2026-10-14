"""End-to-end provider-free canonical Securities baseline demonstration."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apex.adapters.securities_baseline_simulators import (
    ApexPaperSimulatorAdapter,
    ApexResearchSimulatorAdapter,
    DeterministicConformanceFixtureAdapter,
    LeanCanonicalSecuritiesAdapter,
    VectorbtSecuritiesAdapter,
)
from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter, securities_record
from apex.adapters.securities_synthetic import synthetic_securities_dataset
from apex.application.securities import (
    ImportSecuritiesData,
    ImportSecuritiesDataCommand,
    InMemorySecuritiesRepository,
)
from apex.application.securities_baselines import (
    CompareCanonicalSecuritiesSimulators,
    CompareStrategyLabWithCanonicalBaseline,
    InMemorySecuritiesBaselineRepository,
    RegisterCanonicalSecuritiesBaselines,
    RunCanonicalSecuritiesBaseline,
    RunCanonicalSecuritiesBaselineCommand,
    SecuritiesBaselineIdempotencyConflict,
    simulator_catalog,
)
from apex.domain.securities import (
    SecuritiesBarEvidence,
    SecuritiesCorporateActionEvidence,
)
from apex.domain.securities_baselines import (
    BASELINE_RESEARCH_WARNING,
    CANONICAL_BASELINE_WARNING,
    EXTERNAL_SIMULATOR_WARNING,
    CanonicalSecuritiesBaseline,
    CanonicalSecuritiesSimulationRequest,
    SecuritiesConformanceMaterialityPolicy,
    SecuritiesSimulationRun,
    SecuritiesSimulatorIdentity,
    build_baseline_scorecard,
    compare_simulator_runs,
    research_trial_summary,
    run_apex_research_portfolio,
    scorecard_conformance,
    simulation_run,
)
from apex.domain.values import content_digest

DEMO_CAMPAIGN_ID = "campaign-securities-baselines-conformance-demo"


def run_securities_baselines_conformance_demo(
    *,
    data_root: Path | None = None,
    simulator_output_root: Path | None = None,
) -> dict[str, object]:
    dataset_adapter = LocalSecuritiesDatasetAdapter(data_root)
    output_root = simulator_output_root or Path(".local") / "securities" / "simulator-results"
    instruments, sessions, bars, actions = synthetic_securities_dataset()
    records = tuple(securities_record(item) for item in (*instruments, *sessions, *bars, *actions))
    source_hash = content_digest(records)
    managed_source = dataset_adapter.write_generated_jsonl(
        relative_location=f"generated/securities-baselines-{source_hash}.jsonl",
        records=records,
    )
    stage_one = InMemorySecuritiesRepository()
    batch = ImportSecuritiesData(
        repository=stage_one,
        market_data=dataset_adapter,
    ).execute(
        ImportSecuritiesDataCommand(
            managed_source=managed_source,
            dataset_name="Apex deterministic canonical baseline evidence",
            idempotency_key="securities-baselines-demo-data-v1",
            imported_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        )
    )
    repository = InMemorySecuritiesBaselineRepository()
    created_at = datetime(2026, 7, 27, 12, 1, tzinfo=UTC)
    baselines = RegisterCanonicalSecuritiesBaselines(repository).execute(created_at=created_at)
    simulators = (
        ApexResearchSimulatorAdapter(),
        ApexPaperSimulatorAdapter(),
        VectorbtSecuritiesAdapter(output_root),
        LeanCanonicalSecuritiesAdapter(),
        DeterministicConformanceFixtureAdapter(),
    )
    runner = RunCanonicalSecuritiesBaseline(
        repository=repository,
        data=stage_one,
        simulators=simulators,
    )
    universe = tuple(item.instrument_id for item in instruments[:4])
    start_date = sessions[0].session_date
    end_date = sessions[-2].session_date
    cutoff = sessions[-1].closes_at + timedelta(days=1)
    now = datetime(2026, 7, 27, 12, 2, tzinfo=UTC)
    apex_runs: list[SecuritiesSimulationRun] = []
    vectorbt_runs: list[SecuritiesSimulationRun] = []
    requests: dict[str, CanonicalSecuritiesSimulationRequest] = {}
    for baseline in baselines:
        request = CanonicalSecuritiesSimulationRequest.create(
            baseline=baseline,
            data_batch_id=batch.batch_id,
            data_batch_hash=batch.canonical_hash,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            cutoff=cutoff,
        )
        requests[baseline.baseline_id] = request
        apex_runs.append(
            runner.execute(
                RunCanonicalSecuritiesBaselineCommand(
                    campaign_id=DEMO_CAMPAIGN_ID,
                    baseline_id=baseline.baseline_id,
                    data_batch_id=batch.batch_id,
                    universe=universe,
                    start_date=start_date,
                    end_date=end_date,
                    cutoff=cutoff,
                    simulator=SecuritiesSimulatorIdentity.APEX_RESEARCH,
                    idempotency_key=f"baseline-demo-apex-{baseline.family.value.lower()}",
                    started_at=now,
                    completed_at=now,
                )
            )
        )
        vectorbt_runs.append(
            runner.execute(
                RunCanonicalSecuritiesBaselineCommand(
                    campaign_id=DEMO_CAMPAIGN_ID,
                    baseline_id=baseline.baseline_id,
                    data_batch_id=batch.batch_id,
                    universe=universe,
                    start_date=start_date,
                    end_date=end_date,
                    cutoff=cutoff,
                    simulator=SecuritiesSimulatorIdentity.VECTORBT,
                    idempotency_key=(f"baseline-demo-vectorbt-{baseline.family.value.lower()}"),
                    started_at=now,
                    completed_at=now,
                )
            )
        )
    replay = runner.execute(
        RunCanonicalSecuritiesBaselineCommand(
            campaign_id=DEMO_CAMPAIGN_ID,
            baseline_id=baselines[0].baseline_id,
            data_batch_id=batch.batch_id,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            cutoff=cutoff,
            simulator=SecuritiesSimulatorIdentity.APEX_RESEARCH,
            idempotency_key=(f"baseline-demo-apex-{baselines[0].family.value.lower()}"),
            started_at=now,
            completed_at=now,
        )
    )
    idempotency_conflict_rejected = False
    try:
        runner.execute(
            RunCanonicalSecuritiesBaselineCommand(
                campaign_id=DEMO_CAMPAIGN_ID,
                baseline_id=baselines[0].baseline_id,
                data_batch_id=batch.batch_id,
                universe=universe,
                start_date=start_date,
                end_date=sessions[-3].session_date,
                cutoff=cutoff,
                simulator=SecuritiesSimulatorIdentity.APEX_RESEARCH,
                idempotency_key=(f"baseline-demo-apex-{baselines[0].family.value.lower()}"),
                started_at=now,
                completed_at=now,
            )
        )
    except SecuritiesBaselineIdempotencyConflict:
        idempotency_conflict_rejected = True
    lean_run = runner.execute(
        RunCanonicalSecuritiesBaselineCommand(
            campaign_id=DEMO_CAMPAIGN_ID,
            baseline_id=baselines[0].baseline_id,
            data_batch_id=batch.batch_id,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            cutoff=cutoff,
            simulator=SecuritiesSimulatorIdentity.LEAN,
            idempotency_key="baseline-demo-lean-cash",
            started_at=now,
            completed_at=now,
        )
    )
    fixture_runs = _fixture_runs(
        repository=repository,
        baseline=baselines[1],
        request=requests[baselines[1].baseline_id],
        bars=bars,
        actions=actions,
        now=now,
    )
    comparer = CompareCanonicalSecuritiesSimulators(repository)
    conformant = comparer.execute(
        reference=apex_runs[1],
        comparison=fixture_runs[0],
    )
    explained = comparer.execute(
        reference=apex_runs[1],
        comparison=fixture_runs[1],
        explanation_codes=("ROUNDING_POLICY_DIFFERENCE",),
    )
    material = comparer.execute(
        reference=apex_runs[1],
        comparison=fixture_runs[2],
    )
    unavailable = comparer.execute(reference=apex_runs[0], comparison=lean_run)
    vectorbt_reports = tuple(
        comparer.execute(reference=apex, comparison=external)
        for apex, external in zip(apex_runs, vectorbt_runs, strict=True)
    )
    request_mismatch = compare_simulator_runs(
        reference=apex_runs[0],
        comparison=replace(
            fixture_runs[0],
            canonical_input_hash=content_digest({"mismatch": "canonical-request"}),
        ),
        policy=SecuritiesConformanceMaterialityPolicy.v1(),
    )
    scorecards = []
    for baseline, run in zip(baselines, apex_runs, strict=True):
        conformance_state, integrity_warnings = scorecard_conformance(
            tuple(item for item in vectorbt_reports if item.baseline_id == baseline.baseline_id)
        )
        scorecard = build_baseline_scorecard(
            baseline=baseline,
            request=requests[baseline.baseline_id],
            run=run,
            conformance_state=conformance_state,
            integrity_warnings=integrity_warnings,
        )
        repository.save_scorecard(scorecard)
        scorecards.append(scorecard)
    strategy_comparison = CompareStrategyLabWithCanonicalBaseline(repository).execute(
        candidate_id="strategy-lab-aligned-research-champion",
        candidate_result_hash=content_digest({"candidate": "aligned-research-champion"}),
        candidate_data_batch_hash=scorecards[1].data_batch_hash,
        candidate_universe_hash=scorecards[1].universe_hash,
        candidate_start_date=scorecards[1].start_date,
        candidate_end_date=scorecards[1].end_date,
        candidate_net_return=str(scorecards[1].net_return),
        scorecard=scorecards[1],
    )
    delisting_request = CanonicalSecuritiesSimulationRequest.create(
        baseline=baselines[0],
        data_batch_id=batch.batch_id,
        data_batch_hash=batch.canonical_hash,
        universe=tuple(item.instrument_id for item in instruments[:6]),
        start_date=start_date,
        end_date=sessions[-1].session_date,
        cutoff=cutoff,
    )
    delisting_proof = run_apex_research_portfolio(
        request=delisting_request,
        baseline=baselines[0],
        bars=bars,
        corporate_actions=actions,
    )
    reports = repository.reports()
    return {
        "warnings": (
            CANONICAL_BASELINE_WARNING,
            BASELINE_RESEARCH_WARNING,
            EXTERNAL_SIMULATOR_WARNING,
        ),
        "baseline_families": tuple(item.family.value for item in baselines),
        "baseline_count": len(baselines),
        "apex_research_completed": sum(item.state.value == "COMPLETED" for item in apex_runs),
        "vectorbt_completed": sum(item.state.value == "COMPLETED" for item in vectorbt_runs),
        "vectorbt_version": VectorbtSecuritiesAdapter(output_root).availability()[1],
        "vectorbt_artifacts": tuple(item.managed_result_artifact for item in vectorbt_runs),
        "lean": {
            "availability": LeanCanonicalSecuritiesAdapter().availability(),
            "run_state": lean_run.state.value,
            "executed": False,
        },
        "fixture_classifications": (
            conformant.classification.value,
            explained.classification.value,
            material.classification.value,
            unavailable.classification.value,
        ),
        "vectorbt_classifications": tuple(item.classification.value for item in vectorbt_reports),
        "three_layer_counts": {
            "signal_disagreements": material.direction_disagreements
            + material.target_weight_disagreements,
            "transaction_disagreements": material.transaction_disagreements,
            "result_return_difference": material.return_difference,
        },
        "canonical_request_mismatch": request_mismatch.classification.value,
        "idempotent_replay": replay == apex_runs[0],
        "conflicting_replay_rejected": idempotency_conflict_rejected,
        "scorecard_count": len(scorecards),
        "scorecards": tuple(scorecards),
        "strategy_lab_comparison": strategy_comparison,
        "corporate_action_count": sum(item.corporate_action_count for item in scorecards),
        "delisting_fail_closed": (
            "DELISTING_TERMINAL_VALUE_REQUIRED" in delisting_proof.reason_codes
        ),
        "trial_ledger": research_trial_summary(repository.runs(), baselines, reports),
        "managed_data": {
            "artifact": batch.canonical_artifact,
            "instrument_count": batch.instrument_count,
            "session_count": batch.session_count,
            "bar_count": batch.bar_count,
            "corporate_action_count": batch.corporate_action_count,
            "intervals": ("ONE_DAY",),
            "adjustment_states": (
                "RAW",
                "SPLIT_ADJUSTED",
                "TOTAL_RETURN_ADJUSTED",
            ),
            "start": batch.first_period_start,
            "end": batch.last_period_end,
        },
        "authority": {
            "provider_calls": 0,
            "broker_calls": 0,
            "new_strategy_authority": 0,
            "new_paper_trials": 0,
            "live_authority": 0,
        },
    }


def _fixture_runs(
    *,
    repository: InMemorySecuritiesBaselineRepository,
    baseline: CanonicalSecuritiesBaseline,
    request: CanonicalSecuritiesSimulationRequest,
    bars: tuple[SecuritiesBarEvidence, ...],
    actions: tuple[SecuritiesCorporateActionEvidence, ...],
    now: datetime,
) -> tuple[SecuritiesSimulationRun, ...]:
    retained = []
    for difference in ("EXACT", "EXPLAINED", "MATERIAL"):
        adapter = DeterministicConformanceFixtureAdapter(difference=difference)
        result = adapter.run(
            request=request,
            baseline=baseline,
            bars=bars,
            corporate_actions=actions,
        )
        run = simulation_run(
            request=request,
            result=result,
            idempotency_key=f"baseline-demo-fixture-{difference.lower()}",
            started_at=now,
            completed_at=now,
        )
        repository.save_run(request=request, run=run)
        retained.append(run)
    return tuple(retained)


def securities_simulator_status() -> dict[str, object]:
    output_root = Path(".local") / "securities" / "simulator-results"
    simulators = (
        ApexResearchSimulatorAdapter(),
        ApexPaperSimulatorAdapter(),
        VectorbtSecuritiesAdapter(output_root),
        LeanCanonicalSecuritiesAdapter(),
        DeterministicConformanceFixtureAdapter(),
    )
    return {
        "warning": EXTERNAL_SIMULATOR_WARNING,
        "simulators": simulator_catalog(simulators),
        "fixture_adapter": "DETERMINISTIC_FIXTURE_NOT_EXTERNAL_ENGINE",
        "provider_calls": 0,
        "broker_calls": 0,
    }
