from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.adapters.coinbase_public import (
    COINBASE_PUBLIC_HOST,
    FixtureCoinbaseCandleSource,
    _parse_candles,
)
from apex.application.crypto_research import (
    CaptureCoinbaseHistory,
    CryptoResearchConflict,
    ExecuteCryptoTrendValidation,
    InMemoryCryptoResearchRepository,
    MaterializeCryptoHistoricalSnapshot,
    RecordCryptoExperimentOutcome,
    RegisterCryptoHypothesis,
    crypto_research_workspace,
)
from apex.domain.crypto_research import (
    AUTHORITY_WARNING,
    COINBASE_GRANULARITY,
    COINBASE_PRODUCT,
    CONSERVATIVE_FEE_BPS,
    CONSERVATIVE_SLIPPAGE_BPS,
    FIXED_DATA_END,
    FIXED_DATA_START,
    FIXED_VALIDATION_END,
    FIXED_VALIDATION_START,
    MAX_BUCKETS_PER_REQUEST,
    CostScenarioName,
    CryptoCapturePartition,
    CryptoCaptureQuality,
    CryptoDailyCandle,
    CryptoResearchError,
    PortfolioName,
    build_prior_day_signals,
    capture_partitions,
    expected_bucket_starts,
)
from apex.domain.research_evidence import (
    CaptureStatus,
    EvidenceClass,
    ExperimentOutcomeDecision,
)
from apex.operator_api import _retained_crypto_research_workspace, app
from scripts.dev import parser

NOW = datetime(2026, 7, 29, 16, tzinfo=UTC)


def _completed_fixture():
    repository = InMemoryCryptoResearchRepository()
    capture_service = CaptureCoinbaseHistory(repository, FixtureCoinbaseCandleSource())
    capture = capture_service.start(
        capture_id="crypto-fixture-capture-v1",
        start=FIXED_DATA_START,
        end=FIXED_DATA_END,
        requested_at=NOW,
    )
    while capture.status is CaptureStatus.RUNNING:
        capture = capture_service.resume(
            capture_id=capture.capture_id,
            requested_at=NOW + timedelta(seconds=capture.request_count),
        )
    return repository, capture


def _full_fixture_run():
    repository, capture = _completed_fixture()
    hypothesis, specification = RegisterCryptoHypothesis(repository).execute(
        hypothesis_id="btc-usd-daily-trend",
        revision=1,
        actor="test",
        created_at=NOW,
    )
    snapshot = MaterializeCryptoHistoricalSnapshot(repository).execute(
        snapshot_id="crypto-fixture-snapshot-v1",
        capture_id=capture.capture_id,
        created_at=NOW + timedelta(minutes=1),
    )
    run = ExecuteCryptoTrendValidation(repository).execute(
        run_id="crypto-fixture-run-v1",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        snapshot_id=snapshot.snapshot_id,
        code_revision="5ce100232265dce713a392659c10e78c7e7abb39",
        executed_at=NOW + timedelta(minutes=2),
    )
    return repository, specification, snapshot, run


def test_fixed_window_partitions_are_exact_and_bounded() -> None:
    partitions = capture_partitions(FIXED_DATA_START, FIXED_DATA_END)
    assert len(partitions) == 3
    assert tuple(item.expected_buckets for item in partitions) == (300, 300, 130)
    assert partitions[0].start == FIXED_DATA_START
    assert partitions[-1].end == FIXED_DATA_END
    assert all(item.expected_buckets <= MAX_BUCKETS_PER_REQUEST for item in partitions)
    with pytest.raises(CryptoResearchError, match="EXCEEDS_REQUEST_BOUNDS"):
        capture_partitions(FIXED_DATA_START, FIXED_DATA_START + timedelta(days=1201))


