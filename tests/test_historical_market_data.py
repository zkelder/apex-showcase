"""Pure-domain proofs for historical market-data evidence: capture-plan
determinism, checkpoint chaining, dataset-snapshot content addressing,
point-in-time/anti-lookahead guards, and the reusable Decimal calculations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.historical_market_data import (
    HistoricalCaptureChunk,
    HistoricalCaptureChunkState,
    HistoricalCapturePlan,
    HistoricalCapturePlanState,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalDataQualityReport,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
    HistoricalMarketDataError,
    PointInTimeError,
    PointInTimeUniverseState,
    SecurityCaptureOutcome,
    assert_no_lookahead,
    cross_sectional_rank,
    derive_plan_state,
    historical_instrument_id,
    log_return,
    max_drawdown,
    observation_view_from_crypto_daily_candle,
    observation_view_from_securities_bar,
    point_in_time_slice,
    rolling_correlation,
    rolling_liquidity,
    rolling_volatility,
    simple_return,
)
from apex.domain.real_securities_data import SecuritiesSurvivorshipState
from apex.domain.research_evidence import EvidenceClass
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
)

NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


def _policy(**overrides: object) -> HistoricalCapturePolicy:
    defaults: dict[str, object] = {
        "provider": "ALPACA_MARKET_DATA_V2",
        "start_date": "2020-01-01",
        "end_date": "2020-12-31",
        "chunk_size": 2,
        "maximum_instruments": 10,
        "maximum_requests": 10,
        "maximum_bytes": 5_000_000,
    }
    defaults.update(overrides)
    return HistoricalCapturePolicy.create(**defaults)  # type: ignore[arg-type]


def _member(
    stable_id: str, symbol: str, asset_class: str = "COMMON_STOCK"
) -> HistoricalCaptureUniverseMember:
    return HistoricalCaptureUniverseMember.create(
        stable_security_id=stable_id, symbol_at_capture=symbol, asset_class=asset_class
    )


def _plan(
    members: list[HistoricalCaptureUniverseMember], **overrides: object
) -> HistoricalCapturePlan:
    policy = overrides.pop("policy", None) or _policy(**overrides.pop("policy_overrides", {}))  # type: ignore[arg-type]
    return HistoricalCapturePlan.draft(
        policy=policy,  # type: ignore[arg-type]
        universe=members,
        evidence_class=overrides.pop("evidence_class", EvidenceClass.FIXTURE),  # type: ignore[arg-type]
        operator_action_id=overrides.pop("operator_action_id", "op-1"),  # type: ignore[arg-type]
        idempotency_key=overrides.pop("idempotency_key", "idem-1"),  # type: ignore[arg-type]
        created_at=overrides.pop("created_at", NOW),  # type: ignore[arg-type]
    )


def _bar(
    day: int,
    close: object,
    *,
    instrument_id: str = "instrument-1",
    availability_delay_minutes: int = 30,
) -> SecuritiesBarEvidence:
    start = datetime(2020, 1, day, tzinfo=UTC)
    end = start + timedelta(days=1)
    parsed_close = Decimal(str(close))
    return SecuritiesBarEvidence(
        instrument_id=instrument_id,
        interval=SecuritiesBarInterval.ONE_DAY,
        adjustment_state=SecuritiesAdjustmentState.RAW,
        period_start=start,
        period_end=end,
        available_at=end + timedelta(minutes=availability_delay_minutes),
        open=parsed_close,
        high=parsed_close + 1,
        low=parsed_close - 1,
        close=parsed_close,
        volume=1000,
        source_identity="TEST_SOURCE",
    )


# --------------------------------------------------------------------------
# Stable-identity instrument anchoring
# --------------------------------------------------------------------------


def test_historical_instrument_id_is_pure_and_stable_id_anchored() -> None:
    first = historical_instrument_id(stable_security_id="stable_AAA", asset_class="COMMON_STOCK")
    second = historical_instrument_id(stable_security_id="stable_AAA", asset_class="COMMON_STOCK")
    assert first == second
    different_class = historical_instrument_id(stable_security_id="stable_AAA", asset_class="ETF")
    assert different_class != first
    different_stable_id = historical_instrument_id(
        stable_security_id="stable_BBB", asset_class="COMMON_STOCK"
    )
    assert different_stable_id != first


def test_historical_instrument_id_requires_identity() -> None:
    with pytest.raises(HistoricalMarketDataError):
        historical_instrument_id(stable_security_id="", asset_class="COMMON_STOCK")
    with pytest.raises(HistoricalMarketDataError):
        historical_instrument_id(stable_security_id="stable_AAA", asset_class="")


# --------------------------------------------------------------------------
# Capture policy / plan / chunking determinism
# --------------------------------------------------------------------------


def test_capture_policy_rejects_inverted_date_range() -> None:
    with pytest.raises(HistoricalMarketDataError):
        _policy(start_date="2020-12-31", end_date="2020-01-01")


def test_capture_policy_only_supports_daily_raw_v1() -> None:
    with pytest.raises(HistoricalMarketDataError):
        _policy(bar_interval=SecuritiesBarInterval.ONE_HOUR)


def test_capture_policy_rejects_non_raw_adjustment() -> None:
    """The pipeline (HistoricalAlpacaCanonicalizer/normalizer) always
    produces RAW bars; a policy claiming otherwise would silently mislabel
    every captured bar, so it must be rejected at construction."""

    with pytest.raises(HistoricalMarketDataError):
        _policy(adjustment_state=SecuritiesAdjustmentState.SPLIT_ADJUSTED)
    with pytest.raises(HistoricalMarketDataError):
        _policy(adjustment_state=SecuritiesAdjustmentState.TOTAL_RETURN_ADJUSTED)


def test_capture_policy_identity_changes_with_provider() -> None:
    """Provider is part of the policy's identity payload even though only
    one provider is genuinely supported today -- if a second provider is
    ever added, two otherwise-identical policies naming different providers
    must never collide."""

    same_provider = _policy(provider="ALPACA_MARKET_DATA_V2")
    other_provider = _policy(provider="A_DIFFERENT_PROVIDER_V1")
    assert same_provider.policy_id != other_provider.policy_id


def test_capture_plan_is_deterministic_and_chunked() -> None:
    members = [_member(f"stable_{i}", f"SYM{i}") for i in range(5)]
    plan = _plan(members)
    plan_again = _plan(members)
    assert plan.plan_id == plan_again.plan_id
    assert plan.canonical_input_hash == plan_again.canonical_input_hash
    assert len(plan.chunk_plan) == 3  # ceil(5 / chunk_size=2)
    assert plan.state is HistoricalCapturePlanState.DRAFT
    # universe is sorted by stable_security_id regardless of input order
    assert [item.stable_security_id for item in plan.universe] == sorted(
        item.stable_security_id for item in plan.universe
    )


def test_capture_plan_universe_is_deduplicated_by_stable_id() -> None:
    members = [_member("stable_0", "SYM0"), _member("stable_0", "SYM0")]
    with pytest.raises(HistoricalMarketDataError):
        _plan(members)


def test_capture_plan_rejects_universe_exceeding_policy_bound() -> None:
    members = [_member(f"stable_{i}", f"SYM{i}") for i in range(3)]
    policy = _policy(maximum_instruments=2)
    with pytest.raises(HistoricalMarketDataError):
        _plan(members, policy=policy)


def test_capture_plan_identity_changes_with_universe_or_policy() -> None:
    members = [_member(f"stable_{i}", f"SYM{i}") for i in range(3)]
    base = _plan(members)
    changed_universe = _plan([*members, _member("stable_extra", "SYME")])
    assert changed_universe.plan_id != base.plan_id
    changed_policy = _plan(members, policy=_policy(end_date="2021-12-31"))
    assert changed_policy.plan_id != base.plan_id


# --------------------------------------------------------------------------
# Chunk checkpoint/resume lifecycle
# --------------------------------------------------------------------------


def test_chunk_lifecycle_completes_with_result_hash() -> None:
    members = [_member(f"stable_{i}", f"SYM{i}") for i in range(2)]
    plan = _plan(members)
    chunk = HistoricalCaptureChunk.pending(plan=plan, ordinal=0, previous_chunk_hash=None)
    assert chunk.state is HistoricalCaptureChunkState.PENDING
    running = chunk.start(started_at=NOW)
    assert running.state is HistoricalCaptureChunkState.RUNNING
    completed = running.complete(
        operation_id="op-1",
        raw_artifact_set_hash="a" * 64,
        batch_id="batch-1",
        captured_instrument_count=2,
        failed_symbols=(),
        completed_at=NOW,
    )
    assert completed.state is HistoricalCaptureChunkState.COMPLETED
    assert completed.canonical_result_hash is not None


def test_chunk_checkpoint_chain_links_to_prior_result() -> None:
    members = [_member(f"stable_{i}", f"SYM{i}") for i in range(4)]
    plan = _plan(members)
    chunk0 = HistoricalCaptureChunk.pending(plan=plan, ordinal=0, previous_chunk_hash=None)
    completed0 = chunk0.start(started_at=NOW).complete(
        operation_id="op-0",
        raw_artifact_set_hash="a" * 64,
        batch_id="batch-0",
        captured_instrument_count=2,
        failed_symbols=(),
        completed_at=NOW,
    )
    chunk1 = HistoricalCaptureChunk.pending(
        plan=plan, ordinal=1, previous_chunk_hash=completed0.canonical_result_hash
    )
    assert chunk1.previous_chunk_hash == completed0.canonical_result_hash
    # a chunk built with a mismatched (stale/diverged) chain hash is a
    # genuinely different, distinctly-identified chunk -- never silently
    # coerced onto the same identity.
    diverged = HistoricalCaptureChunk.pending(plan=plan, ordinal=1, previous_chunk_hash="f" * 64)
    assert diverged.chunk_id != chunk1.chunk_id


def test_chunk_ordinal_zero_requires_no_previous_hash() -> None:
    members = [_member("stable_0", "SYM0")]
    plan = _plan(members)
    with pytest.raises(HistoricalMarketDataError):
        HistoricalCaptureChunk.pending(plan=plan, ordinal=0, previous_chunk_hash="f" * 64)


def test_chunk_retry_increments_retry_count_and_attempt_key() -> None:
    members = [_member("stable_0", "SYM0")]
    plan = _plan(members)
    chunk = HistoricalCaptureChunk.pending(plan=plan, ordinal=0, previous_chunk_hash=None)
    running = chunk.start(started_at=NOW)
    blocked = running.fail(
        state=HistoricalCaptureChunkState.CREDENTIALS_REQUIRED,
        failure_code="ALPACA_CREDENTIALS_REQUIRED",
        completed_at=NOW,
        operation_id="reserved-op-1",
    )
    assert blocked.operation_id == "reserved-op-1"
    assert blocked.retry_count == 0
    retried = blocked.start(started_at=NOW + timedelta(minutes=5))
    assert retried.retry_count == 1
    assert retried.attempt_idempotency_key != blocked.attempt_idempotency_key


def test_chunk_completed_state_requires_batch_and_running_precondition() -> None:
    members = [_member("stable_0", "SYM0")]
    plan = _plan(members)
    chunk = HistoricalCaptureChunk.pending(plan=plan, ordinal=0, previous_chunk_hash=None)
    with pytest.raises(HistoricalMarketDataError):
        # cannot complete a chunk that has not started
        chunk.complete(
            operation_id="op-1",
            raw_artifact_set_hash="a" * 64,
            batch_id="batch-1",
            captured_instrument_count=1,
            failed_symbols=(),
            completed_at=NOW,
        )


def test_derive_plan_state_reflects_chunk_states_honestly() -> None:
    assert (
        derive_plan_state(
            (HistoricalCaptureChunkState.PENDING, HistoricalCaptureChunkState.PENDING)
        )
        is HistoricalCapturePlanState.DRAFT
    )
    assert (
        derive_plan_state(
            (HistoricalCaptureChunkState.COMPLETED, HistoricalCaptureChunkState.COMPLETED)
        )
        is HistoricalCapturePlanState.COMPLETE
    )
    # a completed subset alongside anything not-yet-completed is PARTIAL,
    # never silently reported as complete.
    assert (
        derive_plan_state(
            (HistoricalCaptureChunkState.COMPLETED, HistoricalCaptureChunkState.PENDING)
        )
        is HistoricalCapturePlanState.PARTIAL
    )
    assert (
        derive_plan_state(
            (HistoricalCaptureChunkState.COMPLETED, HistoricalCaptureChunkState.FAILED)
        )
        is HistoricalCapturePlanState.PARTIAL
    )
    assert (
        derive_plan_state((HistoricalCaptureChunkState.CREDENTIALS_REQUIRED,))
        is HistoricalCapturePlanState.CREDENTIALS_REQUIRED
    )
    assert (
        derive_plan_state((HistoricalCaptureChunkState.FAILED,))
        is HistoricalCapturePlanState.FAILED
    )
    assert (
        derive_plan_state(
            (HistoricalCaptureChunkState.RUNNING, HistoricalCaptureChunkState.PENDING)
        )
        is HistoricalCapturePlanState.RUNNING
    )
    with pytest.raises(HistoricalMarketDataError):
        derive_plan_state(())


# --------------------------------------------------------------------------
# Dataset snapshot determinism and lineage
# --------------------------------------------------------------------------


def _snapshot(plan: HistoricalCapturePlan, **overrides: object) -> HistoricalDatasetSnapshot:
    defaults: dict[str, object] = {
        "source_batch_ids": ["batch-a", "batch-b"],
        "source_operation_ids": ["op-a"],
        "observation_count": 10,
        "completeness_state": HistoricalDatasetCompletenessState.COMPLETE,
        "earliest_observation_date": "2020-01-02",
        "latest_observation_date": "2020-12-30",
        "created_at": NOW,
    }
    defaults.update(overrides)
    return HistoricalDatasetSnapshot.create(plan=plan, **defaults)  # type: ignore[arg-type]


def test_dataset_snapshot_identity_is_order_independent_but_content_addressed() -> None:
    members = [_member(f"stable_{i}", f"SYM{i}") for i in range(3)]
    plan = _plan(members)
    first = _snapshot(plan, source_batch_ids=["batch-b", "batch-a"])
    second = _snapshot(plan, source_batch_ids=["batch-a", "batch-b"])
    assert first.snapshot_id == second.snapshot_id
    assert (
        first.point_in_time_universe_state
        is PointInTimeUniverseState.POINT_IN_TIME_UNIVERSE_INCOMPLETE
    )
    assert first.survivorship_state is SecuritiesSurvivorshipState.SURVIVORSHIP_LIMITED


def test_dataset_snapshot_identity_changes_with_evidence_or_policy() -> None:
    members = [_member(f"stable_{i}", f"SYM{i}") for i in range(3)]
    plan = _plan(members)
    base = _snapshot(plan)
    changed_batches = _snapshot(plan, source_batch_ids=["batch-a", "batch-c"])
    assert changed_batches.snapshot_id != base.snapshot_id
    changed_count = _snapshot(plan, observation_count=11)
    assert changed_count.snapshot_id != base.snapshot_id
    changed_completeness = _snapshot(
        plan, completeness_state=HistoricalDatasetCompletenessState.PARTIAL
    )
    assert changed_completeness.snapshot_id != base.snapshot_id
    # a different universe/date-range/provider is a different plan entirely,
    # which already changes the snapshot identity through plan.plan_id.
    changed_universe_plan = _plan([*members, _member("stable_extra", "SYME")])
    changed_universe = _snapshot(changed_universe_plan)
    assert changed_universe.snapshot_id != base.snapshot_id
    changed_range_plan = _plan(members, policy=_policy(end_date="2021-06-30"))
    changed_range = _snapshot(changed_range_plan)
    assert changed_range.snapshot_id != base.snapshot_id


def test_dataset_snapshot_allows_zero_observations_when_all_attempts_failed() -> None:
    """The credential-absent/all-failed proof: an attempt was genuinely made
    (operation ids retained) but nothing was captured (no batches)."""

    members = [_member("stable_0", "SYM0")]
    plan = _plan(members)
    snapshot = HistoricalDatasetSnapshot.create(
        plan=plan,
        source_batch_ids=(),
        source_operation_ids=["op-blocked"],
        observation_count=0,
        completeness_state=HistoricalDatasetCompletenessState.PARTIAL,
        earliest_observation_date=None,
        latest_observation_date=None,
        created_at=NOW,
    )
    assert snapshot.observation_count == 0
    assert snapshot.source_batch_ids == ()


def test_dataset_snapshot_requires_at_least_one_attempted_capture() -> None:
    members = [_member("stable_0", "SYM0")]
    plan = _plan(members)
    with pytest.raises(HistoricalMarketDataError):
        HistoricalDatasetSnapshot.create(
            plan=plan,
            source_batch_ids=(),
            source_operation_ids=(),
            observation_count=0,
            completeness_state=HistoricalDatasetCompletenessState.PARTIAL,
            earliest_observation_date=None,
            latest_observation_date=None,
            created_at=NOW,
        )


def test_dataset_snapshot_rejects_inconsistent_date_bounds_and_count() -> None:
    members = [_member("stable_0", "SYM0")]
    plan = _plan(members)
    with pytest.raises(HistoricalMarketDataError):
        _snapshot(plan, observation_count=0, earliest_observation_date="2020-01-02")
    with pytest.raises(HistoricalMarketDataError):
        _snapshot(
            plan, earliest_observation_date="2020-12-30", latest_observation_date="2020-01-02"
        )


# --------------------------------------------------------------------------
# Data-quality report
# --------------------------------------------------------------------------


def test_quality_report_counts_captured_vs_requested_and_is_deterministic() -> None:
    members = [_member("stable_0", "SYM0"), _member("stable_1", "SYM1")]
    plan = _plan(members)
    snapshot = _snapshot(plan)
    outcomes = [
        ("stable_0", SecurityCaptureOutcome.CAPTURED, "bars retained"),
        ("stable_1", SecurityCaptureOutcome.PROVIDER_NO_DATA, "no bars returned"),
    ]
    report = HistoricalDataQualityReport.create(
        snapshot=snapshot,
        requested_security_count=2,
        security_outcomes=outcomes,
        duplicate_timestamp_count=0,
        missing_session_count=0,
        invalid_ohlc_count=0,
        nonpositive_price_count=0,
        volume_anomaly_count=0,
        raw_capture_hashes=["h2", "h1"],
        created_at=NOW,
    )
    assert report.captured_security_count == 1
    report_again = HistoricalDataQualityReport.create(
        snapshot=snapshot,
        requested_security_count=2,
        security_outcomes=outcomes,
        duplicate_timestamp_count=0,
        missing_session_count=0,
        invalid_ohlc_count=0,
        nonpositive_price_count=0,
        volume_anomaly_count=0,
        raw_capture_hashes=["h1", "h2"],  # different order, identical set
        created_at=NOW,
    )
    assert report.report_id == report_again.report_id


def test_quality_report_rejects_duplicate_security_outcome() -> None:
    members = [_member("stable_0", "SYM0")]
    plan = _plan(members)
    snapshot = _snapshot(plan)
    with pytest.raises(HistoricalMarketDataError):
        HistoricalDataQualityReport.create(
            snapshot=snapshot,
            requested_security_count=1,
            security_outcomes=[
                ("stable_0", SecurityCaptureOutcome.CAPTURED, "a"),
                ("stable_0", SecurityCaptureOutcome.PROVIDER_NO_DATA, "b"),
            ],
            duplicate_timestamp_count=0,
            missing_session_count=0,
            invalid_ohlc_count=0,
            nonpositive_price_count=0,
            volume_anomaly_count=0,
            raw_capture_hashes=(),
            created_at=NOW,
        )


# --------------------------------------------------------------------------
# Point-in-time / anti-lookahead safety (first-class research-safety boundary)
# --------------------------------------------------------------------------


def test_assert_no_lookahead_rejects_future_evidence() -> None:
    bars = [_bar(day, 100 + day) for day in range(1, 6)]
    as_of = bars[2].available_at  # excludes bars 3 and 4
    with pytest.raises(PointInTimeError):
        assert_no_lookahead(bars, as_of=as_of)


def test_assert_no_lookahead_accepts_evidence_available_exactly_at_as_of() -> None:
    bars = [_bar(day, 100 + day) for day in range(1, 4)]
    as_of = bars[-1].available_at
    ordered = assert_no_lookahead(bars, as_of=as_of)
    assert [bar.period_start for bar in ordered] == sorted(bar.period_start for bar in bars)


def test_point_in_time_slice_never_raises_and_excludes_future_evidence() -> None:
    bars = [_bar(day, 100 + day) for day in range(1, 6)]
    as_of = bars[2].available_at
    sliced = point_in_time_slice(bars, as_of=as_of)
    assert all(bar.available_at <= as_of for bar in sliced)
    assert len(sliced) == 3


def test_calculations_reject_lookahead_inputs() -> None:
    bars = [_bar(day, 100 + day) for day in range(1, 6)]
    as_of = bars[2].available_at
    for calculation, kwargs in (
        (simple_return, {}),
        (log_return, {}),
        (max_drawdown, {}),
        (rolling_volatility, {"window": 2}),
        (rolling_liquidity, {"window": 2}),
    ):
        with pytest.raises(PointInTimeError):
            calculation(bars, as_of=as_of, **kwargs)  # type: ignore[operator]


def test_future_outcome_is_isolated_from_feature_generation() -> None:
    """A feature computed at as_of=day5 must be identical whether or not a
    day10 'future outcome' bar exists in the caller's pool -- proving later
    outcomes cannot leak into an earlier decision's features."""

    bars_without_future = [_bar(day, 100 + day) for day in range(1, 6)]
    as_of = bars_without_future[-1].available_at
    feature_without_future = simple_return(
        point_in_time_slice(bars_without_future, as_of=as_of), as_of=as_of
    )
    bars_with_future_outcome = [*bars_without_future, _bar(10, 999)]
    feature_with_future = simple_return(
        point_in_time_slice(bars_with_future_outcome, as_of=as_of), as_of=as_of
    )
    assert feature_without_future == feature_with_future


