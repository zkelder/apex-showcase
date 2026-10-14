from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex.adapters.securities_baseline_simulators import (
    ApexResearchSimulatorAdapter,
    DeterministicConformanceFixtureAdapter,
)
from apex.adapters.securities_baselines_postgres import (
    PostgresSecuritiesBaselineRepository,
)
from apex.adapters.securities_synthetic import synthetic_securities_dataset
from apex.application.securities_baselines import (
    CompareStrategyLabWithCanonicalBaseline,
    SecuritiesBaselineIdempotencyConflict,
)
from apex.domain.securities_baselines import (
    CanonicalSecuritiesSimulationRequest,
    SecuritiesConformanceMaterialityPolicy,
    build_baseline_scorecard,
    canonical_securities_baselines,
    compare_simulator_runs,
    simulation_run,
)
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
REVISION_0026 = "0026_securities_paper_execution"
REVISION_0027 = "0027_securities_baselines"
REVISION_0028 = "0028_research_overfitting"
REVISION_0029 = "0029_candidate_generation"
REVISION_0030 = "0030_batch_learning"
REVISION_0031 = "0118_evidence_gap_recovery"
TABLES = {
    "canonical_securities_baseline",
    "securities_simulation_run",
    "securities_simulator_conformance_report",
    "securities_baseline_scorecard",
    "strategy_lab_baseline_comparison",
}


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _revision(connection: sa.Connection) -> str:
    return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _evidence() -> tuple[object, ...]:
    instruments, sessions, bars, actions = synthetic_securities_dataset()
    baseline = canonical_securities_baselines(NOW)[1]
    universe = tuple(item.instrument_id for item in instruments[:4])
    request = CanonicalSecuritiesSimulationRequest.create(
        baseline=baseline,
        data_batch_id="batch-baseline-persistence",
        data_batch_hash=content_digest({"batch": "baseline-persistence"}),
        universe=universe,
        start_date=sessions[0].session_date,
        end_date=sessions[-2].session_date,
        cutoff=sessions[-1].closes_at + timedelta(days=1),
    )
    return baseline, request, bars, actions


