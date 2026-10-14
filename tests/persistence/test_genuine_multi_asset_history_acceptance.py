"""Genuine (non-fixture) retained-evidence acceptance for the multi-asset
cohort/quality-gate/cross-market-dataset bring-up (milestone sections 4-10).

Mirrors the exact pattern ``test_historical_replay_genuine_acceptance.py``
and ``test_research_methodology_genuine_acceptance.py`` already established:
this runs against the ephemeral ``postgres_engine`` test database (wiped
before every test -- see ``tests/persistence/conftest.py``), so it uses
whatever genuine evidence already exists *in that database* honestly,
skipping with a clear reason rather than fabricating a dataset when none
does. The actual genuine multi-asset capture this milestone produced lives
in the durable local ``apex`` database (via the real Alpaca provider
boundary and ``scripts/dev.py`` commands), not this test database -- see
the milestone's final report for those artifact ids.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine

from apex.adapters.crypto_research_postgres import PostgresCryptoResearchRepository
from apex.adapters.genuine_multi_asset_history_postgres import (
    PostgresGenuineMultiAssetHistoryRepository,
)
from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.securities_postgres import PostgresSecuritiesRepository
from apex.adapters.xnys_calendar import ReviewedXNYSCalendar
from apex.application.genuine_multi_asset_history import (
    BuildCrossMarketDataset,
    BuildCrossMarketDatasetCommand,
)
from apex.domain.corporate_action_consistency import (
    SplitConsistencyOutcome,
    applicable_known_splits,
    check_split_consistency,
)
from apex.domain.genuine_data_quality_gate import (
    GenuineCohortQualityGateReport,
    assess_instrument_quality,
)
from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def _genuine_non_fixture_plan(historical_repository: PostgresHistoricalMarketDataRepository):
    for plan in historical_repository.list_plans():
        if plan.evidence_class.value != "GENUINE_PUBLIC":
            continue
        chunks = historical_repository.list_chunks(plan.plan_id)
        if chunks and any(item.state.value == "COMPLETED" for item in chunks):
            return plan
    return None


def test_genuine_quality_gate_runs_honestly_over_whatever_real_capture_exists(
    postgres_engine: Engine,
) -> None:
    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    securities_repository = PostgresSecuritiesRepository(postgres_engine)
    plan = _genuine_non_fixture_plan(historical_repository)
    if plan is None:
        pytest.skip(
            "GENUINE_MULTI_ASSET_DATASET_NOT_ADEQUATE: no retained genuine (GENUINE_PUBLIC, "
            "at least one COMPLETED chunk) historical capture plan exists in this environment; "
            "reporting honestly rather than fabricating one."
        )
    snapshots = [
        item
        for item in historical_repository.list_snapshots()
        if item.plan_id == plan.plan_id and item.observation_count > 0
    ]
    if not snapshots:
        pytest.skip(
            "GENUINE_MULTI_ASSET_DATASET_NOT_ADEQUATE: the genuine plan has no non-empty "
            "materialized snapshot in this environment."
        )
    snapshot = snapshots[-1]
    bars_by_instrument: dict[str, list] = {}
    for batch_id in snapshot.source_batch_ids:
        _instruments, _sessions, bars, _actions = securities_repository.get_dataset(batch_id)
        for bar in bars:
            bars_by_instrument.setdefault(bar.instrument_id, []).append(bar)
    calendar = ReviewedXNYSCalendar(
        start_date=plan.policy.start_date, end_date=plan.policy.end_date
    )

    assessments = tuple(
        assess_instrument_quality(
            stable_security_id=member.stable_security_id,
            symbol=member.symbol_at_capture,
            instrument_id=member.instrument_id,
            bars=bars_by_instrument.get(member.instrument_id, ()),
            calendar_sessions=calendar.sessions,
            source_batch_ids=snapshot.source_batch_ids,
        )
        for member in plan.universe
    )
    report = GenuineCohortQualityGateReport.build(
        snapshot_id=snapshot.snapshot_id,
        cohort_policy_id=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id,
        requested_symbols=tuple(member.symbol_at_capture for member in plan.universe),
        assessments=assessments,
        point_in_time_limitations=(
            snapshot.point_in_time_universe_state.value,
            snapshot.survivorship_state.value,
        ),
        source_hashes=snapshot.source_batch_ids,
        created_at=NOW,
    )
    # Internal consistency, never a fabricated total.
    assert (
        report.usable_count
        + report.usable_with_warnings_count
        + report.insufficient_count
        + report.rejected_count
        == report.successful_instrument_count
    )
    assert report.requested_instrument_count == len(plan.universe)
    assert report.successful_instrument_count + len(report.missing_instrument_symbols) == len(
        plan.universe
    )
    # Only usable-or-warned instruments are ever offered to the canonical
    # cohort -- never a rejected or insufficient one.
    canonical = set(report.canonical_research_cohort_symbols())
    rejected_or_insufficient = {
        item.symbol
        for item in report.assessments
        if item.classification.value in ("REJECTED", "INSUFFICIENT")
    }
    assert canonical.isdisjoint(rejected_or_insufficient)


def test_genuine_corporate_action_consistency_over_whatever_real_capture_exists(
    postgres_engine: Engine,
) -> None:
    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    securities_repository = PostgresSecuritiesRepository(postgres_engine)
    plan = _genuine_non_fixture_plan(historical_repository)
    if plan is None:
        pytest.skip(
            "GENUINE_MULTI_ASSET_DATASET_NOT_ADEQUATE: no retained genuine historical capture "
            "plan exists in this environment; reporting honestly rather than fabricating one."
        )
    snapshots = [
        item
        for item in historical_repository.list_snapshots()
        if item.plan_id == plan.plan_id and item.observation_count > 0
    ]
    if not snapshots:
        pytest.skip("GENUINE_MULTI_ASSET_DATASET_NOT_ADEQUATE: no non-empty genuine snapshot.")
    snapshot = snapshots[-1]
    applicable = applicable_known_splits(
        symbols=tuple(member.symbol_at_capture for member in plan.universe),
        coverage_start=plan.policy.start_date,
        coverage_end=plan.policy.end_date,
    )
    if not applicable:
        pytest.skip(
            "GENUINE_MULTI_ASSET_DATASET_NOT_ADEQUATE: none of the cohort's known real splits "
            "fall inside this plan's declared coverage window."
        )
    bars_by_instrument: dict[str, list] = {}
    for batch_id in snapshot.source_batch_ids:
        _instruments, _sessions, bars, _actions = securities_repository.get_dataset(batch_id)
        for bar in bars:
            bars_by_instrument.setdefault(bar.instrument_id, []).append(bar)
    symbol_by_instrument = {
        member.instrument_id: member.symbol_at_capture for member in plan.universe
    }
    checked = 0
    for fact in applicable:
        instrument_id = next(
            (iid for iid, symbol in symbol_by_instrument.items() if symbol == fact.symbol), None
        )
        if instrument_id is None:
            continue
        bars = bars_by_instrument.get(instrument_id, ())
        if not bars:
            continue
        result = check_split_consistency(bars, fact=fact)
        checked += 1
        # Never silently accept a wrong ratio -- an honest coverage gap is
        # fine, an *inconsistent* result on genuinely retained evidence
        # would mean the series is not the raw/unadjusted price it claims.
        assert result.outcome is not SplitConsistencyOutcome.INCONSISTENT_RATIO, (
            f"{fact.symbol} {fact.effective_date}: expected ~{fact.forward_split_ratio}x, "
            f"observed {result.observed_ratio}"
        )
    if checked == 0:
        pytest.skip(
            "GENUINE_MULTI_ASSET_DATASET_NOT_ADEQUATE: no applicable-split symbol was actually "
            "captured with bars in this environment."
        )


def test_genuine_cross_market_dataset_binds_real_equity_and_crypto_if_both_retained(
    postgres_engine: Engine,
) -> None:
    """The full section-10 binding, end to end, against whatever genuine
    equity capture and genuine crypto capture already coexist in this
    environment -- never fabricating either side."""

    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    securities_repository = PostgresSecuritiesRepository(postgres_engine)
    genuine_repository = PostgresGenuineMultiAssetHistoryRepository(postgres_engine)
    crypto_repository = PostgresCryptoResearchRepository(postgres_engine)

    plan = _genuine_non_fixture_plan(historical_repository)
    if plan is None:
        pytest.skip("GENUINE_MULTI_ASSET_DATASET_NOT_ADEQUATE: no genuine equity capture plan.")
    snapshots = [
        item
        for item in historical_repository.list_snapshots()
        if item.plan_id == plan.plan_id and item.observation_count > 0
    ]
    if not snapshots:
        pytest.skip("GENUINE_MULTI_ASSET_DATASET_NOT_ADEQUATE: no non-empty genuine snapshot.")
    snapshot = snapshots[-1]

    crypto_candidates = [
        capture
        for capture in crypto_repository.captures()
        if "fixture" not in capture.capture_id.lower() and capture.status.value == "COMPLETED"
    ]
    adequate_crypto = None
    for capture in crypto_candidates:
        candles = crypto_repository.candles(capture.capture_id)
        if len(candles) >= 42:
            adequate_crypto = (capture, candles)
            break
    if adequate_crypto is None:
        pytest.skip("GENUINE_MULTI_ASSET_DATASET_NOT_ADEQUATE: no adequate genuine crypto capture.")
    crypto_capture, crypto_candles = adequate_crypto

    calendar = ReviewedXNYSCalendar(
        start_date=plan.policy.start_date, end_date=plan.policy.end_date
    )
    bars_by_instrument: dict[str, list] = {}
    for batch_id in snapshot.source_batch_ids:
        _instruments, _sessions, bars, _actions = securities_repository.get_dataset(batch_id)
        for bar in bars:
            bars_by_instrument.setdefault(bar.instrument_id, []).append(bar)
    assessments = tuple(
        assess_instrument_quality(
            stable_security_id=member.stable_security_id,
            symbol=member.symbol_at_capture,
            instrument_id=member.instrument_id,
            bars=bars_by_instrument.get(member.instrument_id, ()),
            calendar_sessions=calendar.sessions,
            source_batch_ids=snapshot.source_batch_ids,
        )
        for member in plan.universe
    )
    report = GenuineCohortQualityGateReport.build(
        snapshot_id=snapshot.snapshot_id,
        cohort_policy_id=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id,
        requested_symbols=tuple(member.symbol_at_capture for member in plan.universe),
        assessments=assessments,
        point_in_time_limitations=(
            snapshot.point_in_time_universe_state.value,
            snapshot.survivorship_state.value,
        ),
        source_hashes=snapshot.source_batch_ids,
        created_at=NOW,
    )
    report = genuine_repository.save_quality_gate_report(report)

    crypto_start = min(candle.bucket_start for candle in crypto_candles).date().isoformat()
    crypto_end = max(candle.bucket_start for candle in crypto_candles).date().isoformat()
    dataset = BuildCrossMarketDataset(
        repository=genuine_repository, historical_repository=historical_repository
    ).execute(
        BuildCrossMarketDatasetCommand(
            equity_snapshot_id=snapshot.snapshot_id,
            cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
            quality_gate_report_id=report.report_id,
            crypto_provider=crypto_candles[0].provider,
            crypto_product_id=crypto_candles[0].product_id,
            crypto_capture_ids=(crypto_capture.capture_id,),
            crypto_coverage_start=crypto_start,
            crypto_coverage_end=crypto_end,
            tmc_component_symbols=("SPY", "TLT", "USO"),
            tmc_source_identity="HISTORICAL_REPLAY_RETAINED",
            alignment_policy_id="CROSS_MARKET_ALIGNMENT_V1",
            created_at=NOW,
        )
    )
    assert dataset.equity_dataset_snapshot_id == snapshot.snapshot_id
    assert dataset.crypto_capture_ids == (crypto_capture.capture_id,)
    # Never a fabricated overlap: either genuinely computed from both real
    # windows, or honestly None when they truly do not intersect.
    if dataset.common_overlap_start is not None:
        assert dataset.common_overlap_start <= dataset.common_overlap_end
        assert dataset.common_overlap_start >= min(snapshot.earliest_observation_date, crypto_start)
    assert (
        genuine_repository.cross_market_dataset_by_id(dataset.cross_market_dataset_id) is not None
    )