# --------------------------------------------------------------------------
# Reusable historical calculations
# --------------------------------------------------------------------------


def test_simple_and_log_return_over_known_series() -> None:
    bars = [_bar(1, 100), _bar(2, 110)]
    as_of = bars[-1].available_at
    assert simple_return(bars, as_of=as_of) == Decimal("0.10000000")
    log = log_return(bars, as_of=as_of)
    assert log is not None
    assert Decimal("0.09") < log < Decimal("0.10")


def test_returns_require_at_least_two_observations() -> None:
    bars = [_bar(1, 100)]
    as_of = bars[-1].available_at
    assert simple_return(bars, as_of=as_of) is None
    assert log_return(bars, as_of=as_of) is None


def test_rolling_volatility_requires_full_window_and_is_nonnegative() -> None:
    bars = [_bar(day, 100 + (day % 3)) for day in range(1, 6)]
    as_of = bars[-1].available_at
    assert rolling_volatility(bars, as_of=as_of, window=10) is None  # insufficient history
    vol = rolling_volatility(bars, as_of=as_of, window=3)
    assert vol is not None and vol >= 0


def test_rolling_liquidity_is_mean_dollar_volume() -> None:
    bars = [_bar(day, 10) for day in range(1, 4)]  # close=10, volume=1000 each
    as_of = bars[-1].available_at
    liquidity = rolling_liquidity(bars, as_of=as_of, window=3)
    assert liquidity == Decimal("10000.00000000")


