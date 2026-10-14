from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex.adapters.research_integrity_artifacts import (
    LocalResearchReturnArtifactStore,
)
from apex.adapters.research_integrity_postgres import (
    PostgresResearchIntegrityRepository,
)
from apex.application.research_integrity import (
    OpenResearchExperimentFamily,
    ResearchIdempotencyConflict,
    ResearchIntegrityService,
    ResearchTrialLedger,
    ResearchTrialSourceType,
    ResearchTrialState,
)
from apex.domain.research_integrity import ResearchFrequency, ReturnObservation
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
REVISION_0027 = "0027_securities_baselines"
REVISION_0028 = "0028_research_overfitting"
REVISION_0029 = "0029_candidate_generation"
REVISION_0030 = "0030_batch_learning"
REVISION_0031 = "0118_evidence_gap_recovery"
TABLES = {
    "research_experiment_family",
    "research_trial_attempt",
    "research_return_series_artifact",
    "research_metric_snapshot",
    "research_selection_event",
    "research_cross_validation_partition",
    "research_multiple_testing_evaluation",
}


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _revision(connection: sa.Connection) -> str:
    return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _family(repository: PostgresResearchIntegrityRepository):
    return OpenResearchExperimentFamily(repository).execute(
        name="Persistent bounded family",
        vertical="SECURITIES",
        research_family="CANONICAL_SECURITIES_BASELINES",
        research_question="Does a predeclared baseline remain stable after costs?",
        target_label_identity="daily-total-return-v1",
        benchmark_identities=("CASH",),
        data_scope_identities=("persistent-fixture",),
        data_scope_hashes=("a" * 64,),
        universe_hash="b" * 64,
        period_start="2025-01-01",
        period_end="2025-06-30",
        cutoff_at=NOW,
        feature_schema_hashes=("c" * 64,),
        permitted_families=("MODEST", "NULL", "LUCKY"),
        parameter_space={"lookback": (5, 10), "threshold": ("0.01", "0.02")},
        maximum_trials=6,
        maximum_feature_variants=2,
        maximum_model_variants=3,
        maximum_simulator_variants=2,
        evaluation_policy_id="RESEARCH_SELECTION_INTEGRITY_POLICY_V1",
        evaluation_policy_payload={"minimum_psr": "0.95", "maximum_pbo": "0.25"},
        cost_execution_assumptions={"commission_bps": "1"},
        frequency=ResearchFrequency.DAILY,
        operator_action_id="operator-persistent-family",
        operator_reason="Focused persistence proof",
        idempotency_key="persistent-family",
        created_at=NOW,
    )


def _observations(mean: Decimal, phase: int) -> tuple[ReturnObservation, ...]:
    pattern = (-3, -2, -1, 1, 2, 3, -1, 1)
    return tuple(
        ReturnObservation(
            period_id=f"2025-{index + 1:03}",
            value=mean + Decimal("0.001") * Decimal(pattern[(index + phase) % len(pattern)]),
            group_id=f"group-{index // 20:02}",
            instrument_id=f"instrument-{index % 4}",
        )
        for index in range(120)
    )


