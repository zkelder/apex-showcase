from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.postgres import (
    external_securities_engine_run,
    securities_bar_evidence,
    securities_instrument,
    securities_market_data_batch,
    securities_research_conformance_report,
    securities_research_run,
    securities_signal,
)
from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter, securities_record
from apex.adapters.securities_external import DeterministicLeanFixtureAdapter
from apex.adapters.securities_postgres import (
    PostgresSecuritiesCampaignBoundary,
    PostgresSecuritiesRepository,
)
from apex.application.securities import (
    ImportSecuritiesData,
    ImportSecuritiesDataCommand,
    RunSecuritiesConformance,
    RunSecuritiesConformanceCommand,
    RunSecuritiesResearch,
    RunSecuritiesResearchCommand,
    SecuritiesIdempotencyConflictError,
)
from apex.domain.securities import (
    SecuritiesBarEvidence,
    SecuritiesCalendarSession,
    SecuritiesCorporateActionEvidence,
    SecuritiesDataBatchState,
    SecuritiesInstrument,
    SecuritiesMarketDataBatch,
    SecuritiesStrategySpecification,
)
from apex.domain.values import content_digest, deterministic_id

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _source(adapter: LocalSecuritiesDatasetAdapter) -> tuple[str, tuple[str, ...], str, str]:
    dates = tuple(date(2026, 1, 5) + timedelta(days=index) for index in range(6))
    sessions = tuple(
        SecuritiesCalendarSession.create(
            calendar_identity="XNYS",
            calendar_version="persistence-v1",
            timezone="America/New_York",
            session_date=value.isoformat(),
            opens_at=datetime.combine(value, time(14, 30), tzinfo=UTC),
            closes_at=datetime.combine(value, time(21), tzinfo=UTC),
            is_early_close=False,
            source_identity="persistence-calendar",
        )
        for value in dates
    )
    instruments = tuple(
        SecuritiesInstrument(
            instrument_id=f"securities-persistence-{index}",
            symbol=f"SP{index}",
            asset_class="ETF" if index == 0 else "COMMON_STOCK",
            exchange_mic="XNYS",
            currency="USD",
            timezone="America/New_York",
            valid_from=sessions[0].session_date,
            valid_to=None,
            source_identity="persistence-instruments",
        )
        for index in range(2)
    )
    bars = tuple(
        SecuritiesBarEvidence(
            instrument_id=instrument.instrument_id,
            interval="ONE_DAY",
            adjustment_state="RAW",
            period_start=session.opens_at,
            period_end=session.closes_at,
            available_at=session.closes_at + timedelta(minutes=1),
            open=str(50 + instrument_index + session_index),
            high=str(52 + instrument_index + session_index),
            low=str(49 + instrument_index + session_index),
            close=str(51 + instrument_index + session_index),
            volume=str(10_000 + session_index),
            source_identity="persistence-bars",
        )
        for instrument_index, instrument in enumerate(instruments)
        for session_index, session in enumerate(sessions)
    )
    actions = (
        SecuritiesCorporateActionEvidence(
            instrument_id=instruments[1].instrument_id,
            action_type="CASH_DIVIDEND",
            effective_date=sessions[4].session_date,
            available_at=sessions[3].closes_at,
            cash_amount="0.1",
            source_identity="persistence-actions",
        ),
    )
    source = adapter.write_generated_jsonl(
        relative_location="incoming/persistence.jsonl",
        records=(securities_record(item) for item in (*instruments, *sessions, *bars, *actions)),
    )
    return (
        source,
        tuple(item.instrument_id for item in instruments),
        sessions[0].session_date,
        sessions[-1].session_date,
    )