def test_max_drawdown_over_known_series() -> None:
    bars = [_bar(1, 100), _bar(2, 120), _bar(3, 90), _bar(4, 100)]
    as_of = bars[-1].available_at
    drawdown = max_drawdown(bars, as_of=as_of)
    # peak 120 -> trough 90 => (120-90)/120 = 0.25
    assert drawdown == Decimal("0.25000000")


def test_max_drawdown_on_sparse_single_observation_is_zero() -> None:
    bars = [_bar(1, 100)]
    as_of = bars[-1].available_at
    assert max_drawdown(bars, as_of=as_of) == Decimal("0")


def test_cross_sectional_rank_is_tie_aware_and_bounded() -> None:
    values = {"a": Decimal("1"), "b": Decimal("2"), "c": Decimal("2"), "d": Decimal("3")}
    assert cross_sectional_rank(values, instrument_id="a") == Decimal("0.12500000")
    assert cross_sectional_rank(values, instrument_id="d") == Decimal("0.87500000")
    assert cross_sectional_rank(values, instrument_id="b") == cross_sectional_rank(
        values, instrument_id="c"
    )
    with pytest.raises(HistoricalMarketDataError):
        cross_sectional_rank(values, instrument_id="missing")
    with pytest.raises(HistoricalMarketDataError):
        cross_sectional_rank({}, instrument_id="a")