def test_public_contract_parser_preserves_decimals_hash_input_and_missingness() -> None:
    partition = CryptoCapturePartition.create(
        ordinal=0,
        start=FIXED_DATA_START,
        end=FIXED_DATA_START + timedelta(days=2),
    )
    raw = json.dumps(
        {
            "candles": [
                {
                    "start": str(int(FIXED_DATA_START.timestamp())),
                    "low": "59000.10",
                    "high": "61000.20",
                    "open": "60000.30",
                    "close": "60500.40",
                    "volume": "123.456",
                }
            ]
        }
    ).encode()
    candles, missing, rejected, duplicates = _parse_candles(raw, partition=partition)
    assert candles[0].open == Decimal("60000.30")
    assert missing == (FIXED_DATA_START + timedelta(days=1),)
    assert rejected == duplicates == 0
    assert hashlib.sha256(raw).hexdigest()
    assert COINBASE_PUBLIC_HOST == "api.coinbase.com"


def test_parser_quarantines_invalid_ohlc_and_conflicting_duplicates_fail() -> None:
    partition = CryptoCapturePartition.create(
        ordinal=0,
        start=FIXED_DATA_START,
        end=FIXED_DATA_START + timedelta(days=1),
    )
    invalid = {
        "start": str(int(FIXED_DATA_START.timestamp())),
        "low": "10",
        "high": "9",
        "open": "10",
        "close": "10",
        "volume": "1",
    }
    candles, missing, rejected, duplicates = _parse_candles(
        json.dumps({"candles": [invalid]}).encode(), partition=partition
    )
    assert candles == ()
    assert missing == (FIXED_DATA_START,)
    assert rejected == 1
    assert duplicates == 0

    first = {**invalid, "high": "11"}
    second = {**first, "close": "10.5"}
    with pytest.raises(ValueError, match="DUPLICATE_CONTENT_CONFLICT"):
        _parse_candles(
            json.dumps({"candles": [first, second]}).encode(),
            partition=partition,
        )


def test_fixture_capture_is_resumable_immutable_and_exactly_complete() -> None:
    repository, capture = _completed_fixture()
    assert capture.evidence_class is EvidenceClass.FIXTURE
    assert capture.status is CaptureStatus.COMPLETED
    assert capture.quality is CryptoCaptureQuality.VALID
    assert capture.request_count == capture.completed_partitions == 3
    assert capture.expected_buckets == capture.accepted_buckets == 730
    assert not capture.missing_bucket_starts
    assert len(repository.checkpoints(capture.capture_id)) == 3
    assert len(repository.candles(capture.capture_id)) == 730
    assert len({item.bucket_start for item in repository.candles(capture.capture_id)}) == 730
    replay = CaptureCoinbaseHistory(repository, FixtureCoinbaseCandleSource()).resume(
        capture_id=capture.capture_id, requested_at=NOW + timedelta(hours=1)
    )
    assert replay.state_hash == capture.state_hash


def test_fixture_and_genuine_capture_identities_cannot_mix() -> None:
    repository, capture = _completed_fixture()

    class GenuineFixtureSource(FixtureCoinbaseCandleSource):
        evidence_class = EvidenceClass.HISTORICAL_PUBLIC

    genuine_source = GenuineFixtureSource()
    with pytest.raises(CryptoResearchConflict, match="EVIDENCE_CLASS_CONFLICT"):
        CaptureCoinbaseHistory(repository, genuine_source).resume(
            capture_id=capture.capture_id,
            requested_at=NOW,
        )


def test_snapshot_manifest_is_immutable_and_raw_lineage_is_retained() -> None:
    repository, capture = _completed_fixture()
    service = MaterializeCryptoHistoricalSnapshot(repository)
    snapshot = service.execute(
        snapshot_id="snapshot-one",
        capture_id=capture.capture_id,
        created_at=NOW,
    )
    replay = service.execute(
        snapshot_id=snapshot.snapshot_id,
        capture_id=capture.capture_id,
        created_at=NOW + timedelta(days=1),
    )
    assert replay == snapshot
    assert len(snapshot.candle_hashes) == 730
    assert len(snapshot.response_hashes) == 3
    assert all(
        repository.raw_response(item.attempt_id) is not None
        for item in repository.attempts(capture.capture_id)
    )


