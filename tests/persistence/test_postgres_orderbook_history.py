from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex.adapters.kalshi_complement_fixtures import (
    MARKET_TICKER,
    KalshiFixtureBinaryComplementClient,
)
from apex.adapters.kalshi_public import (
    KALSHI_MARKETS_ENDPOINT_IDENTITY,
    KalshiPublicMarketNormalizer,
)
from apex.adapters.postgres import (
    PostgresSettings,
    create_postgres_engine,
    postgres_orderbook_observation_history_store,
    postgres_public_binary_complement_store,
    postgres_public_market_discovery_store,
)
from apex.application.build_orderbook_observation_history import (
    BuildOrderbookObservationHistory,
    OrderbookHistoryRequest,
    OrderbookObservationHistoryConflictError,
)
from apex.application.discover_public_markets import (
    CapturedPublicMarketPage,
    DiscoverPublicPredictionMarkets,
    PublicMarketScanRequest,
)
from apex.application.scan_public_binary_complements import (
    BinaryComplementScanRequest,
    CapturedKalshiPublicResponse,
    ScanPublicBinaryComplements,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash, content_digest, deterministic_id

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
REVISION_0010 = "0010_public_orderbook_complement"
REVISION_0011 = "0011_orderbook_history"
HEAD_REVISION = "0118_evidence_gap_recovery"
HISTORY_TABLES = {"orderbook_history_analysis", "orderbook_history_observation"}


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


class _OneMarketClient:
    def __init__(self, *, received_at: datetime) -> None:
        self._received_at = received_at

    def fetch(self, **kwargs: object) -> CapturedPublicMarketPage:
        if kwargs.get("status") != "open" or kwargs.get("cursor") is not None:
            raise ValueError("synthetic market request is malformed")
        document = {
            "markets": [
                {
                    "ticker": MARKET_TICKER,
                    "event_ticker": "COMPLEMENT-EVENT",
                    "series_ticker": "COMPLEMENT-SERIES",
                    "status": "open",
                    "open_time": "2026-07-20T11:00:00Z",
                    "close_time": "2026-07-20T13:00:00Z",
                    "yes_bid_dollars": "0.45",
                    "yes_ask_dollars": "0.55",
                    "no_bid_dollars": "0.45",
                    "no_ask_dollars": "0.55",
                    "yes_bid_size_fp": "10.00",
                    "yes_ask_size_fp": "10.00",
                    "rules_primary": "Synthetic redistributable complement rule",
                }
            ],
            "cursor": None,
        }
        raw_payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return CapturedPublicMarketPage(
            endpoint_identity=KALSHI_MARKETS_ENDPOINT_IDENTITY,
            request_cursor=None,
            received_at=self._received_at,
            http_status=200,
            response_content_type="application/json",
            raw_payload=raw_payload,
            response_cursor=None,
            document=document,
        )


class _TimedComplementClient(KalshiFixtureBinaryComplementClient):
    def __init__(self, *, received_at: datetime) -> None:
        super().__init__()
        self._received_at = received_at

    def _at(self, response: CapturedKalshiPublicResponse) -> CapturedKalshiPublicResponse:
        return replace(response, received_at=self._received_at)

    def fetch_orderbook(self, **kwargs: object) -> CapturedKalshiPublicResponse:
        return self._at(super().fetch_orderbook(**kwargs))

    def fetch_series(self, **kwargs: object) -> CapturedKalshiPublicResponse:
        return self._at(super().fetch_series(**kwargs))

    def fetch_series_fee_changes(self, **kwargs: object) -> CapturedKalshiPublicResponse:
        return self._at(super().fetch_series_fee_changes(**kwargs))


def _persist_observation(engine: Engine, *, ordinal: int) -> None:
    observed_at = datetime(2026, 7, 20, 12, ordinal, tzinfo=UTC)
    run = DiscoverPublicPredictionMarkets(
        client=_OneMarketClient(received_at=observed_at),
        normalizer=KalshiPublicMarketNormalizer(),
        store=postgres_public_market_discovery_store(engine),
        clock=lambda: observed_at,
    ).discover(PublicMarketScanRequest(run_key=f"orderbook-history-{ordinal}", maximum_pages=1))
    ScanPublicBinaryComplements(
        client=_TimedComplementClient(received_at=observed_at),
        store=postgres_public_binary_complement_store(engine),
        clock=lambda: observed_at,
    ).scan(
        BinaryComplementScanRequest(
            market_discovery_run_id=run.ingestion_run_id,
            market_limit=1,
            maximum_depth=5,
            maximum_bundle_quantity="10",
        )
    )


def _history(engine: Engine) -> BuildOrderbookObservationHistory:
    return BuildOrderbookObservationHistory(
        store=postgres_orderbook_observation_history_store(engine)
    )


def _request() -> OrderbookHistoryRequest:
    return OrderbookHistoryRequest(provider_market_ticker=MARKET_TICKER, maximum_observations=3)


def test_0011_schema_isolated_empty_downgrade_and_reupgrade(postgres_engine: Engine) -> None:
    inspector = inspect(postgres_engine)
    assert HISTORY_TABLES.issubset(inspector.get_table_names())
    financial_tables = {"campaign", "ledger_journal", "risk_decision", "order", "position"}
    for table_name in HISTORY_TABLES:
        assert not financial_tables.intersection(
            foreign_key["referred_table"] for foreign_key in inspector.get_foreign_keys(table_name)
        )
    _downgrade(postgres_engine, REVISION_0010)
    try:
        assert _revision(postgres_engine) == REVISION_0010
        assert HISTORY_TABLES.isdisjoint(inspect(postgres_engine).get_table_names())
        _migrate(postgres_engine, "head")
        assert _revision(postgres_engine) == HEAD_REVISION
    finally:
        _migrate(postgres_engine, "head")


def test_history_is_idempotent_immutable_restart_safe_and_financially_isolated(
    postgres_engine: Engine,
) -> None:
    for ordinal in range(3):
        _persist_observation(postgres_engine, ordinal=ordinal)
    financial_tables = (
        "campaign",
        "ledger_journal",
        "risk_decision",
        "risk_reservation",
        "order",
        "position",
    )
    with postgres_engine.connect() as connection:
        before = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
            for table in financial_tables
        }

    first = _history(postgres_engine).build(_request())
    replay = _history(postgres_engine).build(_request())
    restarted_engine = create_postgres_engine(PostgresSettings.from_env().for_test())
    try:
        restarted = _history(restarted_engine).build(_request())
    finally:
        restarted_engine.dispose()

    assert first.analysis.included_observation_count == 3
    assert first.analysis.longest_candidate_streak_observation_count == 3
    assert replay.idempotent_replay
    assert restarted.idempotent_replay
    assert restarted.analysis == first.analysis
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM orderbook_history_analysis")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM orderbook_history_observation")) == 3
        for table, count in before.items():
            assert connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) == count
    with (
        postgres_engine.begin() as connection,
        pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"),
    ):
        connection.execute(
            sa.text("UPDATE orderbook_history_analysis SET evidence_break_count = 99")
        )