def test_securities_evidence_restarts_idempotently_and_never_writes_financial_state(
    postgres_engine: Engine,
    initialize_campaign: Callable[..., object],
    tmp_path: Path,
) -> None:
    initialize_campaign(campaign_id="campaign-securities-persistence")
    financial_tables = (
        "opportunity_proposal",
        "allocation_decision",
        "risk_decision",
        "risk_reservation",
        "order",
        "position",
        "settlement_fact",
        "position_settlement",
        "ledger_journal",
        "ledger_posting",
    )
    with postgres_engine.connect() as connection:
        financial_before = {}
        for table_name in financial_tables:
            quoted = f'"{table_name}"' if table_name == "order" else table_name
            financial_before[table_name] = connection.scalar(
                sa.text(f"SELECT count(*) FROM {quoted}")
            )
    adapter = LocalSecuritiesDatasetAdapter(tmp_path / "managed")
    source, universe, start_date, end_date = _source(adapter)
    repository = PostgresSecuritiesRepository(postgres_engine)
    importer = ImportSecuritiesData(repository=repository, market_data=adapter)
    batch = importer.execute(
        ImportSecuritiesDataCommand(
            managed_source=source,
            dataset_name="durable securities evidence",
            idempotency_key="durable-import-v1",
            imported_at=NOW,
        )
    )
    assert (
        importer.execute(
            ImportSecuritiesDataCommand(
                managed_source=source,
                dataset_name="durable securities evidence",
                idempotency_key="durable-import-v1",
                imported_at=NOW,
            )
        )
        == batch
    )
    with pytest.raises(SecuritiesIdempotencyConflictError):
        importer.execute(
            ImportSecuritiesDataCommand(
                managed_source=source,
                dataset_name="different durable evidence",
                idempotency_key="durable-import-v1",
                imported_at=NOW,
            )
        )
    specification = SecuritiesStrategySpecification.create(
        strategy_id="durable-securities-momentum-v1",
        kind="TIME_SERIES_MOMENTUM",
        version="1",
        lookback_sessions=2,
        rebalance_every_sessions=1,
        created_at=NOW,
    )
    repository.save_specification(specification)
    research_service = RunSecuritiesResearch(
        repository=repository,
        campaigns=PostgresSecuritiesCampaignBoundary(postgres_engine),
        market_data=adapter,
    )
    command = RunSecuritiesResearchCommand(
        campaign_id="campaign-securities-persistence",
        strategy_id=specification.strategy_id,
        data_batch_id=batch.batch_id,
        universe=universe,
        start_date=start_date,
        end_date=end_date,
        decision_cutoff=NOW,
        benchmark_instrument_id=universe[0],
        idempotency_key="durable-research-v1",
    )
    run = research_service.execute(command)
    restarted_repository = PostgresSecuritiesRepository(postgres_engine)
    assert restarted_repository.get_research_run(run.research_run_id) == run
    assert restarted_repository.list_signals(run.research_run_id)
    assert research_service.execute(command) == run

    fixture = DeterministicLeanFixtureAdapter(engine_identity="LEAN_FIXTURE_EXACT")
    report = RunSecuritiesConformance(repository=restarted_repository, engines=(fixture,)).execute(
        RunSecuritiesConformanceCommand(
            research_run_id=run.research_run_id,
            external_engine=fixture.engine_identity,
            created_at=NOW + timedelta(minutes=1),
        )
    )
    assert report.classification.value == "CONFORMANT"
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(securities_instrument)) == 2
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(securities_market_data_batch))
            == 1
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(securities_research_run)) == 1
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(securities_signal)) > 0
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(external_securities_engine_run)
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(securities_research_conformance_report)
            )
            == 1
        )
        for table_name in financial_tables:
            quoted = f'"{table_name}"' if table_name == "order" else table_name
            assert (
                connection.scalar(sa.text(f"SELECT count(*) FROM {quoted}"))
                == financial_before[table_name]
            )


def test_securities_dataset_insert_rolls_back_atomically_on_unknown_instrument(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    adapter = LocalSecuritiesDatasetAdapter(tmp_path / "managed")
    source, _universe, _start, _end = _source(adapter)
    dataset = adapter.load_managed_source(managed_source=source, dataset_name="rollback evidence")
    repository = PostgresSecuritiesRepository(postgres_engine)
    original = dataset.bars[0]
    unknown = SecuritiesBarEvidence(
        instrument_id="unknown-instrument",
        interval=original.interval,
        adjustment_state=original.adjustment_state,
        period_start=original.period_start,
        period_end=original.period_end,
        available_at=original.available_at,
        open=original.open,
        high=original.high,
        low=original.low,
        close=original.close,
        volume=original.volume,
        source_identity="rollback-invalid",
    )
    invalid_dataset = replace(dataset, bars=(*dataset.bars, unknown))
    batch = SecuritiesMarketDataBatch(
        batch_id=deterministic_id(
            "securities_batch", {"canonical_sha256": dataset.canonical_sha256}
        ),
        dataset_name="rollback evidence",
        state=SecuritiesDataBatchState.READY,
        imported_at=NOW,
        source_artifact=dataset.source_artifact,
        source_sha256=dataset.source_sha256,
        source_byte_size=dataset.source_byte_size,
        canonical_artifact=dataset.canonical_artifact,
        canonical_sha256=dataset.canonical_sha256,
        canonical_byte_size=dataset.canonical_byte_size,
        instrument_count=len(dataset.instruments),
        session_count=len(dataset.sessions),
        bar_count=len(invalid_dataset.bars),
        corporate_action_count=len(dataset.corporate_actions),
        first_period_start=min(item.period_start for item in invalid_dataset.bars),
        last_period_end=max(item.period_end for item in invalid_dataset.bars),
        idempotency_key="rollback-import",
        warnings=(),
        canonical_hash=content_digest({"rollback": "late-foreign-key"}),
    )
    with pytest.raises(SecuritiesIdempotencyConflictError):
        repository.save_dataset(
            batch=batch,
            instruments=invalid_dataset.instruments,
            sessions=invalid_dataset.sessions,
            bars=invalid_dataset.bars,
            corporate_actions=invalid_dataset.corporate_actions,
        )
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(securities_instrument)) == 0
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(securities_market_data_batch))
            == 0
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(securities_bar_evidence)) == 0
        )


def test_securities_evidence_rows_are_immutable(
    postgres_engine: Engine,
    initialize_campaign: Callable[..., object],
    tmp_path: Path,
) -> None:
    initialize_campaign(campaign_id="campaign-securities-immutable")
    adapter = LocalSecuritiesDatasetAdapter(tmp_path / "managed")
    source, _universe, _start, _end = _source(adapter)
    repository = PostgresSecuritiesRepository(postgres_engine)
    batch = ImportSecuritiesData(repository=repository, market_data=adapter).execute(
        ImportSecuritiesDataCommand(
            managed_source=source,
            dataset_name="immutable evidence",
            idempotency_key="immutable-import",
            imported_at=NOW,
        )
    )
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"):
            connection.execute(
                sa.text(
                    "UPDATE securities_market_data_batch SET dataset_name = 'changed' "
                    "WHERE batch_id = :batch_id"
                ),
                {"batch_id": batch.batch_id},
            )
        transaction.rollback()
