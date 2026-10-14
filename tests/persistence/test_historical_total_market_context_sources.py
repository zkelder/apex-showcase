"""Proofs for the Historical Total Market Context source (section 7):
reuses the existing ``MaterializeTotalMarketContextSnapshot`` application
service and ``domain.total_market_context`` classification logic unchanged,
sourced from genuine point-in-time replay bar evidence. Confirms SPY/TLT/USO
presence yields classified components, absence yields an honestly
``UNAVAILABLE`` bundle, future bars are never used, and the persisted
snapshot's timestamps are the historical as_of -- never wall-clock now.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.engine import Engine

from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.historical_total_market_context_sources import (
    HistoricalTotalMarketContextSource,
)
from apex.adapters.securities_postgres import PostgresSecuritiesRepository
from apex.adapters.total_market_context_postgres import PostgresTotalMarketContextRepository
from apex.application.total_market_context import MaterializeTotalMarketContextSnapshot
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
)
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
from apex.domain.total_market_context import OverallContextState
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


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
        source_identity="TEST_TMC_HISTORICAL_CALENDAR",
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
        source_identity="TEST_TMC_HISTORICAL",
    )


def _build_snapshot(
    *, postgres_engine: Engine, symbols: tuple[str, ...], day_count: int, idempotency_key: str
) -> HistoricalDatasetSnapshot:
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
            stable_security_id=f"stable-{symbol}", symbol_at_capture=symbol, asset_class="ETF"
        )
        for symbol in symbols
    )
    plan = HistoricalCapturePlan.draft(
        policy=policy,
        universe=universe,
        evidence_class="FIXTURE",
        operator_action_id="op-tmc-fixture",
        idempotency_key=idempotency_key,
        created_at=NOW,
    )
    historical_repository.save_plan(plan)
    instruments = tuple(
        SecuritiesInstrument(
            instrument_id=member.instrument_id,
            symbol=member.symbol_at_capture,
            asset_class=SecuritiesAssetClass.ETF,
            exchange_mic="ARCX",
            currency="USD",
            timezone="America/New_York",
            valid_from="0001-01-01",
            valid_to=None,
            source_identity="TEST_TMC_HISTORICAL",
        )
        for member in universe
    )
    sessions = tuple(_session(day) for day in dates)
    bars: list[SecuritiesBarEvidence] = []
    for offset, member in enumerate(universe):
        for index, day in enumerate(dates):
            close = str(Decimal("100") + Decimal(offset * 10) + Decimal(index) * Decimal("0.15"))
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
    return snapshot


def test_reference_symbols_present_yield_classified_historical_components(
    postgres_engine: Engine,
) -> None:
    snapshot = _build_snapshot(
        postgres_engine=postgres_engine,
        symbols=("SPY", "TLT", "USO"),
        day_count=60,
        idempotency_key="tmc-present",
    )
    as_of = datetime.fromisoformat("2021-02-28T21:30:00+00:00")
    bundle = HistoricalTotalMarketContextSource(postgres_engine).load(
        source_batch_id=snapshot.snapshot_id, as_of=as_of
    )
    types_present = {item.component_type.value for item in bundle.components}
    assert {"US_EQUITY_MARKET", "TREASURY_RATE", "COMMODITY_OR_INFLATION"}.issubset(types_present)
    assert all(item.evidence_class == "HISTORICAL_PUBLIC" for item in bundle.components)
    assert all(item.available_at <= as_of for item in bundle.components)


def test_reference_symbols_absent_is_honestly_all_unavailable(postgres_engine: Engine) -> None:
    snapshot = _build_snapshot(
        postgres_engine=postgres_engine,
        symbols=("ZZZ",),
        day_count=30,
        idempotency_key="tmc-absent",
    )
    as_of = datetime.fromisoformat("2021-01-30T21:30:00+00:00")
    bundle = HistoricalTotalMarketContextSource(postgres_engine).load(
        source_batch_id=snapshot.snapshot_id, as_of=as_of
    )
    assert bundle.components == ()
    assert len(bundle.unavailable_components) == 7  # every ContextComponentType


def test_future_bars_are_never_used_by_historical_context(postgres_engine: Engine) -> None:
    snapshot = _build_snapshot(
        postgres_engine=postgres_engine,
        symbols=("SPY", "TLT", "USO"),
        day_count=60,
        idempotency_key="tmc-future",
    )
    early_as_of = datetime.fromisoformat("2021-01-10T21:30:00+00:00")
    bundle = HistoricalTotalMarketContextSource(postgres_engine).load(
        source_batch_id=snapshot.snapshot_id, as_of=early_as_of
    )
    # Only 10 days in: not enough for a 21-session component yet -- honestly
    # unavailable, never fabricated from a shorter or future-dated window.
    assert bundle.components == ()
    assert len(bundle.unavailable_components) == 7
    # A cutoff entirely before the dataset's own retained range is likewise
    # graceful (no evidence yet), never an error and never a fabricated read.
    before_dataset = HistoricalTotalMarketContextSource(postgres_engine).load(
        source_batch_id=snapshot.snapshot_id,
        as_of=datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
    )
    assert before_dataset.components == ()


def test_materialized_snapshot_timestamp_is_the_historical_as_of_not_wall_clock(
    postgres_engine: Engine,
) -> None:
    snapshot = _build_snapshot(
        postgres_engine=postgres_engine,
        symbols=("SPY", "TLT", "USO"),
        day_count=60,
        idempotency_key="tmc-timestamp",
    )
    historical_as_of = datetime.fromisoformat("2021-02-15T21:30:00+00:00")
    repository = PostgresTotalMarketContextRepository(postgres_engine)
    result = MaterializeTotalMarketContextSnapshot(
        repository, HistoricalTotalMarketContextSource(postgres_engine)
    ).execute(
        snapshot_id=f"historical-tmc-{snapshot.snapshot_id}-2021-02-15",
        source_batch_id=snapshot.snapshot_id,
        as_of=historical_as_of,
        idempotency_key="tmc-materialize-1",
    )
    assert result.snapshot.created_at == historical_as_of
    assert result.snapshot.created_at.year == 2021
    now_years_later = datetime.now(tz=UTC).year
    assert result.snapshot.created_at.year != now_years_later
    reloaded = repository.context_snapshot(result.snapshot.snapshot_id)
    assert reloaded is not None
    assert reloaded.overall_state in tuple(OverallContextState)