def test_signals_use_only_prior_completed_closes_and_missing_days_fail_closed() -> None:
    repository, capture = _completed_fixture()
    candles = repository.candles(capture.capture_id)
    signals = build_prior_day_signals(
        candles,
        validation_start=FIXED_VALIDATION_START,
        validation_end=FIXED_VALIDATION_END,
    )
    assert len(signals) == 365
    assert all(item.information_through == item.signal_for - timedelta(days=1) for item in signals)
    validation_day = FIXED_VALIDATION_START + timedelta(days=20)
    mutated_current = tuple(
        replace(item, close=item.close * Decimal("100"))
        if item.bucket_start == validation_day
        else item
        for item in candles
    )
    mutated_signals = build_prior_day_signals(
        mutated_current,
        validation_start=FIXED_VALIDATION_START,
        validation_end=FIXED_VALIDATION_END,
    )
    original = next(item for item in signals if item.signal_for == validation_day)
    changed = next(item for item in mutated_signals if item.signal_for == validation_day)
    assert changed.fast_average == original.fast_average
    assert changed.slow_average == original.slow_average
    missing_prior = tuple(
        item for item in candles if item.bucket_start != FIXED_VALIDATION_START - timedelta(days=1)
    )
    with pytest.raises(CryptoResearchError, match="PRIOR_BUCKET_MISSING"):
        build_prior_day_signals(
            missing_prior,
            validation_start=FIXED_VALIDATION_START,
            validation_end=FIXED_VALIDATION_END,
        )


def test_experiment_metrics_costs_baselines_and_replay_are_deterministic() -> None:
    repository, specification, snapshot, run = _full_fixture_run()
    assert specification.product_id == COINBASE_PRODUCT
    assert specification.granularity == COINBASE_GRANULARITY
    assert specification.conservative_fee_bps == CONSERVATIVE_FEE_BPS
    assert specification.conservative_slippage_bps == CONSERVATIVE_SLIPPAGE_BPS
    assert snapshot.start == FIXED_DATA_START
    assert snapshot.end == FIXED_DATA_END
    assert run.complete
    assert len(run.metrics) == 6
    assert {item.portfolio for item in run.metrics} == set(PortfolioName)
    assert {item.scenario for item in run.metrics} == set(CostScenarioName)
    assert all(item.starting_notional == Decimal("10000") for item in run.metrics)
    zero_trend = next(
        item
        for item in run.metrics
        if item.portfolio is PortfolioName.TREND_RULE
        and item.scenario is CostScenarioName.DIAGNOSTIC_ZERO_COST
    )
    costly_trend = next(
        item
        for item in run.metrics
        if item.portfolio is PortfolioName.TREND_RULE
        and item.scenario is CostScenarioName.CONSERVATIVE_RESEARCH
    )
    assert costly_trend.cost_paid > zero_trend.cost_paid == 0
    assert costly_trend.net_return <= zero_trend.net_return
    assert costly_trend.total_turnover >= 0
    assert Decimal(0) <= costly_trend.exposure_percentage <= Decimal(100)
    replay = ExecuteCryptoTrendValidation(repository).replay(run.run_id)
    assert replay.result_hash == run.result_hash
    assert replay.canonical_hash == run.canonical_hash


def test_experiment_rejects_snapshot_outside_the_frozen_window() -> None:
    repository, _, snapshot, _ = _full_fixture_run()
    repository._snapshots[snapshot.snapshot_id] = replace(
        snapshot,
        start=FIXED_DATA_START + timedelta(days=1),
    )
    with pytest.raises(CryptoResearchConflict, match="FIXED_WINDOW_MISMATCH"):
        ExecuteCryptoTrendValidation(repository).execute(
            run_id="wrong-window-run",
            hypothesis_version_id=repository.hypotheses()[0][0].hypothesis_version_id,
            snapshot_id=snapshot.snapshot_id,
            code_revision="5ce100232265dce713a392659c10e78c7e7abb39",
            executed_at=NOW,
        )