def test_0028_empty_downgrade_and_reupgrade(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        assert _revision(connection) == REVISION_0031
        command.downgrade(_config(connection), REVISION_0028)
        connection.commit()
        assert _revision(connection) == REVISION_0028
        command.downgrade(_config(connection), REVISION_0027)
        connection.commit()
        assert _revision(connection) == REVISION_0027
        assert TABLES.isdisjoint(inspect(connection).get_table_names())
        command.upgrade(_config(connection), REVISION_0028)
        connection.commit()
        assert _revision(connection) == REVISION_0028
        assert TABLES.issubset(inspect(connection).get_table_names())
        command.upgrade(_config(connection), REVISION_0029)
        connection.commit()
        assert _revision(connection) == REVISION_0029


def test_repository_retains_every_trial_restart_artifact_and_metrics(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    repository = PostgresResearchIntegrityRepository(postgres_engine)
    family = _family(repository)
    ledger = ResearchTrialLedger(repository)
    artifacts = LocalResearchReturnArtifactStore(tmp_path / "return-series")
    service = ResearchIntegrityService(repository, artifacts)
    completed_ids: list[str] = []
    for ordinal, state in enumerate(
        (
            ResearchTrialState.COMPLETED,
            ResearchTrialState.COMPLETED,
            ResearchTrialState.COMPLETED,
            ResearchTrialState.FAILED,
            ResearchTrialState.REJECTED_INTEGRITY,
        ),
        start=1,
    ):
        trial = ledger.reserve(
            experiment_id=family.experiment_id,
            source_type=ResearchTrialSourceType.SECURITIES_SIMULATION,
            source_identity=f"persistent-source-{ordinal}",
            source_hash=content_digest({"source": ordinal}),
            strategy_model_family=("MODEST", "NULL", "LUCKY", "NULL", "NULL")[ordinal - 1],
            configuration={"variant": ordinal},
            feature_set_hash="c" * 64,
            dataset_hash="a" * 64,
            simulator_identity="APEX_RESEARCH",
            simulator_version="1",
            execution_cost_policy_hash="d" * 64,
            random_seed=ordinal,
            idempotency_key=f"persistent-trial-{ordinal}",
            started_at=NOW + timedelta(seconds=ordinal),
            selected_for_comparison=state is ResearchTrialState.COMPLETED,
        )
        terminal = ledger.finish(
            trial.trial_id,
            state=state,
            completed_at=NOW + timedelta(minutes=ordinal),
            result_identity=f"result-{ordinal}" if state is ResearchTrialState.COMPLETED else None,
            result_hash=content_digest({"result": ordinal})
            if state is ResearchTrialState.COMPLETED
            else None,
            failure_reason=None
            if state is ResearchTrialState.COMPLETED
            else (
                "MATERIAL_SIMULATOR_MISMATCH"
                if state is ResearchTrialState.REJECTED_INTEGRITY
                else "LEAN_RUNTIME_UNAVAILABLE"
            ),
            integrity_blocked=state is ResearchTrialState.REJECTED_INTEGRITY,
        )
        if state is ResearchTrialState.COMPLETED:
            completed_ids.append(terminal.trial_id)
            evidence = _observations(
                Decimal("0.0006") - Decimal(ordinal) * Decimal("0.0001"),
                ordinal,
            )
            service.retain_return_evidence(
                trial_id=terminal.trial_id,
                gross_observations=evidence,
                net_observations=evidence,
                frequency=ResearchFrequency.DAILY,
            )

    restarted = PostgresResearchIntegrityRepository(postgres_engine)
    assert restarted.family(family.experiment_id) == family
    retained = restarted.trials(family.experiment_id)
    assert tuple(item.ordinal for item in retained) == (1, 2, 3, 4, 5)
    assert retained[3].failure_reason == "LEAN_RUNTIME_UNAVAILABLE"
    assert retained[4].integrity_blocked is True
    assert restarted.metric(completed_ids[0]) is not None
    assert len(restarted.artifacts(completed_ids[0])) == 2
    evaluation = ResearchIntegrityService(restarted, artifacts).evaluate(
        experiment_id=family.experiment_id,
        cutoff_at=NOW + timedelta(days=1),
    )
    assert evaluation.completed_trial_count == 3
    assert evaluation.failed_rejected_trial_count == 2

    replay = ledger.reserve(
        experiment_id=family.experiment_id,
        source_type=ResearchTrialSourceType.SECURITIES_SIMULATION,
        source_identity="persistent-source-1",
        source_hash=content_digest({"source": 1}),
        strategy_model_family="MODEST",
        configuration={"variant": 1},
        feature_set_hash="c" * 64,
        dataset_hash="a" * 64,
        simulator_identity="APEX_RESEARCH",
        simulator_version="1",
        execution_cost_policy_hash="d" * 64,
        random_seed=1,
        idempotency_key="persistent-trial-1",
        started_at=NOW + timedelta(seconds=1),
    )
    assert replay.trial_id == completed_ids[0]
    with pytest.raises(ResearchIdempotencyConflict, match="TRIAL_IDEMPOTENCY_CONFLICT"):
        ledger.reserve(
            experiment_id=family.experiment_id,
            source_type=ResearchTrialSourceType.SECURITIES_SIMULATION,
            source_identity="persistent-source-1",
            source_hash=content_digest({"source": 1}),
            strategy_model_family="MODEST",
            configuration={"variant": 999},
            feature_set_hash="c" * 64,
            dataset_hash="a" * 64,
            simulator_identity="APEX_RESEARCH",
            simulator_version="1",
            execution_cost_policy_hash="d" * 64,
            random_seed=1,
            idempotency_key="persistent-trial-1",
            started_at=NOW + timedelta(seconds=1),
        )


def test_family_scope_metric_and_selection_evidence_are_immutable(
    postgres_engine: Engine,
) -> None:
    repository = PostgresResearchIntegrityRepository(postgres_engine)
    family = _family(repository)
    with (
        pytest.raises(
            sa.exc.ProgrammingError,
            match="research experiment scope and budget are immutable",
        ),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE research_experiment_family "
                "SET maximum_trials = maximum_trials + 1 "
                "WHERE experiment_id = :experiment_id"
            ),
            {"experiment_id": family.experiment_id},
        )


def test_exact_numeric_columns_and_static_schema(postgres_engine: Engine) -> None:
    inspector = inspect(postgres_engine)
    numeric = {
        (table, str(column["name"]))
        for table in TABLES
        for column in inspector.get_columns(table)
        if isinstance(column["type"], sa.Numeric)
    }
    assert ("research_metric_snapshot", "sharpe_ratio") in numeric
    assert ("research_metric_snapshot", "effective_sample_size") in numeric
    assert (
        "research_multiple_testing_evaluation",
        "probability_backtest_overfitting",
    ) in numeric
    assert (
        "research_cross_validation_partition",
        "selected_test_rank_percentile",
    ) in numeric


def test_0028_retained_evidence_blocks_downgrade_before_ddl(
    postgres_engine: Engine,
) -> None:
    repository = PostgresResearchIntegrityRepository(postgres_engine)
    family = _family(repository)
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), REVISION_0028)
        connection.commit()
        before_tables = set(inspect(connection).get_table_names())
        with pytest.raises(
            sa.exc.ProgrammingError,
            match="cannot downgrade 0028 with retained research experiment",
        ):
            command.downgrade(_config(connection), REVISION_0027)
        connection.rollback()
        assert _revision(connection) == REVISION_0028
        assert set(inspect(connection).get_table_names()) == before_tables
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM research_experiment_family "
                    "WHERE experiment_id = :experiment_id"
                ),
                {"experiment_id": family.experiment_id},
            )
            == 1
        )
        command.upgrade(_config(connection), REVISION_0029)
        connection.commit()
        assert _revision(connection) == REVISION_0029
