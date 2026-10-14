from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex.adapters.kalshi_consistency_fixtures import KalshiFixturePublicConsistencyClient
from apex.adapters.kalshi_public import KalshiPublicEventNormalizer, KalshiPublicMarketNormalizer
from apex.adapters.postgres import (
    PostgresSettings,
    create_postgres_engine,
    postgres_public_event_consistency_store,
    postgres_public_event_history_store,
    postgres_public_market_discovery_store,
)
from apex.application.build_event_observation_history import (
    BuildEventObservationHistory,
    EventHistoryRequest,
    EventObservationHistoryConflictError,
)
from apex.application.discover_public_events import (
    DiscoverPublicEventConsistency,
    PublicEventScanRequest,
)
from apex.application.discover_public_markets import (
    DiscoverPublicPredictionMarkets,
    PublicMarketScanRequest,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash, content_digest, deterministic_id

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
REVISION_0008 = "0008_persisted_scan_comparison"
REVISION_0009 = "0009_event_observation_history"
HISTORY_TABLES = {"event_history_analysis", "event_history_observation"}


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _migrate(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        command.upgrade(_config(connection), revision)
        connection.commit()


def _downgrade(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        command.downgrade(_config(connection), revision)
        connection.commit()


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _persist_scan(engine: Engine, *, run_key: str, offset: timedelta) -> str:
    client = KalshiFixturePublicConsistencyClient(received_at_offset=offset)
    markets = DiscoverPublicPredictionMarkets(
        client=client,
        normalizer=KalshiPublicMarketNormalizer(),
        store=postgres_public_market_discovery_store(engine),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC) + offset,
    ).discover(PublicMarketScanRequest(run_key=run_key, maximum_pages=3))
    DiscoverPublicEventConsistency(
        client=client,
        normalizer=KalshiPublicEventNormalizer(),
        store=postgres_public_event_consistency_store(engine),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC) + offset,
    ).discover(
        PublicEventScanRequest(
            run_key=f"{run_key}-events",
            maximum_pages=3,
            market_ingestion_run_id=markets.ingestion_run_id,
        )
    )
    return markets.ingestion_run_id


def _history(engine: Engine):
    return BuildEventObservationHistory(store=postgres_public_event_history_store(engine))


def test_0009_schema_isolated_and_empty_downgrade_reupgrade(postgres_engine: Engine) -> None:
    inspector = inspect(postgres_engine)
    assert HISTORY_TABLES.issubset(inspector.get_table_names())
    financial_tables = {"campaign", "ledger_journal", "risk_decision", "order", "position"}
    for table_name in HISTORY_TABLES:
        assert not financial_tables.intersection(
            foreign_key["referred_table"] for foreign_key in inspector.get_foreign_keys(table_name)
        )
    with postgres_engine.connect() as connection:
        triggers = set(
            connection.scalars(
                sa.text(
                    "SELECT trigger.tgname FROM pg_trigger AS trigger "
                    "JOIN pg_class AS relation ON relation.oid = trigger.tgrelid "
                    "WHERE relation.relname IN "
                    "('event_history_analysis', 'event_history_observation') "
                    "AND NOT trigger.tgisinternal"
                )
            )
        )
    assert triggers >= {
        "apex_guard_event_history_analysis_trigger",
        "apex_guard_event_history_observation_trigger",
        "apex_immutable_event_history_analysis_trigger",
        "apex_immutable_event_history_observation_trigger",
    }
    _downgrade(postgres_engine, REVISION_0008)
    try:
        assert _revision(postgres_engine) == REVISION_0008
        assert HISTORY_TABLES.isdisjoint(inspect(postgres_engine).get_table_names())
        _migrate(postgres_engine, "head")
        assert _revision(postgres_engine) == "0118_evidence_gap_recovery"
    finally:
        _migrate(postgres_engine, "head")


def test_history_is_idempotent_retains_every_run_and_does_not_mutate_financial_state(
    postgres_engine: Engine,
) -> None:
    for ordinal in range(3):
        _persist_scan(
            postgres_engine,
            run_key=f"history-postgres-{ordinal}",
            offset=timedelta(minutes=10 * ordinal),
        )
    financial_tables = ("campaign", "ledger_journal", "risk_decision", "order", "position")
    with postgres_engine.connect() as connection:
        before = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
            for table in financial_tables
        }

    first = _history(postgres_engine).build(
        EventHistoryRequest(provider_event_ticker="EVT-CANDIDATE", maximum_completed_runs=3)
    )
    replay = _history(postgres_engine).build(
        EventHistoryRequest(provider_event_ticker="EVT-CANDIDATE", maximum_completed_runs=3)
    )
    restarted_engine = create_postgres_engine(PostgresSettings.from_env().for_test())
    try:
        restarted = _history(restarted_engine).build(
            EventHistoryRequest(provider_event_ticker="EVT-CANDIDATE", maximum_completed_runs=3)
        )
    finally:
        restarted_engine.dispose()

    assert first.analysis.included_completed_run_count == 3
    assert first.analysis.absent_observation_count == 0
    assert first.analysis.current_candidate_streak_observation_count == 2
    assert first.analysis.evidence_break_count == 1
    assert replay.idempotent_replay
    assert restarted.idempotent_replay
    assert restarted.analysis == first.analysis
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM event_history_analysis")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM event_history_observation")) == 3
        for table, count in before.items():
            assert connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) == count
    with (
        postgres_engine.begin() as connection,
        pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"),
    ):
        connection.execute(sa.text("UPDATE event_history_analysis SET evidence_break_count = 99"))