def test_outcome_and_workspace_create_no_authority_or_financial_state() -> None:
    repository, _, _, run = _full_fixture_run()
    outcome = RecordCryptoExperimentOutcome(repository).execute(
        outcome_id="crypto-fixture-outcome-v1",
        run_id=run.run_id,
        actor="test",
        decided_at=NOW + timedelta(minutes=3),
    )
    assert outcome.decision in {
        ExperimentOutcomeDecision.DEFERRED,
        ExperimentOutcomeDecision.REJECTED,
    }
    assert not outcome.paper_eligible
    assert not outcome.live_eligible
    workspace = crypto_research_workspace(repository)
    assert workspace["candle_count"] == 730
    assert workspace["orders"] == workspace["positions"] == 0
    assert workspace["reservations"] == workspace["broker_writes"] == 0
    assert workspace["journals"] == 0
    assert workspace["authority"] == "NONE"
    assert AUTHORITY_WARNING in workspace["warnings"]


def test_decimal_candles_reject_floats_and_out_of_range_values() -> None:
    partition = capture_partitions(FIXED_DATA_START, FIXED_DATA_END)[0]
    with pytest.raises(CryptoResearchError, match="DECIMAL_INVALID"):
        CryptoDailyCandle.create(
            capture_id="capture",
            attempt_id="attempt",
            evidence_class=EvidenceClass.FIXTURE,
            bucket_start=partition.start,
            open=1.0,
            high="2",
            low="1",
            close="1",
            volume="1",
            ingested_at=NOW,
            raw_response_hash="a" * 64,
        )


def test_empty_workspace_is_useful_and_explicit() -> None:
    workspace = crypto_research_workspace(InMemoryCryptoResearchRepository())
    assert workspace["captures"] == ()
    assert workspace["snapshots"] == ()
    assert workspace["runs"] == ()
    assert workspace["outcomes"] == ()
    assert workspace["paper_eligible"] is False
    assert workspace["live_eligible"] is False
    assert len(expected_bucket_starts(FIXED_VALIDATION_START, FIXED_VALIDATION_END)) == 365


def test_exact_id_crypto_cli_surface_requires_explicit_scope() -> None:
    start = parser().parse_args(
        [
            "crypto-capture-start",
            "--capture-id",
            "capture-one",
            "--mode",
            "fixture",
            "--product",
            "BTC-USD",
            "--start",
            "2024-07-01T00:00:00Z",
            "--end",
            "2026-07-01T00:00:00Z",
        ]
    )
    assert start.capture_id == "capture-one"
    assert start.product == "BTC-USD"
    replay = parser().parse_args(["crypto-experiment-replay", "--run-id", "run-one"])
    assert replay.run_id == "run-one"


def test_read_only_api_routes_and_populated_workspace_are_exposed() -> None:
    methods = {
        (route.path, method) for route in app.routes for method in getattr(route, "methods", ())
    }
    assert ("/api/v1/crypto-research/workspace", "GET") in methods
    assert ("/api/v1/crypto-research/captures/{capture_id}", "GET") in methods
    assert ("/api/v1/crypto-research/snapshots/{snapshot_id}", "GET") in methods
    assert ("/api/v1/crypto-research/runs/{run_id}", "GET") in methods
    assert ("/api/v1/crypto-research/outcomes/{outcome_id}", "GET") in methods
    repository, _, _, _ = _full_fixture_run()
    workspace = _retained_crypto_research_workspace(repository)
    assert workspace["candle_count"] == 730
    assert workspace["authority"] == "NONE"
    assert workspace["paper_eligible"] is False
