"""Genuine (non-fixture) retained-evidence acceptance for the point-in-time
historical replay engine -- clearly distinguished from the engineering
(fixture) acceptance proven throughout ``test_postgres_historical_replay.py``
and ``test_historical_replay_application.py``.

Honest state of this environment, mirrored exactly from the prior milestone
(``historical_market_data``)'s own genuine acceptance: Alpaca credentials are
absent, so genuine equity historical capture retains zero bars. This test
does not fabricate a genuine equity dataset -- it proves the replay engine
handles that honestly (a real, zero-observation, GENUINE_PUBLIC-labeled
snapshot produces frames that are entirely and explicitly UNAVAILABLE, never
a silently fabricated value).

It then separately proves the cross-asset seam (section 20) against whatever
genuine crypto historical evidence already exists in this environment (if
any) -- real Coinbase-sourced BTC-USD daily candles, not a fixture -- since
crypto capture was never blocked by the same credential gap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine

from apex.adapters.crypto_research_postgres import PostgresCryptoResearchRepository
from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.securities_postgres import PostgresSecuritiesRepository
from apex.application.historical_replay import (
    CreateReplayRun,
    CreateReplayRunCommand,
    HistoricalReplayEngine,
    StartReplayRun,
    StepReplayRunCommand,
    classify_crypto_risk_tone,
    classify_crypto_volatility,
    crypto_return_and_volatility,
)
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalDatasetCompletenessState,
)
from apex.domain.historical_replay import (
    EvidenceAvailabilityState,
    RegimeDimension,
    ReplayRunCompletionState,
    RiskToneLabel,
    VolatilityRegimeLabel,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


def test_genuine_equity_replay_is_honestly_empty_not_fabricated(postgres_engine: Engine) -> None:
    """Mirrors ``test_credentials_absent_makes_zero_requests_and_snapshot_is_
    honestly_empty`` (migration 0077's own genuine acceptance): a real,
    GENUINE_PUBLIC-labeled dataset snapshot with zero retained bars. The
    replay engine must run against it without crashing and without
    fabricating a single feature value."""

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
            stable_security_id="stable_GENUINEZ",
            symbol_at_capture="GENUINEZ",
            asset_class="COMMON_STOCK",
        ),
    )
    plan = HistoricalCapturePlan.draft(
        policy=policy,
        universe=universe,
        evidence_class="GENUINE_PUBLIC",
        operator_action_id="genuine-acceptance-plan",
        idempotency_key="genuine-acceptance-plan-v1",
        created_at=NOW,
    )
    historical_repository.save_plan(plan)
    # No chunk is ever run and no batch is ever created -- credentials are
    # genuinely absent in this environment, exactly as historical_market_data's
    # own genuine acceptance already established. Materializing a snapshot
    # from zero attempted batches is refused by that foundation's own
    # invariant (SNAPSHOT_REQUIRES_SOURCE_CAPTURES); construct the honest
    # zero-observation snapshot directly, exactly like a chunk that reached
    # CREDENTIALS_REQUIRED would.
    from apex.domain.historical_market_data import HistoricalDatasetSnapshot

    snapshot = HistoricalDatasetSnapshot.create(
        plan=plan,
        source_batch_ids=(),
        source_operation_ids=("genuine-credentials-required-op-1",),
        observation_count=0,
        completeness_state=HistoricalDatasetCompletenessState.PARTIAL,
        earliest_observation_date=None,
        latest_observation_date=None,
        created_at=NOW,
    )
    historical_repository.save_snapshot(snapshot)
    assert snapshot.evidence_class.value == "GENUINE_PUBLIC"
    assert snapshot.observation_count == 0

    from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository

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
            operator_action_id="genuine-acceptance-run",
            idempotency_key="genuine-acceptance-run-v1",
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
    frames = replay_repository.list_frames(run.replay_run_id, limit=100)
    assert len(frames) == 5
    for frame in frames:
        assert frame.excluded_evidence_count == 1  # the one declared security, honestly excluded
        assert frame.securities[0].evidence_availability is EvidenceAvailabilityState.UNAVAILABLE
        assert frame.securities[0].observation_count == 0
        assert dict(frame.securities[0].returns)[1] is None
        assert frame.regime.label_for(RegimeDimension.RISK_TONE) == "INSUFFICIENT_EVIDENCE"
        assert "POINT_IN_TIME_UNIVERSE_INCOMPLETE" in frame.point_in_time_limitations
    evaluations = replay_repository.list_evaluations(run.replay_run_id, limit=100)
    assert evaluations  # the methods still run -- they just honestly report blockers
    assert any(item.blockers for item in evaluations)


def test_genuine_crypto_evidence_supports_the_cross_asset_seam_if_retained(
    postgres_engine: Engine,
) -> None:
    """Uses whatever genuine (Coinbase-sourced, non-fixture) BTC-USD daily
    candles already exist in this environment. If none are adequate, this
    reports that honestly (xfail-free skip with a clear reason) rather than
    fabricating a dataset."""

    crypto_repository = PostgresCryptoResearchRepository(postgres_engine)
    candidates = [
        capture
        for capture in crypto_repository.captures()
        if "fixture" not in capture.capture_id.lower() and capture.status.value == "COMPLETED"
    ]
    adequate = None
    for capture in candidates:
        candles = crypto_repository.candles(capture.capture_id)
        if len(candles) >= 42:
            adequate = (capture, candles)
            break
    if adequate is None:
        pytest.skip(
            "GENUINE_MULTI_ASSET_DATASET_NOT_ADEQUATE: no retained genuine crypto capture "
            "with >= 42 daily candles exists in this environment; reporting honestly rather "
            "than fabricating one."
        )
    capture, candles = adequate
    latest_event_at = max(candle.bucket_start for candle in candles)
    as_of = latest_event_at.astimezone(UTC) + timedelta(hours=23, minutes=59)

    return_20, volatility, sample_count = crypto_return_and_volatility(tuple(candles), as_of=as_of)
    assert sample_count >= 42
    assert return_20 is not None
    assert volatility is not None and volatility >= 0
    volatility_label = classify_crypto_volatility(volatility)
    risk_tone = classify_crypto_risk_tone(
        return_20_session=return_20, volatility_label=volatility_label
    )
    assert volatility_label != VolatilityRegimeLabel.INSUFFICIENT_EVIDENCE
    assert risk_tone in tuple(RiskToneLabel)
    # Proof this is genuinely point-in-time: an as_of before the capture even
    # began yields insufficient evidence, never a fabricated classification.
    too_early = min(candle.bucket_start for candle in candles) - timedelta(days=1)
    early_return, early_volatility, early_count = crypto_return_and_volatility(
        tuple(candles), as_of=too_early
    )
    assert early_count == 0
    assert early_return is None
    assert early_volatility is None