def test_history_append_rollback_removes_all_partial_rows(postgres_engine: Engine) -> None:
    for ordinal in range(3):
        _persist_scan(
            postgres_engine,
            run_key=f"history-rollback-{ordinal}",
            offset=timedelta(minutes=10 * ordinal),
        )
    request = EventHistoryRequest(provider_event_ticker="EVT-CANDIDATE", maximum_completed_runs=3)
    store = postgres_public_event_history_store(postgres_engine)
    runs = store.select_completed_runs(
        provider=PublicMarketProvider.KALSHI,
        provider_event_ticker=request.provider_event_ticker,
        first_ingestion_run_id=None,
        last_ingestion_run_id=None,
        maximum_completed_runs=request.maximum_completed_runs,
    )
    configuration = BuildEventObservationHistory._configuration(request)
    configuration_hash = ContentHash(content_digest(configuration))
    input_hash = ContentHash(
        content_digest(BuildEventObservationHistory._input_payload(configuration, runs))
    )
    report = BuildEventObservationHistory._build(
        analysis_id=deterministic_id("event_history_analysis", {"rollback": input_hash}),
        request=request,
        configuration_hash=configuration_hash,
        input_hash=input_hash,
        runs=runs,
    )
    corrupted = (
        replace(report.observations[0], event_snapshot_id="missing-event-snapshot"),
        *report.observations[1:],
    )

    with pytest.raises(EventObservationHistoryConflictError, match="immutable identity"):
        store.append_history(report.analysis, corrupted)
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM event_history_analysis")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM event_history_observation")) == 0


def test_retained_history_evidence_blocks_0009_downgrade_atomically(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0009)
    for ordinal in range(3):
        _persist_scan(
            postgres_engine,
            run_key=f"history-guard-{ordinal}",
            offset=timedelta(minutes=10 * ordinal),
        )
    _history(postgres_engine).build(
        EventHistoryRequest(provider_event_ticker="EVT-CANDIDATE", maximum_completed_runs=3)
    )
    with postgres_engine.connect() as connection:
        tables_before = set(inspect(postgres_engine).get_table_names())
        evidence_before = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
            for table in (*sorted(HISTORY_TABLES), "ledger_journal", "ledger_posting")
        }
    with pytest.raises(Exception, match="retained event observation history evidence"):
        _downgrade(postgres_engine, REVISION_0008)
    try:
        assert _revision(postgres_engine) == REVISION_0009
        assert set(inspect(postgres_engine).get_table_names()) == tables_before
        with postgres_engine.connect() as connection:
            evidence_after = {
                table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
                for table in (*sorted(HISTORY_TABLES), "ledger_journal", "ledger_posting")
            }
        assert evidence_after == evidence_before
    finally:
        _migrate(postgres_engine, "head")
