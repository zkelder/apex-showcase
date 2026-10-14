"""PostgreSQL persistence proofs for the point-in-time historical replay
engine: migration 0078 round-trip through fresh repository instances, a
genuine multi-step replay against the real ``securities_bar_evidence``
store, checkpoint/resume across repository instances, guarded mutability
(run cursor/completion, evaluation outcome progression), immutable-evidence
enforcement, historical experiment/hypothesis lineage, and downgrade
protection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.historical_replay_postgres import (
    HISTORICAL_REPLAY_TABLES,
    PostgresHistoricalReplayRepository,
)
from apex.adapters.research_evidence_postgres import PostgresResearchEvidenceRepository
from apex.adapters.securities_postgres import PostgresSecuritiesRepository
from apex.application.historical_market_data import HistoricalPointInTimeReplay
from apex.application.historical_replay import (
    ConstructReplayExperiment,
    ConstructReplayExperimentCommand,
    CreateReplayRun,
    CreateReplayRunCommand,
    HistoricalReplayEngine,
    ResumeReplayRun,
    StartReplayRun,
    StepReplayRunCommand,
    build_historical_research_summary,
)
from apex.application.research_evidence import RegisterResearchHypothesis
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalClock,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
)
from apex.domain.historical_replay import ReplayRunCompletionState
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesAssetClass,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesCalendarSession,
    SecuritiesDataBatchState,
    SecuritiesInstrument,
    SecuritiesMarketDataBatch,
)
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _dates(start: str, count: int) -> list[str]:
    from datetime import date as _date

    start_date = _date.fromisoformat(start)
    return [(start_date + timedelta(days=index)).isoformat() for index in range(count)]


def _session(day: str) -> SecuritiesCalendarSession:
    return SecuritiesCalendarSession.create(
        calendar_identity="XNYS",
        calendar_version="test-v1",
        timezone="America/New_York",
        session_date=day,
        opens_at=datetime.fromisoformat(f"{day}T14:30:00+00:00"),
        closes_at=datetime.fromisoformat(f"{day}T21:00:00+00:00"),
        is_early_close=False,
        source_identity="TEST_GENUINE_TABLE_CALENDAR",
    )


def _bar(instrument_id: str, day: str, close: str) -> SecuritiesBarEvidence:
    period_start = datetime.fromisoformat(f"{day}T14:30:00+00:00")
    period_end = datetime.fromisoformat(f"{day}T21:00:00+00:00")
    return SecuritiesBarEvidence(
        instrument_id=instrument_id,
        interval=SecuritiesBarInterval.ONE_DAY,
        adjustment_state=SecuritiesAdjustmentState.RAW,
        period_start=period_start,
        period_end=period_end,
        available_at=period_end + timedelta(minutes=30),
        open=close,
        high=str(Decimal(close) + 1),
        low=str(Decimal(close) - 1),
        close=close,
        volume="1000",
        source_identity="TEST_GENUINE_TABLE",
    )


def _build_snapshot(
    *, postgres_engine: Engine, symbols: tuple[str, ...], day_count: int, idempotency_key: str
) -> tuple[PostgresHistoricalMarketDataRepository, HistoricalDatasetSnapshot]:
    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    securities_repository = PostgresSecuritiesRepository(postgres_engine)
    dates = _dates("2021-01-01", day_count)
    policy = HistoricalCapturePolicy.create(
        provider="FIXTURE_PROVIDER",
        start_date=dates[0],
        end_date=dates[-1],
        chunk_size=100,
        maximum_instruments=100,
        maximum_requests=100,
        maximum_bytes=50_000_000,
    )
    universe = tuple(
        HistoricalCaptureUniverseMember.create(
            stable_security_id=f"stable-{symbol}",
            symbol_at_capture=symbol,
            asset_class="COMMON_STOCK",
        )
        for symbol in symbols
    )
    plan = HistoricalCapturePlan.draft(
        policy=policy,
        universe=universe,
        evidence_class="FIXTURE",
        operator_action_id="op-fixture",
        idempotency_key=idempotency_key,
        created_at=NOW,
    )
    historical_repository.save_plan(plan)
    instruments = tuple(
        SecuritiesInstrument(
            instrument_id=member.instrument_id,
            symbol=member.symbol_at_capture,
            asset_class=SecuritiesAssetClass.COMMON_STOCK,
            exchange_mic="XNYS",
            currency="USD",
            timezone="America/New_York",
            valid_from="0001-01-01",
            valid_to=None,
            source_identity="TEST_GENUINE_TABLE",
        )
        for member in universe
    )
    sessions = tuple(_session(day) for day in dates)
    bars: list[SecuritiesBarEvidence] = []
    for index_offset, member in enumerate(universe):
        for index, day in enumerate(dates):
            close = str(
                Decimal("100") + Decimal(index) * Decimal("0.1") + Decimal(index_offset * 5)
            )
            bars.append(_bar(member.instrument_id, day, close))
    batch = SecuritiesMarketDataBatch(
        batch_id=f"batch-{plan.plan_id}",
        dataset_name="fixture",
        state=SecuritiesDataBatchState.READY,
        imported_at=NOW,
        source_artifact="x",
        source_sha256="a" * 64,
        source_byte_size=1,
        canonical_artifact="y",
        canonical_sha256="b" * 64,
        canonical_byte_size=1,
        instrument_count=len(instruments),
        session_count=len(sessions),
        bar_count=len(bars),
        corporate_action_count=0,
        first_period_start=datetime.fromisoformat(f"{dates[0]}T14:30:00+00:00"),
        last_period_end=datetime.fromisoformat(f"{dates[-1]}T21:00:00+00:00"),
        idempotency_key=f"batch-{idempotency_key}",
        warnings=(),
        canonical_hash=content_digest(f"batch-{idempotency_key}"),
    )
    securities_repository.save_dataset(
        batch=batch, instruments=instruments, sessions=sessions, bars=bars, corporate_actions=()
    )
    snapshot = HistoricalDatasetSnapshot.create(
        plan=plan,
        source_batch_ids=(batch.batch_id,),
        source_operation_ids=("op-1",),
        observation_count=len(bars),
        completeness_state=HistoricalDatasetCompletenessState.COMPLETE,
        earliest_observation_date=dates[0],
        latest_observation_date=dates[-1],
        created_at=NOW,
    )
    historical_repository.save_snapshot(snapshot)
    return historical_repository, snapshot


def test_historical_replay_tables_exist_at_head(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(HISTORICAL_REPLAY_TABLES)


def test_run_round_trips_exactly_through_a_fresh_repository_instance(
    postgres_engine: Engine,
) -> None:
    _historical_repository, snapshot = _build_snapshot(
        postgres_engine=postgres_engine, symbols=("AAA",), day_count=10, idempotency_key="rt-plan"
    )
    replay_repository = PostgresHistoricalReplayRepository(postgres_engine)
    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    run = CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    ).execute(
        CreateReplayRunCommand(
            dataset_snapshot_id=snapshot.snapshot_id,
            start_date=snapshot.start_date,
            end_date=snapshot.end_date,
            calendar_identity=None,
            hypothesis_version_id=None,
            operator_action_id="op-1",
            idempotency_key="rt-run",
            created_at=NOW,
        )
    )
    fresh = PostgresHistoricalReplayRepository(postgres_engine)
    reloaded = fresh.get_run(run.replay_run_id)
    assert reloaded == run
    assert reloaded.completion_state is ReplayRunCompletionState.NOT_STARTED

    engine = HistoricalReplayEngine(
        repository=replay_repository,
        historical_data_repository=historical_repository,
        evidence=PostgresSecuritiesRepository(postgres_engine),
    )
    completed = StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=10, requested_at=NOW)
    )
    assert completed.completion_state is ReplayRunCompletionState.COMPLETE
    fresh_after = PostgresHistoricalReplayRepository(postgres_engine)
    assert (
        fresh_after.get_run(run.replay_run_id).completion_state is ReplayRunCompletionState.COMPLETE
    )
    frames = fresh_after.list_frames(run.replay_run_id, limit=1000)
    assert len(frames) == 10
    checkpoints = fresh_after.list_checkpoints(run.replay_run_id)
    assert len(checkpoints) == 10
    evaluations = fresh_after.list_evaluations(run.replay_run_id, limit=1000)
    assert evaluations
    for frame in frames:
        assert fresh_after.frame_by_id(frame.frame_id) == frame


def test_resume_across_fresh_repository_instances_creates_no_duplicates(
    postgres_engine: Engine,
) -> None:
    _historical_repository, snapshot = _build_snapshot(
        postgres_engine=postgres_engine,
        symbols=("AAA", "BBB"),
        day_count=40,
        idempotency_key="resume-plan",
    )
    replay_repository = PostgresHistoricalReplayRepository(postgres_engine)
    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    run = CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    ).execute(
        CreateReplayRunCommand(
            dataset_snapshot_id=snapshot.snapshot_id,
            start_date=snapshot.start_date,
            end_date=snapshot.end_date,
            calendar_identity=None,
            hypothesis_version_id=None,
            operator_action_id="op-1",
            idempotency_key="resume-run",
            created_at=NOW,
        )
    )
    evidence = PostgresSecuritiesRepository(postgres_engine)
    engine_1 = HistoricalReplayEngine(
        repository=replay_repository,
        historical_data_repository=historical_repository,
        evidence=evidence,
    )
    StartReplayRun(repository=replay_repository, engine=engine_1).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=15, requested_at=NOW)
    )
    # A brand-new repository/engine pair (as a fresh process resuming later would use).
    fresh_replay_repository = PostgresHistoricalReplayRepository(postgres_engine)
    fresh_historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    engine_2 = HistoricalReplayEngine(
        repository=fresh_replay_repository,
        historical_data_repository=fresh_historical_repository,
        evidence=PostgresSecuritiesRepository(postgres_engine),
    )
    completed = ResumeReplayRun(repository=fresh_replay_repository, engine=engine_2).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=100, requested_at=NOW)
    )
    assert completed.completion_state is ReplayRunCompletionState.COMPLETE
    frames = fresh_replay_repository.list_frames(run.replay_run_id, limit=1000)
    assert len(frames) == 40
    assert len({item.frame_id for item in frames}) == 40
    checkpoints = fresh_replay_repository.list_checkpoints(run.replay_run_id)
    assert len(checkpoints) == 40


def test_run_forbids_delete_and_terminal_state_regression(postgres_engine: Engine) -> None:
    _historical_repository, snapshot = _build_snapshot(
        postgres_engine=postgres_engine, symbols=("AAA",), day_count=5, idempotency_key="guard-plan"
    )
    replay_repository = PostgresHistoricalReplayRepository(postgres_engine)
    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    run = CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    ).execute(
        CreateReplayRunCommand(
            dataset_snapshot_id=snapshot.snapshot_id,
            start_date=snapshot.start_date,
            end_date=snapshot.end_date,
            calendar_identity=None,
            hypothesis_version_id=None,
            operator_action_id="op-1",
            idempotency_key="guard-run",
            created_at=NOW,
        )
    )
    engine = HistoricalReplayEngine(
        repository=replay_repository,
        historical_data_repository=historical_repository,
        evidence=PostgresSecuritiesRepository(postgres_engine),
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=5, requested_at=NOW)
    )
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="terminal"):
            connection.execute(
                sa.text(
                    "UPDATE historical_replay_run SET completion_state = 'IN_PROGRESS' "
                    "WHERE replay_run_id = :id"
                ),
                {"id": run.replay_run_id},
            )
        connection.rollback()
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="cannot be deleted"):
            connection.execute(
                sa.text("DELETE FROM historical_replay_run WHERE replay_run_id = :id"),
                {"id": run.replay_run_id},
            )
        connection.rollback()


def test_feature_frame_and_checkpoint_are_immutable(postgres_engine: Engine) -> None:
    _historical_repository, snapshot = _build_snapshot(
        postgres_engine=postgres_engine,
        symbols=("AAA",),
        day_count=5,
        idempotency_key="immutable-plan",
    )
    replay_repository = PostgresHistoricalReplayRepository(postgres_engine)
    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    run = CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    ).execute(
        CreateReplayRunCommand(
            dataset_snapshot_id=snapshot.snapshot_id,
            start_date=snapshot.start_date,
            end_date=snapshot.end_date,
            calendar_identity=None,
            hypothesis_version_id=None,
            operator_action_id="op-1",
            idempotency_key="immutable-run",
            created_at=NOW,
        )
    )
    engine = HistoricalReplayEngine(
        repository=replay_repository,
        historical_data_repository=historical_repository,
        evidence=PostgresSecuritiesRepository(postgres_engine),
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=5, requested_at=NOW)
    )
    frame = replay_repository.list_frames(run.replay_run_id, limit=1)[0]
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="immutable"):
            connection.execute(
                sa.text(
                    "UPDATE historical_replay_feature_frame SET session_date = '1999-01-01' "
                    "WHERE frame_id = :id"
                ),
                {"id": frame.frame_id},
            )
        connection.rollback()
    checkpoint = replay_repository.list_checkpoints(run.replay_run_id)[0]
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="immutable"):
            connection.execute(
                sa.text("DELETE FROM historical_replay_checkpoint WHERE checkpoint_id = :id"),
                {"id": checkpoint.checkpoint_id},
            )
        connection.rollback()


def test_evaluation_completed_outcome_cannot_be_altered(postgres_engine: Engine) -> None:
    _historical_repository, snapshot = _build_snapshot(
        postgres_engine=postgres_engine,
        symbols=("AAA",),
        day_count=60,
        idempotency_key="outcome-plan",
    )
    replay_repository = PostgresHistoricalReplayRepository(postgres_engine)
    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    run = CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    ).execute(
        CreateReplayRunCommand(
            dataset_snapshot_id=snapshot.snapshot_id,
            start_date=snapshot.start_date,
            end_date=snapshot.end_date,
            calendar_identity=None,
            hypothesis_version_id=None,
            operator_action_id="op-1",
            idempotency_key="outcome-run",
            created_at=NOW,
        )
    )
    engine = HistoricalReplayEngine(
        repository=replay_repository,
        historical_data_repository=historical_repository,
        evidence=PostgresSecuritiesRepository(postgres_engine),
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(
            replay_run_id=run.replay_run_id,
            maximum_steps=60,
            requested_at=NOW,
            benchmark_stable_security_id="stable-AAA",
        )
    )
    completed_evaluations = [
        item
        for item in replay_repository.list_evaluations(run.replay_run_id, limit=1000)
        if item.outcome is not None and item.outcome.completeness.value == "COMPLETE"
    ]
    assert completed_evaluations
    target = completed_evaluations[0]
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="cannot be altered"):
            connection.execute(
                sa.text(
                    "UPDATE historical_replay_evaluation SET outcome_completeness = 'PARTIAL' "
                    "WHERE evaluation_id = :id"
                ),
                {"id": target.evaluation_id},
            )
        connection.rollback()


def test_run_dataset_snapshot_identity_cannot_be_changed_after_draft(
    postgres_engine: Engine,
) -> None:
    """The database itself -- not only the application layer's own
    pre-resume check -- forbids a replay run's bound dataset snapshot (or any
    other declared identity field) from being altered after the run is
    drafted, structurally ruling out the "changed dataset" hazard at the
    persistence layer."""

    from dataclasses import replace as _replace

    _historical_repository, snapshot = _build_snapshot(
        postgres_engine=postgres_engine,
        symbols=("AAA",),
        day_count=5,
        idempotency_key="identity-plan-a",
    )
    _historical_repository_b, snapshot_b = _build_snapshot(
        postgres_engine=postgres_engine,
        symbols=("AAA",),
        day_count=5,
        idempotency_key="identity-plan-b",
    )
    replay_repository = PostgresHistoricalReplayRepository(postgres_engine)
    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    run = CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    ).execute(
        CreateReplayRunCommand(
            dataset_snapshot_id=snapshot.snapshot_id,
            start_date=snapshot.start_date,
            end_date=snapshot.end_date,
            calendar_identity=None,
            hypothesis_version_id=None,
            operator_action_id="op-1",
            idempotency_key="identity-run",
            created_at=NOW,
        )
    )
    # snapshot_b is a genuine, valid, FK-satisfying snapshot -- so a rejection
    # here can only be the identity guard, not a foreign-key violation.
    retargeted = _replace(run, dataset_snapshot_id=snapshot_b.snapshot_id)
    with pytest.raises(sa.exc.DBAPIError, match="identity fields are immutable"):
        replay_repository.save_run(retargeted)


def test_experiment_lineage_persists_through_genuine_repositories(postgres_engine: Engine) -> None:
    _historical_repository, snapshot = _build_snapshot(
        postgres_engine=postgres_engine,
        symbols=("AAA", "BBB"),
        day_count=90,
        idempotency_key="lineage-plan",
    )
    replay_repository = PostgresHistoricalReplayRepository(postgres_engine)
    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    research_repository = PostgresResearchEvidenceRepository(postgres_engine)
    hypothesis = RegisterResearchHypothesis(research_repository).execute(
        hypothesis_id="pg-breadth-hypothesis",
        revision=1,
        question="Does elevated breadth precede positive subsequent returns?",
        motivation="Persistence proof of replay-to-experiment lineage.",
        actor="researcher-1",
        market_scope=("US_EQUITY",),
        created_at=NOW,
    )
    run = CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    ).execute(
        CreateReplayRunCommand(
            dataset_snapshot_id=snapshot.snapshot_id,
            start_date=snapshot.start_date,
            end_date=snapshot.end_date,
            calendar_identity=None,
            hypothesis_version_id=hypothesis.hypothesis_version_id,
            operator_action_id="op-1",
            idempotency_key="lineage-run",
            created_at=NOW,
        )
    )
    engine = HistoricalReplayEngine(
        repository=replay_repository,
        historical_data_repository=historical_repository,
        evidence=PostgresSecuritiesRepository(postgres_engine),
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(
            replay_run_id=run.replay_run_id,
            maximum_steps=90,
            requested_at=NOW,
            benchmark_stable_security_id="stable-AAA",
        )
    )
    link = ConstructReplayExperiment(
        repository=replay_repository,
        historical_data_repository=historical_repository,
        research_evidence=research_repository,
    ).execute(
        ConstructReplayExperimentCommand(
            replay_run_id=run.replay_run_id,
            code_revision="pg-test-revision",
            actor="researcher-1",
            decided_at=NOW,
        )
    )
    fresh_research_repository = PostgresResearchEvidenceRepository(postgres_engine)
    experiment_run = fresh_research_repository.run(link.research_experiment_run_id)
    assert experiment_run is not None
    assert experiment_run.hypothesis_version_id == hypothesis.hypothesis_version_id
    outcome = fresh_research_repository.outcome_for_run(experiment_run.run_id)
    assert outcome is not None
    assert outcome.decision.value == "DEFERRED"

    summary = build_historical_research_summary(
        repository=PostgresHistoricalReplayRepository(postgres_engine),
        replay_run_id=run.replay_run_id,
    )
    assert summary.evaluation_count > 0


def test_replay_seam_still_works_against_a_replay_bound_snapshot(postgres_engine: Engine) -> None:
    """Confirms the pre-existing ``HistoricalPointInTimeReplay`` seam (section
    13 of the prior milestone) is untouched and still usable alongside the
    new replay engine against the identical genuine snapshot."""

    historical_repository, snapshot = _build_snapshot(
        postgres_engine=postgres_engine, symbols=("AAA",), day_count=30, idempotency_key="seam-plan"
    )
    seam = HistoricalPointInTimeReplay(
        repository=historical_repository, evidence=PostgresSecuritiesRepository(postgres_engine)
    )
    as_of = datetime.fromisoformat("2021-01-30T21:30:00+00:00")
    value = seam.calculate(
        snapshot_id=snapshot.snapshot_id,
        stable_security_id="stable-AAA",
        clock=HistoricalClock(as_of=as_of),
        calculation="simple_return",
    )
    assert value is not None


def test_retained_replay_evidence_blocks_destructive_0078_downgrade(
    postgres_engine: Engine,
) -> None:
    _historical_repository, snapshot = _build_snapshot(
        postgres_engine=postgres_engine,
        symbols=("AAA",),
        day_count=5,
        idempotency_key="downgrade-plan",
    )
    replay_repository = PostgresHistoricalReplayRepository(postgres_engine)
    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    ).execute(
        CreateReplayRunCommand(
            dataset_snapshot_id=snapshot.snapshot_id,
            start_date=snapshot.start_date,
            end_date=snapshot.end_date,
            calendar_identity=None,
            hypothesis_version_id=None,
            operator_action_id="op-1",
            idempotency_key="downgrade-run",
            created_at=NOW,
        )
    )
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception, match="cannot downgrade 0078 with retained historical replay evidence"
        ):
            command.downgrade(_config(connection), "0077_historical_market_data")
        connection.rollback()
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0078_historical_replay"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")


def test_0078_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(HISTORICAL_REPLAY_TABLES)
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0077_historical_market_data")
    assert set(HISTORICAL_REPLAY_TABLES).isdisjoint(sa.inspect(postgres_engine).get_table_names())
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(HISTORICAL_REPLAY_TABLES)