def test_rolling_correlation_of_linear_series_is_near_one() -> None:
    bars_a = [_bar(day, 100 + day, instrument_id="a") for day in range(1, 11)]
    bars_b = [_bar(day, 200 + 2 * day, instrument_id="b") for day in range(1, 11)]
    as_of = bars_a[-1].available_at
    correlation = rolling_correlation(bars_a, bars_b, as_of=as_of, window=5)
    assert correlation is not None
    assert Decimal("0.99") < correlation <= Decimal("1")


def test_rolling_correlation_requires_sufficient_overlap() -> None:
    bars_a = [_bar(day, 100 + day, instrument_id="a") for day in range(1, 3)]
    bars_b = [_bar(day, 200 + day, instrument_id="b") for day in range(1, 3)]
    as_of = bars_a[-1].available_at
    assert rolling_correlation(bars_a, bars_b, as_of=as_of, window=5) is None


def test_rolling_correlation_returns_none_for_constant_series() -> None:
    bars_a = [_bar(day, 100, instrument_id="a") for day in range(1, 8)]
    bars_b = [_bar(day, 200 + day, instrument_id="b") for day in range(1, 8)]
    as_of = bars_a[-1].available_at
    assert rolling_correlation(bars_a, bars_b, as_of=as_of, window=5) is None


# --------------------------------------------------------------------------
# Cross-venue replay seam
# --------------------------------------------------------------------------


def test_observation_view_from_securities_bar_preserves_point_in_time_fields() -> None:
    bar = _bar(1, 105)
    view = observation_view_from_securities_bar(bar)
    assert view.venue == "EQUITY_ETF"
    assert view.event_at == bar.period_end
    assert view.available_at == bar.available_at
    assert view.close == bar.close


def test_observation_view_from_crypto_daily_candle_is_conservatively_available() -> None:
    bucket_start = datetime(2020, 1, 1, tzinfo=UTC)
    view = observation_view_from_crypto_daily_candle(
        product_id="BTC-USD",
        bucket_start=bucket_start,
        open=Decimal(100),
        high=Decimal(110),
        low=Decimal(90),
        close=Decimal(105),
        volume=Decimal(5),
        provider="COINBASE",
    )
    assert view.venue == "CRYPTO"
    assert view.event_at == bucket_start
    # conservative availability: no earlier than the UTC end of its own day
    assert view.available_at == bucket_start.replace(hour=23, minute=59, second=59)
    assert view.available_at > view.event_at
