"""Orchestration-level proofs for the genuine multi-asset quality gate and
cross-market dataset binding, against in-memory fakes (no network, no DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apex.application.genuine_multi_asset_history import (
    BuildCrossMarketDataset,
    BuildCrossMarketDatasetCommand,
    BuildQualityGateReport,
    BuildQualityGateReportCommand,
    GenuineMultiAssetHistoryNotFound,
    InMemoryGenuineMultiAssetHistoryRepository,
)
from apex.application.historical_market_data import InMemoryHistoricalMarketDataRepository
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
)
from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesCalendarSession,
)

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


class FakeEvidenceStore:
    def __init__(self) -> None:
        self.datasets: dict[str, tuple] = {}

    def get_dataset(self, batch_id: str) -> tuple:
        return self.datasets[batch_id]


class FakeCalendar:
    def __init__(self, sessions: tuple[SecuritiesCalendarSession, ...]) -> None:
        self._sessions = sessions

    def sessions_between(
        self, *, calendar_identity: str, start_date: str, end_date: str
    ) -> tuple[SecuritiesCalendarSession, ...]:
        return tuple(item for item in self._sessions if start_date <= item.session_date <= end_date)

    def is_session(self, *, calendar_identity: str, session_date: str) -> bool:
        return any(item.session_date == session_date for item in self._sessions)

    def previous_session(self, *, calendar_identity: str, session_date: str):
        values = [item for item in self._sessions if item.session_date < session_date]
        return values[-1] if values else None

    def next_session(self, *, calendar_identity: str, session_date: str):
        return next((item for item in self._sessions if item.session_date > session_date), None)


def _session(day: str) -> SecuritiesCalendarSession:
    opens = datetime.fromisoformat(f"{day}T13:30:00+00:00")
    return SecuritiesCalendarSession.create(
        calendar_identity="XNYS",
        calendar_version="1",
        timezone="America/New_York",
        session_date=day,
        opens_at=opens,
        closes_at=opens + timedelta(hours=6, minutes=30),
        is_early_close=False,
        source_identity="TEST",
    )


def _bar(instrument_id: str, day: str, close: str) -> SecuritiesBarEvidence:
    start = datetime.fromisoformat(f"{day}T13:30:00+00:00")
    end = start + timedelta(hours=6, minutes=30)
    return SecuritiesBarEvidence(
        instrument_id=instrument_id,
        interval=SecuritiesBarInterval.ONE_DAY,
        adjustment_state=SecuritiesAdjustmentState.RAW,
        period_start=start,
        period_end=end,
        available_at=end + timedelta(minutes=30),
        open=close,
        high=close,
        low=close,
        close=close,
        volume="1000",
        source_identity="FAKE_ALPACA",
    )


def _weekdays(start: str, count: int) -> list[str]:
    cursor = datetime.fromisoformat(f"{start}T00:00:00+00:00")
    out: list[str] = []
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.date().isoformat())
        cursor += timedelta(days=1)
    return out


def _setup() -> tuple[
    InMemoryHistoricalMarketDataRepository,
    FakeEvidenceStore,
    FakeCalendar,
    HistoricalDatasetSnapshot,
]:
    days = _weekdays("2024-01-02", 260)
    historical_repository = InMemoryHistoricalMarketDataRepository()
    evidence = FakeEvidenceStore()
    policy = HistoricalCapturePolicy.create(
        provider="ALPACA_MARKET_DATA_V2",
        start_date=days[0],
        end_date=days[-1],
        chunk_size=10,
        maximum_instruments=10,
        maximum_requests=10,
        maximum_bytes=1_000_000,
    )
    universe = (
        HistoricalCaptureUniverseMember.create(
            stable_security_id="stable_AAA", symbol_at_capture="AAA", asset_class="COMMON_STOCK"
        ),
        HistoricalCaptureUniverseMember.create(
            stable_security_id="stable_SPY", symbol_at_capture="SPY", asset_class="ETF"
        ),
    )
    plan = HistoricalCapturePlan.draft(
        policy=policy,
        universe=universe,
        evidence_class="GENUINE_PUBLIC",
        operator_action_id="test-plan",
        idempotency_key="test-plan-v1",
        created_at=NOW,
    )
    historical_repository.save_plan(plan)
    bars = tuple(_bar(member.instrument_id, day, "100") for member in universe for day in days)
    evidence.datasets["batch-1"] = ((), (), bars, ())
    snapshot = HistoricalDatasetSnapshot.create(
        plan=plan,
        source_batch_ids=("batch-1",),
        source_operation_ids=("op-1",),
        observation_count=len(bars),
        completeness_state=HistoricalDatasetCompletenessState.COMPLETE,
        earliest_observation_date=days[0],
        latest_observation_date=days[-1],
        created_at=NOW,
    )
    historical_repository.save_snapshot(snapshot)
    calendar = FakeCalendar(tuple(_session(day) for day in days))
    return historical_repository, evidence, calendar, snapshot


def test_quality_gate_report_covers_every_universe_member() -> None:
    historical_repository, evidence, calendar, snapshot = _setup()
    repository = InMemoryGenuineMultiAssetHistoryRepository()
    report = BuildQualityGateReport(
        repository=repository,
        historical_repository=historical_repository,
        evidence=evidence,
        calendar=calendar,
    ).execute(
        BuildQualityGateReportCommand(
            snapshot_id=snapshot.snapshot_id,
            cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
            created_at=NOW,
        )
    )
    assert report.requested_instrument_count == 2
    assert report.successful_instrument_count == 2
    assert report.usable_count == 2
    assert repository.quality_gate_report_by_id(report.report_id) is not None


def test_quality_gate_report_is_idempotent() -> None:
    historical_repository, evidence, calendar, snapshot = _setup()
    repository = InMemoryGenuineMultiAssetHistoryRepository()
    command = BuildQualityGateReportCommand(
        snapshot_id=snapshot.snapshot_id,
        cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
        created_at=NOW,
    )
    builder = BuildQualityGateReport(
        repository=repository,
        historical_repository=historical_repository,
        evidence=evidence,
        calendar=calendar,
    )
    first = builder.execute(command)
    second = builder.execute(command)
    assert first.report_id == second.report_id
    assert len(repository.list_quality_gate_reports()) == 1


def test_quality_gate_report_missing_snapshot_raises_not_found() -> None:
    historical_repository, evidence, calendar, _snapshot = _setup()
    repository = InMemoryGenuineMultiAssetHistoryRepository()
    with pytest.raises(GenuineMultiAssetHistoryNotFound, match="SNAPSHOT_NOT_FOUND"):
        BuildQualityGateReport(
            repository=repository,
            historical_repository=historical_repository,
            evidence=evidence,
            calendar=calendar,
        ).execute(
            BuildQualityGateReportCommand(
                snapshot_id="does-not-exist",
                cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
                created_at=NOW,
            )
        )


def test_cross_market_dataset_binds_equity_and_crypto_coverage() -> None:
    historical_repository, evidence, calendar, snapshot = _setup()
    repository = InMemoryGenuineMultiAssetHistoryRepository()
    report = BuildQualityGateReport(
        repository=repository,
        historical_repository=historical_repository,
        evidence=evidence,
        calendar=calendar,
    ).execute(
        BuildQualityGateReportCommand(
            snapshot_id=snapshot.snapshot_id,
            cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
            created_at=NOW,
        )
    )
    dataset = BuildCrossMarketDataset(
        repository=repository, historical_repository=historical_repository
    ).execute(
        BuildCrossMarketDatasetCommand(
            equity_snapshot_id=snapshot.snapshot_id,
            cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
            quality_gate_report_id=report.report_id,
            crypto_provider="COINBASE",
            crypto_product_id="BTC-USD",
            crypto_capture_ids=("btc-a",),
            crypto_coverage_start="2020-07-01",
            crypto_coverage_end="2026-06-30",
            tmc_component_symbols=("SPY", "TLT", "USO"),
            tmc_source_identity="HISTORICAL_REPLAY_RETAINED",
            alignment_policy_id="CROSS_MARKET_ALIGNMENT_V1",
            created_at=NOW,
        )
    )
    assert dataset.equity_dataset_snapshot_id == snapshot.snapshot_id
    assert dataset.quality_gate_report_id == report.report_id
    assert dataset.common_overlap_start is not None
    assert repository.cross_market_dataset_by_id(dataset.cross_market_dataset_id) is not None


def test_cross_market_dataset_missing_quality_gate_report_raises_not_found() -> None:
    historical_repository, _evidence, _calendar, snapshot = _setup()
    repository = InMemoryGenuineMultiAssetHistoryRepository()
    with pytest.raises(GenuineMultiAssetHistoryNotFound, match="QUALITY_GATE_REPORT_NOT_FOUND"):
        BuildCrossMarketDataset(
            repository=repository, historical_repository=historical_repository
        ).execute(
            BuildCrossMarketDatasetCommand(
                equity_snapshot_id=snapshot.snapshot_id,
                cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
                quality_gate_report_id="does-not-exist",
                crypto_provider="COINBASE",
                crypto_product_id="BTC-USD",
                crypto_capture_ids=("btc-a",),
                crypto_coverage_start="2020-07-01",
                crypto_coverage_end="2026-06-30",
                tmc_component_symbols=("SPY", "TLT", "USO"),
                tmc_source_identity="HISTORICAL_REPLAY_RETAINED",
                alignment_policy_id="CROSS_MARKET_ALIGNMENT_V1",
                created_at=NOW,
            )
        )
