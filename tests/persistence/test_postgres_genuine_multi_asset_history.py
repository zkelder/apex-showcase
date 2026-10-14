"""PostgreSQL persistence proofs for migration 0080: the per-instrument
genuine cohort quality-gate report and the cross-market dataset binding --
round-trip, idempotent replay, immutable-evidence enforcement, and
downgrade protection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.genuine_multi_asset_history_postgres import (
    GENUINE_MULTI_ASSET_HISTORY_TABLES,
    PostgresGenuineMultiAssetHistoryRepository,
)
from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.domain.cross_market_dataset import CrossMarketDataset
from apex.domain.genuine_data_quality_gate import GenuineCohortQualityGateReport
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 7, 15, tzinfo=UTC)


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _seeded_snapshot(engine: Engine) -> HistoricalDatasetSnapshot:
    repository = PostgresHistoricalMarketDataRepository(engine)
    policy = HistoricalCapturePolicy.create(
        provider="ALPACA_MARKET_DATA_V2",
        start_date="2020-07-27",
        end_date="2026-08-06",
        chunk_size=10,
        maximum_instruments=10,
        maximum_requests=10,
        maximum_bytes=1_000_000,
    )
    universe = (
        HistoricalCaptureUniverseMember.create(
            stable_security_id="stable_ZZZ", symbol_at_capture="ZZZ", asset_class="COMMON_STOCK"
        ),
    )
    plan = HistoricalCapturePlan.draft(
        policy=policy,
        universe=universe,
        evidence_class="GENUINE_PUBLIC",
        operator_action_id="test-plan",
        idempotency_key="genuine-multi-asset-history-postgres-test-v1",
        created_at=NOW,
    )
    repository.save_plan(plan)
    snapshot = HistoricalDatasetSnapshot.create(
        plan=plan,
        source_batch_ids=(),
        source_operation_ids=("op-1",),
        observation_count=0,
        completeness_state=HistoricalDatasetCompletenessState.PARTIAL,
        earliest_observation_date=None,
        latest_observation_date=None,
        created_at=NOW,
    )
    return repository.save_snapshot(snapshot)


def _report(snapshot: HistoricalDatasetSnapshot) -> GenuineCohortQualityGateReport:
    return GenuineCohortQualityGateReport.build(
        snapshot_id=snapshot.snapshot_id,
        cohort_policy_id="cohort-policy-test-v1",
        requested_symbols=("ZZZ",),
        assessments=(),
        point_in_time_limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
        source_hashes=(),
        created_at=NOW,
    )


def _dataset(
    snapshot: HistoricalDatasetSnapshot, report: GenuineCohortQualityGateReport
) -> CrossMarketDataset:
    return CrossMarketDataset.create(
        equity_dataset_snapshot_id=snapshot.snapshot_id,
        equity_dataset_snapshot_hash=snapshot.canonical_hash,
        equity_coverage_start="2020-07-27",
        equity_coverage_end="2026-08-06",
        cohort_policy_id="cohort-policy-test-v1",
        cohort_policy_hash="a" * 64,
        quality_gate_report_id=report.report_id,
        quality_gate_report_hash=report.canonical_hash,
        crypto_provider="COINBASE",
        crypto_product_id="BTC-USD",
        crypto_capture_ids=("btc-a",),
        crypto_coverage_start="2020-07-01",
        crypto_coverage_end="2026-06-30",
        tmc_component_symbols=("SPY", "TLT", "USO"),
        tmc_source_identity="HISTORICAL_REPLAY_RETAINED",
        adjustment_policy_id="ALPACA_IEX_RAW_NO_ADJUSTMENT_V1",
        missing_data_policy_id="FAIL_CLOSED_NO_FORWARD_FILL_V1",
        alignment_policy_id="CROSS_MARKET_ALIGNMENT_V1",
        point_in_time_limitations=(),
        created_at=NOW,
    )


def test_tables_exist_at_head(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(
        GENUINE_MULTI_ASSET_HISTORY_TABLES
    )


def test_quality_gate_report_round_trips_exactly(postgres_engine: Engine) -> None:
    snapshot = _seeded_snapshot(postgres_engine)
    repository = PostgresGenuineMultiAssetHistoryRepository(postgres_engine)
    saved = repository.save_quality_gate_report(_report(snapshot))
    fresh = PostgresGenuineMultiAssetHistoryRepository(postgres_engine)
    loaded = fresh.quality_gate_report_by_id(saved.report_id)
    assert loaded == saved
    assert loaded in fresh.list_quality_gate_reports()


def test_quality_gate_report_save_is_idempotent(postgres_engine: Engine) -> None:
    snapshot = _seeded_snapshot(postgres_engine)
    repository = PostgresGenuineMultiAssetHistoryRepository(postgres_engine)
    report = _report(snapshot)
    first = repository.save_quality_gate_report(report)
    second = repository.save_quality_gate_report(report)
    assert first.report_id == second.report_id
    assert len(repository.list_quality_gate_reports()) == 1


def test_cross_market_dataset_round_trips_exactly(postgres_engine: Engine) -> None:
    snapshot = _seeded_snapshot(postgres_engine)
    repository = PostgresGenuineMultiAssetHistoryRepository(postgres_engine)
    report = repository.save_quality_gate_report(_report(snapshot))
    saved = repository.save_cross_market_dataset(_dataset(snapshot, report))
    fresh = PostgresGenuineMultiAssetHistoryRepository(postgres_engine)
    loaded = fresh.cross_market_dataset_by_id(saved.cross_market_dataset_id)
    assert loaded == saved
    assert loaded in fresh.list_cross_market_datasets()


def test_genuine_multi_asset_rows_are_immutable(postgres_engine: Engine) -> None:
    snapshot = _seeded_snapshot(postgres_engine)
    repository = PostgresGenuineMultiAssetHistoryRepository(postgres_engine)
    report = repository.save_quality_gate_report(_report(snapshot))
    dataset = repository.save_cross_market_dataset(_dataset(snapshot, report))

    with postgres_engine.connect() as connection:
        with pytest.raises(Exception, match="immutable"):
            connection.execute(
                sa.text(
                    "UPDATE genuine_cohort_quality_gate_report SET usable_count = 999 "
                    "WHERE report_id = :report_id"
                ),
                {"report_id": report.report_id},
            )
        connection.rollback()
        with pytest.raises(Exception, match="immutable"):
            connection.execute(sa.text("DELETE FROM genuine_cohort_quality_gate_report"))
        connection.rollback()
        with pytest.raises(Exception, match="immutable"):
            connection.execute(
                sa.text(
                    "UPDATE cross_market_dataset SET crypto_provider = 'OTHER' "
                    "WHERE cross_market_dataset_id = :dataset_id"
                ),
                {"dataset_id": dataset.cross_market_dataset_id},
            )
        connection.rollback()
        with pytest.raises(Exception, match="immutable"):
            connection.execute(sa.text("DELETE FROM cross_market_dataset"))
        connection.rollback()


def test_retained_genuine_multi_asset_evidence_blocks_destructive_0080_downgrade(
    postgres_engine: Engine,
) -> None:
    snapshot = _seeded_snapshot(postgres_engine)
    repository = PostgresGenuineMultiAssetHistoryRepository(postgres_engine)
    repository.save_quality_gate_report(_report(snapshot))
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0080 with retained genuine multi-asset history evidence",
        ):
            command.downgrade(_config(connection), "0079_quant_research_methodology")
        connection.rollback()
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0080_genuine_multi_asset_history"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")


def test_0080_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(
        GENUINE_MULTI_ASSET_HISTORY_TABLES
    )
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0079_quant_research_methodology")
    assert set(GENUINE_MULTI_ASSET_HISTORY_TABLES).isdisjoint(
        sa.inspect(postgres_engine).get_table_names()
    )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(
        GENUINE_MULTI_ASSET_HISTORY_TABLES
    )
