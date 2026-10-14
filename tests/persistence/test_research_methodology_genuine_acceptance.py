"""Genuine (non-fixture) retained-evidence acceptance for quant research
methodology and walk-forward validation (milestone section 16) -- reuses
the exact genuine BTC-USD retained evidence the prior milestone's own
genuine acceptance already established
(``btc-usd-genuine-2024-07-01-2026-07-01-v3`` or whatever else genuinely
qualifies), applying the *new*, asset-agnostic methodology primitives
(fold generation, benchmark comparison, statistical summaries, conclusion
derivation) directly to it.

This deliberately does not force crypto candles through the equity-
anchored ``ResearchTrial``/``WalkForwardFold`` domain types -- those are
typed around a replay-run evaluation lineage that crypto capture does not
and should not produce (crypto is not ingested through
``HistoricalCapturePlan``/``SecuritiesBarEvidence`` at all). What genuinely
matters for section 16 -- and what this proves -- is that the *methodology
controls themselves* (deterministic fold boundaries, structurally-enforced
no-lookahead, honest sample accounting, an explicit benchmark, and the
conclusion model) are asset-agnostic pure functions that work correctly
against real BTC history, not equity-specific machinery that merely
happens to also run on crypto. No alpha is claimed anywhere in this file.

Genuine equity remains unavailable in this environment (Alpaca credentials
absent, inherited from every prior milestone's own documented limitation)
-- that is proven honestly, separately, in
``test_historical_replay_genuine_acceptance.py::
test_genuine_equity_replay_is_honestly_empty_not_fabricated`` and is not
repeated here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.engine import Engine

from apex.adapters.crypto_research_postgres import PostgresCryptoResearchRepository
from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
from apex.adapters.securities_postgres import PostgresSecuritiesRepository
from apex.application.historical_replay import (
    CreateReplayRun,
    CreateReplayRunCommand,
    HistoricalReplayEngine,
    StartReplayRun,
    StepReplayRunCommand,
)
from apex.application.research_methodology import (
    RecordResearchTrial,
    RecordResearchTrialCommand,
    ResearchMethodologyApplicationError,
)
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
)
from apex.domain.historical_replay import ReplayRunCompletionState, SummaryKind, summarize_values
from apex.domain.research_methodology import (
    BenchmarkKind,
    ConfigurationOrigin,
    ResearchConclusionState,
    ResearchConfiguration,
    ResearchPhase,
    SampleQualityState,
    TrainingWindowMode,
    compare_to_benchmark,
    derive_conclusion_state,
    generate_fold_windows,
    resolve_minimum_sample_requirement,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)

MINIMUM_GENUINE_CANDLES = 120


def _window_return(
    candles: tuple[CryptoDailyCandle, ...], *, start_date: str, end_date: str
) -> Decimal | None:
    """Genuine, point-in-time-safe window return computed directly from
    retained candles' own ``bucket_start`` dates -- the identical style
    ``evaluate_breadth_vs_subsequent_return``'s own outcome calculation
    uses: ``(last.close - first.close) / first.close`` over whatever is
    actually retained in the declared window, never a fabricated or
    interpolated value when a window is thin."""

    windowed = sorted(
        (
            candle
            for candle in candles
            if start_date <= candle.bucket_start.date().isoformat() <= end_date
        ),
        key=lambda candle: candle.bucket_start,
    )
    if len(windowed) < 2:
        return None
    first_close = Decimal(windowed[0].close)
    last_close = Decimal(windowed[-1].close)
    return (last_close - first_close) / first_close


def _adequate_genuine_btc_capture(
    postgres_engine: Engine,
) -> tuple[object, tuple[CryptoDailyCandle, ...]] | None:
    repository = PostgresCryptoResearchRepository(postgres_engine)
    candidates = [
        capture
        for capture in repository.captures()
        if "fixture" not in capture.capture_id.lower() and capture.status.value == "COMPLETED"
    ]
    for capture in candidates:
        candles = repository.candles(capture.capture_id)
        if len(candles) >= MINIMUM_GENUINE_CANDLES:
            return capture, tuple(sorted(candles, key=lambda candle: candle.bucket_start))
    return None


def test_genuine_walk_forward_style_evaluation_over_real_btc_history(
    postgres_engine: Engine,
) -> None:
    """Section 16: at least one bounded descriptive/walk-forward research
    evaluation over genuine BTC history, proving phase separation, folds,
    no lookahead, sample accounting, benchmark comparison, and the
    conclusion model -- explicitly not an alpha claim."""

    adequate = _adequate_genuine_btc_capture(postgres_engine)
    if adequate is None:
        pytest.skip(
            "GENUINE_MULTI_ASSET_DATASET_NOT_ADEQUATE: no retained genuine crypto capture "
            f"with >= {MINIMUM_GENUINE_CANDLES} daily candles exists in this environment; "
            "reporting honestly rather than fabricating one."
        )
    _capture, candles = adequate
    session_dates = tuple(sorted({candle.bucket_start.date().isoformat() for candle in candles}))

    # Genuine, asset-agnostic fold generation -- the identical pure function
    # the equity-anchored WalkForwardEngine uses internally, applied here
    # directly to real BTC session dates.
    folds = generate_fold_windows(
        session_dates=session_dates,
        training_window_sessions=60,
        validation_window_sessions=0,
        test_window_sessions=20,
        step_sessions=20,
        window_mode=TrainingWindowMode.ROLLING,
    )
    assert folds, "genuinely retained BTC history did not produce a single fold"

    # No lookahead: every fold's test window starts strictly after its own
    # training window ends -- a structural property of generate_fold_windows
    # itself (see test_fold_windows_never_run_past_the_available_session_dates
    # and test_walk_forward_fold_rejects_test_window_overlapping_train_or_
    # validation for the domain-level proof), reconfirmed here against real
    # dates rather than assumed.
    for fold in folds:
        assert fold.test_start > fold.train_end
        assert fold.test_end <= session_dates[-1]

    train_returns: list[Decimal] = []
    test_returns: list[Decimal] = []
    for fold in folds:
        train_return = _window_return(candles, start_date=fold.train_start, end_date=fold.train_end)
        test_return = _window_return(candles, start_date=fold.test_start, end_date=fold.test_end)
        if train_return is not None:
            train_returns.append(train_return)
        if test_return is not None:
            test_returns.append(test_return)
    assert test_returns, "genuine BTC test-fold outcomes must be non-empty, not fabricated"
    assert train_returns, "genuine BTC train-fold signals must be non-empty, not fabricated"

    # Honest sample accounting: the walk-forward phase's own (conservative,
    # versioned) minimum -- not the placeholder of 5 this milestone replaced.
    requirement = resolve_minimum_sample_requirement(phase=ResearchPhase.WALK_FORWARD)
    sample_quality = (
        SampleQualityState.SUFFICIENT
        if Decimal(len(test_returns)) >= requirement.minimum_effective_sample
        else SampleQualityState.INSUFFICIENT_EFFECTIVE_SAMPLE
    )

    # Descriptive statistics over the genuine test-fold outcomes -- phase
    # explicitly WALK_FORWARD_AGGREGATE, never presented as an exploratory,
    # validation, or holdout result (phase separation).
    summary = summarize_values(
        tuple(test_returns),
        kind=SummaryKind.WALK_FORWARD_AGGREGATE,
        group_label="genuine_btc_test_folds",
    )
    assert summary.sample_count == len(test_returns)

    # Benchmark: each fold's own training-period return distribution -- a
    # genuinely independent (different date range), non-cherry-picked null
    # already present in this same walk-forward structure, not an invented
    # baseline series.
    sample_mean = summary.mean if summary.mean is not None else Decimal(0)
    benchmark = compare_to_benchmark(
        sample_mean=sample_mean,
        benchmark_kind=BenchmarkKind.PRIOR_REGIME_DISTRIBUTION,
        benchmark_values=tuple(train_returns),
    )
    assert benchmark.benchmark_sample_count == len(train_returns)

    # The conclusion model, run honestly against real data: with no holdout
    # declared and no multiple-testing correction performed for this single
    # bounded demonstration, the conclusion can never legitimately be
    # SUPPORTED -- proving the derivation genuinely fails closed rather than
    # rubber-stamping a positive-looking number.
    conclusion_state = derive_conclusion_state(
        sample_quality=sample_quality,
        robustness_summary=None,
        validation_beats_benchmark=benchmark.beats_benchmark,
        holdout_beats_benchmark=None,
        multiple_testing_corrected_significant=None,
    )
    assert conclusion_state is not ResearchConclusionState.SUPPORTED
    assert conclusion_state in (
        ResearchConclusionState.WEAK_SUPPORT,
        ResearchConclusionState.INCONCLUSIVE,
        ResearchConclusionState.REJECTED,
        ResearchConclusionState.INSUFFICIENT_EVIDENCE,
    )


def test_genuine_equity_anchored_trial_is_honestly_refused_not_fabricated(
    postgres_engine: Engine,
) -> None:
    """Extends the prior milestone's own honest-empty proof
    (``test_genuine_equity_replay_is_honestly_empty_not_fabricated``) into
    this new layer specifically: attempting to record a research trial
    against a real, GENUINE_PUBLIC, zero-bar equity snapshot must refuse
    with an explicit, honest reason -- never a fabricated trial with an
    invented mean, and never a silent crash."""

    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    policy = HistoricalCapturePolicy.create(
        provider="ALPACA_MARKET_DATA_V2",
        start_date="2021-01-01",
        end_date="2021-01-10",
        chunk_size=10,
        maximum_instruments=10,
        maximum_requests=10,
        maximum_bytes=1_000_000,
    )
    universe = (
        HistoricalCaptureUniverseMember.create(
            stable_security_id="stable_METHODOLOGYGENUINEZ",
            symbol_at_capture="METHODOLOGYGENUINEZ",
            asset_class="COMMON_STOCK",
        ),
    )
    plan = HistoricalCapturePlan.draft(
        policy=policy,
        universe=universe,
        evidence_class="GENUINE_PUBLIC",
        operator_action_id="methodology-genuine-acceptance-plan",
        idempotency_key="methodology-genuine-acceptance-plan-v1",
        created_at=NOW,
    )
    historical_repository.save_plan(plan)
    # Credentials are genuinely absent in this environment -- no chunk is
    # ever run and no batch is ever created, exactly as historical_market_
    # data's own genuine acceptance already established. Construct the
    # honest zero-observation snapshot directly, exactly like a chunk that
    # reached CREDENTIALS_REQUIRED would.
    snapshot = HistoricalDatasetSnapshot.create(
        plan=plan,
        source_batch_ids=(),
        source_operation_ids=("methodology-genuine-credentials-required-op-1",),
        observation_count=0,
        completeness_state=HistoricalDatasetCompletenessState.PARTIAL,
        earliest_observation_date=None,
        latest_observation_date=None,
        created_at=NOW,
    )
    historical_repository.save_snapshot(snapshot)
    assert snapshot.evidence_class.value == "GENUINE_PUBLIC"
    assert snapshot.observation_count == 0

    replay_repository = PostgresHistoricalReplayRepository(postgres_engine)
    run = CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    ).execute(
        CreateReplayRunCommand(
            dataset_snapshot_id=snapshot.snapshot_id,
            start_date="2021-01-01",
            end_date="2021-01-05",
            calendar_identity=None,
            hypothesis_version_id=None,
            operator_action_id="methodology-genuine-acceptance-run",
            idempotency_key="methodology-genuine-acceptance-run-v1",
            created_at=NOW,
        )
    )
    engine = HistoricalReplayEngine(
        repository=replay_repository,
        historical_data_repository=historical_repository,
        evidence=PostgresSecuritiesRepository(postgres_engine),
    )
    completed = StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(replay_run_id=run.replay_run_id, maximum_steps=5, requested_at=NOW)
    )
    assert completed.completion_state is ReplayRunCompletionState.COMPLETE

    from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository

    methodology_repository = PostgresResearchMethodologyRepository(postgres_engine)
    configuration = ResearchConfiguration.create(
        method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
        method_version="1",
        parameter_values=(("lookback_sessions", "20"),),
        origin=ConfigurationOrigin.FIXED_A_PRIORI,
        candidates_examined_count=1,
    )
    with pytest.raises(ResearchMethodologyApplicationError, match="NO_RESOLVED_OUTCOMES"):
        RecordResearchTrial(
            repository=methodology_repository, historical_replay=replay_repository
        ).execute(
            RecordResearchTrialCommand(
                hypothesis_version_id="hv-methodology-genuine-empty",
                dataset_snapshot_id=snapshot.snapshot_id,
                replay_run_id=run.replay_run_id,
                phase=ResearchPhase.EXPLORATORY,
                market_scope=("US_EQUITY",),
                configuration=configuration,
                method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
                session_date_start="2021-01-01",
                session_date_end="2021-01-05",
                benchmark_kind=None,
                created_at=NOW,
            )
        )
    # No trial was fabricated -- the repository genuinely retains nothing.
    assert methodology_repository.trials_for_replay_run(run.replay_run_id) == ()