def test_history_append_rollback_removes_all_partial_rows(postgres_engine: Engine) -> None:
    for ordinal in range(3):
        _persist_observation(postgres_engine, ordinal=ordinal)
    request = _request()
    store = postgres_orderbook_observation_history_store(postgres_engine)
    values = store.select_observations(
        provider=PublicMarketProvider.KALSHI,
        provider_market_ticker=request.provider_market_ticker,
        ingestion_run_id=None,
        first_orderbook_snapshot_id=None,
        last_orderbook_snapshot_id=None,
        maximum_observations=request.maximum_observations,
    )
    configuration = BuildOrderbookObservationHistory._configuration(request)
    configuration_hash = ContentHash(content_digest(configuration))
    input_hash = ContentHash(
        content_digest(BuildOrderbookObservationHistory._input_payload(configuration, values))
    )
    report = BuildOrderbookObservationHistory._build(
        analysis_id=deterministic_id("orderbook_history_analysis", {"rollback": input_hash}),
        request=request,
        configuration_hash=configuration_hash,
        input_hash=input_hash,
        values=values,
    )
    corrupted = (
        replace(report.observations[0], orderbook_snapshot_id="missing-orderbook"),
        *report.observations[1:],
    )

    with pytest.raises(OrderbookObservationHistoryConflictError, match="immutable identity"):
        store.append_history(report.analysis, corrupted)
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM orderbook_history_analysis")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM orderbook_history_observation")) == 0


def test_retained_history_evidence_blocks_0011_downgrade_atomically(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0011)
    try:
        for ordinal in range(3):
            _persist_observation(postgres_engine, ordinal=ordinal)
        _history(postgres_engine).build(_request())
        with postgres_engine.connect() as connection:
            tables_before = set(inspect(postgres_engine).get_table_names())
            evidence_before = {
                table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
                for table in (*sorted(HISTORY_TABLES), "ledger_journal", "ledger_posting")
            }

        with pytest.raises(Exception, match="retained orderbook observation history evidence"):
            _downgrade(postgres_engine, REVISION_0010)

        assert _revision(postgres_engine) == REVISION_0011
        assert set(inspect(postgres_engine).get_table_names()) == tables_before
        with postgres_engine.connect() as connection:
            evidence_after = {
                table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
                for table in evidence_before
            }
        assert evidence_after == evidence_before
    finally:
        _migrate(postgres_engine, "head")