def test_0027_empty_downgrade_and_reupgrade(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        assert _revision(connection) == REVISION_0031
        command.downgrade(_config(connection), REVISION_0028)
        connection.commit()
        assert _revision(connection) == REVISION_0028
        command.downgrade(_config(connection), REVISION_0027)
        connection.commit()
        assert _revision(connection) == REVISION_0027
        command.downgrade(_config(connection), REVISION_0026)
        connection.commit()
        assert _revision(connection) == REVISION_0026
        assert TABLES.isdisjoint(inspect(connection).get_table_names())
        command.upgrade(_config(connection), REVISION_0027)
        connection.commit()
        assert _revision(connection) == REVISION_0027
        assert TABLES.issubset(inspect(connection).get_table_names())
        command.upgrade(_config(connection), REVISION_0028)
        connection.commit()
        assert _revision(connection) == REVISION_0028
        command.upgrade(_config(connection), REVISION_0029)
        connection.commit()
        assert _revision(connection) == REVISION_0029


def test_postgres_repository_is_idempotent_exact_and_restart_safe(
    postgres_engine: Engine,
) -> None:
    baseline, request, bars, actions = _evidence()
    repository = PostgresSecuritiesBaselineRepository(postgres_engine)
    repository.save_baseline(baseline)
    repository.save_baseline(baseline)
    result = ApexResearchSimulatorAdapter().run(
        request=request,
        baseline=baseline,
        bars=bars,
        corporate_actions=actions,
    )
    run = simulation_run(
        request=request,
        result=result,
        idempotency_key="baseline-persistence-run",
        started_at=NOW,
        completed_at=NOW,
    )
    repository.save_run(request=request, run=run)
    repository.save_run(request=request, run=run)

    restarted = PostgresSecuritiesBaselineRepository(postgres_engine)
    assert restarted.baseline(baseline.baseline_id) == baseline
    assert restarted.find_run_by_idempotency_key(run.idempotency_key) == run
    assert restarted.runs() == (run,)

    fixture_result = DeterministicConformanceFixtureAdapter().run(
        request=request,
        baseline=baseline,
        bars=bars,
        corporate_actions=actions,
    )
    fixture = simulation_run(
        request=request,
        result=fixture_result,
        idempotency_key="baseline-persistence-fixture",
        started_at=NOW,
        completed_at=NOW,
    )
    restarted.save_run(request=request, run=fixture)
    report = compare_simulator_runs(
        reference=run,
        comparison=fixture,
        policy=SecuritiesConformanceMaterialityPolicy.v1(),
    )
    restarted.save_report(report)
    restarted.save_report(report)
    assert restarted.reports() == (report,)
    detail = restarted.conformance_detail(report.report_id)
    assert detail["report"] == report
    assert detail["reference"]["request"]["canonical_hash"] == request.canonical_hash
    assert detail["reference"]["result"]["signals"]

    scorecard = build_baseline_scorecard(
        baseline=baseline,
        request=request,
        run=run,
        conformance_state=report.classification,
    )
    restarted.save_scorecard(scorecard)
    restarted.save_scorecard(scorecard)
    assert restarted.scorecards() == (scorecard,)
    comparison = CompareStrategyLabWithCanonicalBaseline(restarted).execute(
        candidate_id="strategy-lab-candidate-aligned",
        candidate_result_hash=content_digest({"candidate": "aligned"}),
        candidate_data_batch_hash=request.data_batch_hash,
        candidate_universe_hash=request.universe_hash,
        candidate_start_date=request.start_date,
        candidate_end_date=request.end_date,
        candidate_net_return="0.1",
        scorecard=scorecard,
    )
    assert comparison.aligned is True
    assert comparison.integrity_blocked is False

    with postgres_engine.begin() as connection:
        numeric = {
            (table_name, str(column["name"]))
            for table_name in TABLES
            for column in inspect(connection).get_columns(table_name)
            if isinstance(column["type"], sa.Numeric)
        }
    assert ("securities_simulation_run", "starting_capital") in numeric
    assert ("securities_baseline_scorecard", "net_return") in numeric


def test_0027_retained_evidence_blocks_downgrade_before_ddl(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.connect() as connection:
        assert _revision(connection) == REVISION_0031
        command.downgrade(_config(connection), REVISION_0028)
        connection.commit()
        assert _revision(connection) == REVISION_0028
        command.downgrade(_config(connection), REVISION_0027)
        connection.commit()
    baseline = canonical_securities_baselines(NOW)[0]
    repository = PostgresSecuritiesBaselineRepository(postgres_engine)
    repository.save_baseline(baseline)
    before_tables = set(inspect(postgres_engine).get_table_names())

    with postgres_engine.connect() as connection:
        with pytest.raises(
            sa.exc.ProgrammingError,
            match="cannot downgrade 0027 with retained canonical Securities baseline",
        ):
            command.downgrade(_config(connection), REVISION_0026)
        connection.rollback()
        assert _revision(connection) == REVISION_0027
        assert set(inspect(connection).get_table_names()) == before_tables
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(sa.table("canonical_securities_baseline"))
            )
            == 1
        )
        command.upgrade(_config(connection), REVISION_0028)
        connection.commit()
        assert _revision(connection) == REVISION_0028
        command.upgrade(_config(connection), REVISION_0029)
        connection.commit()
        assert _revision(connection) == REVISION_0029


def test_immutable_rows_and_conflicting_replay_are_rejected(
    postgres_engine: Engine,
) -> None:
    baseline, request, bars, actions = _evidence()
    repository = PostgresSecuritiesBaselineRepository(postgres_engine)
    repository.save_baseline(baseline)
    result = ApexResearchSimulatorAdapter().run(
        request=request,
        baseline=baseline,
        bars=bars,
        corporate_actions=actions,
    )
    run = simulation_run(
        request=request,
        result=result,
        idempotency_key="immutable-baseline-run",
        started_at=NOW,
        completed_at=NOW,
    )
    repository.save_run(request=request, run=run)

    with (
        pytest.raises(sa.exc.ProgrammingError, match="immutable Apex row"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text("UPDATE securities_simulation_run SET signal_count = signal_count + 1")
        )
    conflicting_request = CanonicalSecuritiesSimulationRequest.create(
        baseline=baseline,
        data_batch_id=request.data_batch_id,
        data_batch_hash=request.data_batch_hash,
        universe=request.universe,
        start_date=request.start_date,
        end_date=request.end_date,
        cutoff=request.cutoff,
        starting_capital="20000",
    )
    with pytest.raises(SecuritiesBaselineIdempotencyConflict):
        repository.save_run(request=conflicting_request, run=run)
