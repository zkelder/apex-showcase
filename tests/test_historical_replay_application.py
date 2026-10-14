"""Orchestration-level proofs for the point-in-time historical replay engine:
run creation, start/checkpoint/resume, exact determinism, conflict detection
(idempotency, changed dataset, changed policy), partial failure, bounded
result lists, outcome-isolation progression across steps, crypto cross-asset
integration, historical experiment/hypothesis lineage, holdout enforcement,
and a performance measurement -- all against in-memory fakes.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.historical_market_data import InMemoryHistoricalMarketDataRepository
from apex.application.historical_replay import (
    ConstructReplayExperiment,
    ConstructReplayExperimentCommand,
    CreateReplayRun,
    CreateReplayRunCommand,
    DeclareResearchPeriodPolicy,
    DeclareResearchPeriodPolicyCommand,
    HistoricalReplayApplicationError,
    HistoricalReplayConflict,
    HistoricalReplayEngine,
    HistoricalReplayNotFound,
    InMemoryHistoricalReplayRepository,
    ListEvaluationsForPeriod,
    ResumeReplayRun,
    StartReplayRun,
    StepReplayRunCommand,
    build_historical_research_summary,
)
from apex.application.research_evidence import (
    InMemoryResearchEvidenceRepository,
    RegisterResearchHypothesis,
)
from apex.domain.crypto_research import COINBASE_PROVIDER, CryptoDailyCandle
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
)
from apex.domain.historical_replay import (
    HoldoutViolationError,
    ReplayRunCompletionState,
    ResearchPeriodDeclaration,
    ResearchPeriodPolicy,
    ResearchPeriodRole,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesDataBatchState,
    SecuritiesMarketDataBatch,
)
from apex.domain.values import content_digest

NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


class FakeEvidenceStore:
    """Satisfies the narrow ``SecuritiesEvidencePort`` (``get_dataset``) --
    identical role to the equivalent fake in
    ``test_historical_market_data_application.py``."""

    def __init__(self) -> None:
        self.datasets: dict[str, tuple] = {}
        self.get_dataset_calls = 0

    def get_dataset(self, batch_id: str) -> tuple:
        self.get_dataset_calls += 1
        return self.datasets[batch_id]


class FakeCryptoEvidence:
    def __init__(self, candles: tuple[CryptoDailyCandle, ...]) -> None:
        self._candles = candles

    def candles(self, capture_id: str) -> tuple[CryptoDailyCandle, ...]:
        return self._candles


def _daily_dates(start: str, count: int) -> list[str]:
    from datetime import date as _date

    start_date = _date.fromisoformat(start)
    return [(start_date + timedelta(days=index)).isoformat() for index in range(count)]


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
        source_identity="FIXTURE",
    )


def _rising(index: int) -> str:
    return str(Decimal("100") + Decimal(index) * Decimal("0.3"))


def _falling(index: int) -> str:
    # Bounded slope so even a multi-year (750+ session) fixture never
    # approaches a nonpositive close/low.
    return str(Decimal("150") - Decimal(index) * Decimal("0.05"))


def _choppy(index: int) -> str:
    offset = Decimal("5") if index % 4 < 2 else Decimal("-5")
    return str(Decimal("100") + offset + Decimal(index % 7))


PRICE_PATHS = {"AAA": _rising, "BBB": _falling, "CCC": _choppy}


def snapshot_fixture(
    *,
    symbols: tuple[str, ...] = ("AAA", "BBB", "CCC"),
    day_count: int = 140,
    start: str = "2020-01-01",
    idempotency_key: str = "plan-fixture",
    evidence_class: str = "FIXTURE",
) -> tuple[InMemoryHistoricalMarketDataRepository, FakeEvidenceStore, HistoricalDatasetSnapshot]:
    historical_repository = InMemoryHistoricalMarketDataRepository()
    dates = _daily_dates(start, day_count)
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
        evidence_class=evidence_class,
        operator_action_id="op-fixture",
        idempotency_key=idempotency_key,
        created_at=NOW,
    )
    historical_repository.save_plan(plan)
    bars: list[SecuritiesBarEvidence] = []
    for member in universe:
        path = PRICE_PATHS[member.symbol_at_capture]
        for index, day in enumerate(dates):
            bars.append(_bar(member.instrument_id, day, path(index)))
    batch_id = f"batch-{plan.plan_id}"
    store = FakeEvidenceStore()
    store.datasets[batch_id] = ((), (), tuple(bars), ())
    _batch_record = SecuritiesMarketDataBatch(
        batch_id=batch_id,
        dataset_name="fixture",
        state=SecuritiesDataBatchState.READY,
        imported_at=NOW,
        source_artifact="x",
        source_sha256="a" * 64,
        source_byte_size=1,
        canonical_artifact="y",
        canonical_sha256="b" * 64,
        canonical_byte_size=1,
        instrument_count=len(universe),
        session_count=0,
        bar_count=len(bars),
        corporate_action_count=0,
        first_period_start=datetime.fromisoformat(f"{dates[0]}T14:30:00+00:00"),
        last_period_end=datetime.fromisoformat(f"{dates[-1]}T21:00:00+00:00"),
        idempotency_key=f"batch-{idempotency_key}",
        warnings=(),
        canonical_hash=content_digest(batch_id),
    )
    snapshot = HistoricalDatasetSnapshot.create(
        plan=plan,
        source_batch_ids=(batch_id,),
        source_operation_ids=("op-1",),
        observation_count=len(bars),
        completeness_state=HistoricalDatasetCompletenessState.COMPLETE,
        earliest_observation_date=dates[0],
        latest_observation_date=dates[-1],
        created_at=NOW,
    )
    historical_repository.save_snapshot(snapshot)
    return historical_repository, store, snapshot


def _create_run(
    *,
    replay_repository: InMemoryHistoricalReplayRepository,
    historical_repository: InMemoryHistoricalMarketDataRepository,
    snapshot: HistoricalDatasetSnapshot,
    idempotency_key: str = "run-1",
    hypothesis_version_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
):
    service = CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    )
    return service.execute(
        CreateReplayRunCommand(
            dataset_snapshot_id=snapshot.snapshot_id,
            start_date=start_date or snapshot.start_date,
            end_date=end_date or snapshot.end_date,
            calendar_identity=None,
            hypothesis_version_id=hypothesis_version_id,
            operator_action_id="op-1",
            idempotency_key=idempotency_key,
            created_at=NOW,
        )
    )


def _engine(
    *, replay_repository, historical_repository, evidence, crypto_evidence=None
) -> HistoricalReplayEngine:
    return HistoricalReplayEngine(
        repository=replay_repository,
        historical_data_repository=historical_repository,
        evidence=evidence,
        crypto_evidence=crypto_evidence,
    )


# --------------------------------------------------------------------------
# Create replay run
# --------------------------------------------------------------------------


def test_create_replay_run_binds_dataset_snapshot_and_universe() -> None:
    historical_repository, _store, snapshot = snapshot_fixture()
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    assert run.dataset_snapshot_id == snapshot.snapshot_id
    assert run.universe == ("stable-AAA", "stable-BBB", "stable-CCC")
    assert run.completion_state.value == "NOT_STARTED"
    assert run.cursor_as_of is None
    assert run.authority == "NONE"


def test_create_replay_run_idempotency_conflict() -> None:
    historical_repository, _store, snapshot = snapshot_fixture()
    replay_repository = InMemoryHistoricalReplayRepository()
    _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
        idempotency_key="same-key",
    )
    with pytest.raises(HistoricalReplayConflict):
        _create_run(
            replay_repository=replay_repository,
            historical_repository=historical_repository,
            snapshot=snapshot,
            idempotency_key="same-key",
            hypothesis_version_id="a-different-declared-hypothesis",
        )


def test_create_replay_run_unknown_snapshot_not_found() -> None:
    historical_repository = InMemoryHistoricalMarketDataRepository()
    replay_repository = InMemoryHistoricalReplayRepository()
    service = CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    )
    with pytest.raises(HistoricalReplayNotFound):
        service.execute(
            CreateReplayRunCommand(
                dataset_snapshot_id="does-not-exist",
                start_date="2020-01-01",
                end_date="2020-01-05",
                calendar_identity=None,
                hypothesis_version_id=None,
                operator_action_id="op-1",
                idempotency_key="k",
                created_at=NOW,
            )
        )


# --------------------------------------------------------------------------
# Start / checkpoint / resume
# --------------------------------------------------------------------------


def test_start_requires_not_started_state() -> None:
    historical_repository, store, snapshot = snapshot_fixture()
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    starter = StartReplayRun(repository=replay_repository, engine=engine)
    starter.execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=3, requested_at=NOW)
    )
    with pytest.raises(HistoricalReplayApplicationError, match="ALREADY_STARTED"):
        starter.execute(
            StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=3, requested_at=NOW)
        )


def test_start_then_resume_completes_the_declared_range_with_checkpoints() -> None:
    historical_repository, store, snapshot = snapshot_fixture(day_count=100)
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    started = StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=40, requested_at=NOW)
    )
    assert started.completion_state is ReplayRunCompletionState.IN_PROGRESS
    assert started.completed_step_count == 40
    checkpoints_after_start = replay_repository.list_checkpoints(run.replay_run_id)
    assert len(checkpoints_after_start) == 40

    resumer = ResumeReplayRun(repository=replay_repository, engine=engine)
    completed = resumer.execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=200, requested_at=NOW)
    )
    assert completed.completion_state is ReplayRunCompletionState.COMPLETE
    assert completed.cursor_as_of == completed.final_as_of
    checkpoints_after_resume = replay_repository.list_checkpoints(run.replay_run_id)
    assert (
        len(checkpoints_after_resume) == 100
    )  # one calendar day per day_count, no gaps, no duplicates

    frames = replay_repository.list_frames(run.replay_run_id, limit=1000)
    assert len(frames) == 100
    assert len({item.frame_id for item in frames}) == 100  # no duplicates


def test_resuming_a_complete_run_is_an_idempotent_no_op() -> None:
    historical_repository, store, snapshot = snapshot_fixture(day_count=20)
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=20, requested_at=NOW)
    )
    resumer = ResumeReplayRun(repository=replay_repository, engine=engine)
    once = resumer.execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=5, requested_at=NOW)
    )
    twice = resumer.execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=5, requested_at=NOW)
    )
    assert once.completion_state is ReplayRunCompletionState.COMPLETE
    assert twice == once
    assert len(replay_repository.list_frames(run.replay_run_id, limit=1000)) == 20


def test_resume_before_start_is_rejected() -> None:
    historical_repository, store, snapshot = snapshot_fixture(day_count=10)
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    with pytest.raises(HistoricalReplayApplicationError, match="NOT_YET_STARTED"):
        ResumeReplayRun(repository=replay_repository, engine=engine).execute(
            StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=5, requested_at=NOW)
        )


def test_exact_replay_is_deterministic_across_two_independent_runs() -> None:
    historical_repository, store, snapshot = snapshot_fixture(
        day_count=60, idempotency_key="plan-a"
    )
    historical_repository_2, store_2, snapshot_2 = snapshot_fixture(
        day_count=60, idempotency_key="plan-a"
    )
    assert snapshot.snapshot_id == snapshot_2.snapshot_id  # content-addressed, identical inputs

    replay_repository = InMemoryHistoricalReplayRepository()
    replay_repository_2 = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
        idempotency_key="exact-1",
    )
    run_2 = _create_run(
        replay_repository=replay_repository_2,
        historical_repository=historical_repository_2,
        snapshot=snapshot_2,
        idempotency_key="exact-1",
    )
    assert run.replay_run_id == run_2.replay_run_id

    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    engine_2 = _engine(
        replay_repository=replay_repository_2,
        historical_repository=historical_repository_2,
        evidence=store_2,
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=60, requested_at=NOW)
    )
    StartReplayRun(repository=replay_repository_2, engine=engine_2).execute(
        StepReplayRunCommand(replay_run_id=run_2.replay_run_id, maximum_steps=60, requested_at=NOW)
    )
    frames = sorted(
        replay_repository.list_frames(run.replay_run_id, limit=1000), key=lambda f: f.as_of
    )
    frames_2 = sorted(
        replay_repository_2.list_frames(run_2.replay_run_id, limit=1000), key=lambda f: f.as_of
    )
    assert [item.canonical_hash for item in frames] == [item.canonical_hash for item in frames_2]
    evaluations = sorted(
        replay_repository.list_evaluations(run.replay_run_id, limit=1000),
        key=lambda e: (e.as_of, e.method_id),
    )
    evaluations_2 = sorted(
        replay_repository_2.list_evaluations(run_2.replay_run_id, limit=1000),
        key=lambda e: (e.as_of, e.method_id),
    )
    assert [item.canonical_hash for item in evaluations] == [
        item.canonical_hash for item in evaluations_2
    ]


def test_resume_rejects_when_policy_hash_changed_since_draft() -> None:
    historical_repository, store, snapshot = snapshot_fixture(day_count=10)
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=2, requested_at=NOW)
    )
    stale = replace(replay_repository.get_run(run.replay_run_id), feature_policy_hash="f" * 64)
    replay_repository.save_run(stale)
    with pytest.raises(HistoricalReplayConflict, match="POLICY_IDENTITY_CHANGED"):
        ResumeReplayRun(repository=replay_repository, engine=engine).execute(
            StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=2, requested_at=NOW)
        )


def test_resume_rejects_when_dataset_snapshot_no_longer_found() -> None:
    historical_repository, store, snapshot = snapshot_fixture(day_count=10)
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=2, requested_at=NOW)
    )
    empty_historical_repository = InMemoryHistoricalMarketDataRepository()
    detached_engine = _engine(
        replay_repository=replay_repository,
        historical_repository=empty_historical_repository,
        evidence=store,
    )
    with pytest.raises(HistoricalReplayConflict, match="NO_LONGER_FOUND"):
        ResumeReplayRun(repository=replay_repository, engine=detached_engine).execute(
            StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=2, requested_at=NOW)
        )


def test_partial_failure_marks_run_failed_and_retains_prior_checkpoints() -> None:
    historical_repository, store, snapshot = snapshot_fixture(day_count=20)

    class _FlakyRepository(InMemoryHistoricalReplayRepository):
        def __init__(self, *, fail_after_frames: int) -> None:
            super().__init__()
            self._fail_after = fail_after_frames
            self._frame_saves = 0

        def save_frame(self, frame):  # type: ignore[override]
            self._frame_saves += 1
            if self._frame_saves > self._fail_after:
                raise RuntimeError("SIMULATED_DOWNSTREAM_FAILURE")
            return super().save_frame(frame)

    replay_repository = _FlakyRepository(fail_after_frames=2)
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    with pytest.raises(HistoricalReplayApplicationError, match="UNEXPECTED_ERROR"):
        StartReplayRun(repository=replay_repository, engine=engine).execute(
            StepReplayRunCommand(
                replay_run_id=run.replay_run_id, maximum_steps=10, requested_at=NOW
            )
        )
    failed_run = replay_repository.get_run(run.replay_run_id)
    assert failed_run.completion_state is ReplayRunCompletionState.FAILED
    assert failed_run.failure_reason is not None
    assert failed_run.completed_step_count == 2
    assert len(replay_repository.list_checkpoints(run.replay_run_id)) == 2
    assert len(replay_repository.list_frames(run.replay_run_id, limit=1000)) == 2
    # A failed run cannot silently be stepped further.
    with pytest.raises(HistoricalReplayApplicationError, match="FAILED_CANNOT_STEP"):
        ResumeReplayRun(repository=replay_repository, engine=engine).execute(
            StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=2, requested_at=NOW)
        )


def test_bounded_result_lists_respect_limit() -> None:
    historical_repository, store, snapshot = snapshot_fixture(day_count=30)
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=30, requested_at=NOW)
    )
    assert len(replay_repository.list_frames(run.replay_run_id, limit=5)) == 5
    assert len(replay_repository.list_evaluations(run.replay_run_id, limit=7)) == 7


# --------------------------------------------------------------------------
# Outcome isolation across steps
# --------------------------------------------------------------------------


def test_outcome_progresses_from_pending_to_complete_as_replay_advances() -> None:
    historical_repository, store, snapshot = snapshot_fixture(day_count=140)
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(
            replay_run_id=run.replay_run_id,
            maximum_steps=5,
            requested_at=NOW,
            benchmark_stable_security_id="stable-AAA",
        )
    )
    early_evaluations = replay_repository.list_evaluations(run.replay_run_id, limit=1000)
    breadth_evaluations = [
        item
        for item in early_evaluations
        if item.method_id.value == "BREADTH_VS_SUBSEQUENT_RETURN_V1"
    ]
    assert breadth_evaluations
    # A 30-calendar-day outcome window cannot possibly be COMPLETE after only
    # 5 replay steps (it may legitimately already be PARTIAL for the
    # earliest evaluations, whose outcome_start has already come into view).
    assert not any(
        item.outcome is not None and item.outcome.completeness.value == "COMPLETE"
        for item in breadth_evaluations
    )

    ResumeReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(
            replay_run_id=run.replay_run_id,
            maximum_steps=135,
            requested_at=NOW,
            benchmark_stable_security_id="stable-AAA",
        )
    )
    later_evaluations = replay_repository.list_evaluations(run.replay_run_id, limit=1000)
    completed_outcomes = [
        item
        for item in later_evaluations
        if item.outcome is not None and item.outcome.completeness.value == "COMPLETE"
    ]
    assert completed_outcomes
    for item in completed_outcomes:
        assert item.outcome.window.outcome_start > item.as_of
        assert item.outcome.outcome_value is not None


def test_crypto_cross_asset_evaluation_present_when_crypto_evidence_supplied() -> None:
    historical_repository, store, snapshot = snapshot_fixture(day_count=60)
    dates = _daily_dates("2020-01-01", 60)
    candles = tuple(
        CryptoDailyCandle.create(
            capture_id="crypto-capture-1",
            attempt_id="attempt-1",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            bucket_start=datetime.fromisoformat(f"{day}T00:00:00+00:00"),
            open=str(Decimal("30000") + Decimal(index) * Decimal("50")),
            high=str(Decimal("30100") + Decimal(index) * Decimal("50")),
            low=str(Decimal("29900") + Decimal(index) * Decimal("50")),
            close=str(Decimal("30000") + Decimal(index) * Decimal("50")),
            volume="100",
            ingested_at=datetime.fromisoformat(f"{day}T23:59:59+00:00"),
            raw_response_hash="a" * 64,
        )
        for index, day in enumerate(dates)
    )
    assert all(candle.provider == COINBASE_PROVIDER for candle in candles)
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
        crypto_evidence=FakeCryptoEvidence(candles),
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(
            replay_run_id=run.replay_run_id,
            maximum_steps=30,
            requested_at=NOW,
            crypto_capture_id="crypto-capture-1",
        )
    )
    evaluations = replay_repository.list_evaluations(run.replay_run_id, limit=1000)
    crypto_evaluations = [
        item
        for item in evaluations
        if item.method_id.value == "CRYPTO_EQUITY_CONTEXT_COMPARISON_V1"
    ]
    assert crypto_evaluations
    assert any(key == "crypto_risk_tone" for key, _value in crypto_evaluations[-1].result)


# --------------------------------------------------------------------------
# Historical experiment integration + holdout
# --------------------------------------------------------------------------


def test_experiment_lineage_from_hypothesis_through_replay_to_outcome() -> None:
    historical_repository, store, snapshot = snapshot_fixture(day_count=140)
    replay_repository = InMemoryHistoricalReplayRepository()
    research_repository = InMemoryResearchEvidenceRepository()
    hypothesis = RegisterResearchHypothesis(research_repository).execute(
        hypothesis_id="breadth-hypothesis",
        revision=1,
        question="Does elevated breadth precede positive subsequent returns?",
        motivation="Engineering proof of the replay-to-experiment lineage.",
        actor="researcher-1",
        market_scope=("US_EQUITY",),
        created_at=NOW,
    )
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
        hypothesis_version_id=hypothesis.hypothesis_version_id,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(
            replay_run_id=run.replay_run_id,
            maximum_steps=140,
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
            code_revision="test-revision",
            actor="researcher-1",
            decided_at=NOW,
        )
    )
    assert link.hypothesis_version_id == hypothesis.hypothesis_version_id
    research_snapshot = research_repository.snapshot(link.research_dataset_snapshot_id)
    assert research_snapshot is not None
    assert research_snapshot.evidence_class.value == "FIXTURE"
    experiment_run = research_repository.run(link.research_experiment_run_id)
    assert experiment_run is not None
    assert experiment_run.hypothesis_version_id == hypothesis.hypothesis_version_id
    assert experiment_run.snapshot_id == research_snapshot.snapshot_id
    outcome = research_repository.outcome_for_run(experiment_run.run_id)
    assert outcome is not None
    assert outcome.decision.value == "DEFERRED"  # never promoted this milestone
    assert link.research_experiment_outcome_id == outcome.outcome_id
    # idempotent replay of the construction returns identical lineage
    replayed_link = ConstructReplayExperiment(
        repository=replay_repository,
        historical_data_repository=historical_repository,
        research_evidence=research_repository,
    ).execute(
        ConstructReplayExperimentCommand(
            replay_run_id=run.replay_run_id,
            code_revision="test-revision",
            actor="researcher-1",
            decided_at=NOW,
        )
    )
    # Identical inputs (same run, same code revision) are content-addressed
    # to the identical link -- reconstructing does not create a duplicate.
    assert replayed_link.link_id == link.link_id
    assert len(replay_repository.experiment_links(run.replay_run_id)) == 1
    assert replayed_link.research_experiment_run_id == link.research_experiment_run_id


def test_experiment_construction_requires_a_declared_hypothesis() -> None:
    historical_repository, _store, snapshot = snapshot_fixture(day_count=10)
    replay_repository = InMemoryHistoricalReplayRepository()
    research_repository = InMemoryResearchEvidenceRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    with pytest.raises(HistoricalReplayApplicationError, match="NO_DECLARED_HYPOTHESIS"):
        ConstructReplayExperiment(
            repository=replay_repository,
            historical_data_repository=historical_repository,
            research_evidence=research_repository,
        ).execute(
            ConstructReplayExperimentCommand(
                replay_run_id=run.replay_run_id,
                code_revision="rev",
                actor="researcher-1",
                decided_at=NOW,
            )
        )


def test_holdout_locked_period_blocks_development_access_end_to_end() -> None:
    historical_repository, store, snapshot = snapshot_fixture(day_count=60)
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=60, requested_at=NOW)
    )
    dates = _daily_dates("2020-01-01", 60)
    policy = ResearchPeriodPolicy.create(
        replay_run_id=run.replay_run_id,
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, dates[0], dates[29], False),
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, dates[30], dates[59], True),
        ),
        minimum_sample_count=5,
        created_at=NOW,
    )
    DeclareResearchPeriodPolicy(repository=replay_repository).execute(
        DeclareResearchPeriodPolicyCommand(replay_run_id=run.replay_run_id, policy=policy)
    )
    reader = ListEvaluationsForPeriod(repository=replay_repository)
    train_evaluations = reader.execute(
        replay_run_id=run.replay_run_id,
        role=ResearchPeriodRole.TRAIN,
        purpose="FEATURE_DEVELOPMENT",
        limit=1000,
    )
    assert train_evaluations
    with pytest.raises(HoldoutViolationError, match="CANNOT_BE_USED_FOR_DEVELOPMENT"):
        reader.execute(
            replay_run_id=run.replay_run_id,
            role=ResearchPeriodRole.HOLDOUT,
            purpose="FEATURE_DEVELOPMENT",
            limit=1000,
        )
    holdout_evaluations = reader.execute(
        replay_run_id=run.replay_run_id,
        role=ResearchPeriodRole.HOLDOUT,
        purpose="EVALUATION_ONLY",
        limit=1000,
    )
    assert holdout_evaluations
    assert all(dates[30] <= item.session_date <= dates[59] for item in holdout_evaluations)


# --------------------------------------------------------------------------
# Historical research summary
# --------------------------------------------------------------------------


def test_historical_research_summary_aggregates_statistics_and_regime_distribution() -> None:
    historical_repository, store, snapshot = snapshot_fixture(day_count=140)
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(
            replay_run_id=run.replay_run_id,
            maximum_steps=140,
            requested_at=NOW,
            benchmark_stable_security_id="stable-AAA",
        )
    )
    summary = build_historical_research_summary(
        repository=replay_repository, replay_run_id=run.replay_run_id
    )
    assert summary.evaluation_count > 0
    assert summary.complete_outcome_count > 0
    assert summary.regime_label_distribution
    assert sum(count for _label, count in summary.regime_label_distribution) == 140
    assert summary.overall_summary.sample_count == summary.complete_outcome_count
    assert not summary.holdout_declared


# --------------------------------------------------------------------------
# Performance measurement (section 18)
# --------------------------------------------------------------------------


def test_multi_year_multi_security_replay_completes_within_a_practical_bound() -> None:
    historical_repository, store, snapshot = snapshot_fixture(
        symbols=("AAA", "BBB", "CCC"), day_count=750, idempotency_key="perf-plan"
    )
    replay_repository = InMemoryHistoricalReplayRepository()
    run = _create_run(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        snapshot=snapshot,
        idempotency_key="perf-run",
    )
    engine = _engine(
        replay_repository=replay_repository,
        historical_repository=historical_repository,
        evidence=store,
    )
    started_at = time.monotonic()
    completed = StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(
            replay_run_id=run.replay_run_id,
            maximum_steps=750,
            requested_at=NOW,
            benchmark_stable_security_id="stable-AAA",
        )
    )
    elapsed_seconds = time.monotonic() - started_at
    assert completed.completion_state is ReplayRunCompletionState.COMPLETE
    assert completed.completed_step_count == 750
    # ~3 years x 3 securities of daily point-in-time feature/regime/research-
    # method computation against an in-memory (no network) evidence store.
    # Generous bound: this is a correctness/scale proof, not a micro-benchmark.
    assert elapsed_seconds < 30, f"replay of 750 daily steps took {elapsed_seconds:.2f}s"
    # Exactly one bounded evidence load per snapshot for the whole run, not
    # one per replay step -- the caching contract section 18 requires.
    assert store.get_dataset_calls == 1
