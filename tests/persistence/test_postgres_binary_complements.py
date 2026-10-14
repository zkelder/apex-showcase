from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
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
    postgres_public_binary_complement_store,
    postgres_public_market_discovery_store,
)
from apex.application.discover_public_markets import (
    CapturedPublicMarketPage,
    DiscoverPublicPredictionMarkets,
    PublicMarketScanRequest,
)
from apex.application.scan_public_binary_complements import (
    BinaryComplementScanRequest,
    ScanPublicBinaryComplements,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
REVISION_0009 = "0009_event_observation_history"
REVISION_0010 = "0010_public_orderbook_complement"
HEAD_REVISION = "0118_evidence_gap_recovery"
COMPLEMENT_TABLES = {
    "series_fee_snapshot",
    "market_orderbook_snapshot",
    "market_orderbook_level",
    "binary_complement_evaluation",
    "binary_complement_leg",
}
ORDERBOOK_HISTORY_TABLES = {"orderbook_history_analysis", "orderbook_history_observation"}


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
    def __init__(self, *, include_series_ticker: bool = True) -> None:
        self.include_series_ticker = include_series_ticker

    def fetch(self, **kwargs: object) -> CapturedPublicMarketPage:
        if kwargs.get("status") != "open" or kwargs.get("cursor") is not None:
            raise ValueError("synthetic market request is malformed")
        market = {
            "ticker": MARKET_TICKER,
            "event_ticker": "COMPLEMENT-EVENT",
            "status": "open",
            "open_time": "2026-07-20T11:00:00Z",
            "close_time": "2026-07-20T13:00:00Z",
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.55",
            "no_bid_dollars": "0.45",
            "no_ask_dollars": "0.55",
            "yes_bid_size_fp": "10.00",
            "yes_ask_size_fp": "10.00",
            "rules_primary": "rule-v1",
        }
        if self.include_series_ticker:
            market["series_ticker"] = "COMPLEMENT-SERIES"
        document = {"markets": [market], "cursor": None}
        raw_payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return CapturedPublicMarketPage(
            endpoint_identity=KALSHI_MARKETS_ENDPOINT_IDENTITY,
            request_cursor=None,
            received_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
            http_status=200,
            response_content_type="application/json",
            raw_payload=raw_payload,
            response_cursor=None,
            document=document,
        )


def _persist_market_scan(
    engine: Engine, *, run_key: str, include_series_ticker: bool = True
) -> str:
    return (
        DiscoverPublicPredictionMarkets(
            client=_OneMarketClient(include_series_ticker=include_series_ticker),
            normalizer=KalshiPublicMarketNormalizer(),
            store=postgres_public_market_discovery_store(engine),
            clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        .discover(PublicMarketScanRequest(run_key=run_key, maximum_pages=1))
        .ingestion_run_id
    )


def _scan(engine: Engine, *, run_id: str):
    return ScanPublicBinaryComplements(
        client=KalshiFixtureBinaryComplementClient(),
        store=postgres_public_binary_complement_store(engine),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    ).scan(
        BinaryComplementScanRequest(
            market_discovery_run_id=run_id,
            market_limit=1,
            maximum_depth=5,
            maximum_bundle_quantity="10",
        )
    )


def test_0010_isolated_schema_empty_downgrade_reupgrade_and_prior_public_evidence_survives(
    postgres_engine: Engine,
) -> None:
    assert COMPLEMENT_TABLES.issubset(inspect(postgres_engine).get_table_names())
    financial_tables = {"campaign", "ledger_journal", "risk_decision", "order", "position"}
    for table_name in COMPLEMENT_TABLES:
        assert not financial_tables.intersection(
            foreign_key["referred_table"]
            for foreign_key in inspect(postgres_engine).get_foreign_keys(table_name)
        )
    _downgrade(postgres_engine, REVISION_0009)
    try:
        assert _revision(postgres_engine) == REVISION_0009
        assert COMPLEMENT_TABLES.isdisjoint(inspect(postgres_engine).get_table_names())
        run_id = _persist_market_scan(postgres_engine, run_key="public-survives-0010")
        _migrate(postgres_engine, "head")
        assert _revision(postgres_engine) == HEAD_REVISION
        with postgres_engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM normalized_market_snapshot "
                        "WHERE ingestion_run_id = :run_id"
                    ),
                    {"run_id": run_id},
                )
                == 1
            )
    finally:
        _migrate(postgres_engine, "head")


def test_complement_evidence_is_idempotent_immutable_restart_safe_and_financially_isolated(
    postgres_engine: Engine,
) -> None:
    run_id = _persist_market_scan(postgres_engine, run_key="complement-evidence")
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

    first = _scan(postgres_engine, run_id=run_id)
    replay = _scan(postgres_engine, run_id=run_id)
    restarted_engine = create_postgres_engine(PostgresSettings.from_env().for_test())
    try:
        restarted = _scan(restarted_engine, run_id=run_id)
    finally:
        restarted_engine.dispose()

    assert first.candidate_count == 1
    assert replay.idempotent_replay
    assert restarted.idempotent_replay
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM series_fee_snapshot")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM market_orderbook_snapshot")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM market_orderbook_level")) == 2
        assert connection.scalar(sa.text("SELECT count(*) FROM binary_complement_evaluation")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM binary_complement_leg")) == 2
        for table, count in before.items():
            assert connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) == count
    with (
        postgres_engine.begin() as connection,
        pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"),
    ):
        connection.execute(sa.text("UPDATE series_fee_snapshot SET fee_type = 'flat'"))


def test_missing_list_series_is_completed_from_exact_event_evidence(
    postgres_engine: Engine,
) -> None:
    run_id = _persist_market_scan(
        postgres_engine,
        run_key="complement-exact-market-series",
        include_series_ticker=False,
    )

    first = _scan(postgres_engine, run_id=run_id)
    replay = _scan(postgres_engine, run_id=run_id)

    assert first.candidate_count == 1
    assert replay.idempotent_replay
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM raw_provider_response "
                    "WHERE ingestion_run_id = :run_id "
                    "AND endpoint_identity = 'KALSHI_GET_EVENTS'"
                ),
                {"run_id": run_id},
            )
            == 1
        )


def test_retained_complement_evidence_blocks_0010_downgrade_atomically(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0010)
    try:
        run_id = _persist_market_scan(postgres_engine, run_key="complement-downgrade-guard")
        _scan(postgres_engine, run_id=run_id)
        with postgres_engine.connect() as connection:
            tables_before = set(inspect(postgres_engine).get_table_names())
            evidence_before = {
                table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
                for table in (*sorted(COMPLEMENT_TABLES), "ledger_journal", "ledger_posting")
            }

        with pytest.raises(Exception, match="retained public orderbook or fee evidence"):
            _downgrade(postgres_engine, REVISION_0009)

        assert _revision(postgres_engine) == REVISION_0010
        assert set(inspect(postgres_engine).get_table_names()) == tables_before
        with postgres_engine.connect() as connection:
            evidence_after = {
                table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
                for table in evidence_before
            }
        assert evidence_after == evidence_before
    finally:
        _migrate(postgres_engine, "head")


class _ActivityRankedMarketsClient:
    """Three genuinely distinct open markets: high, low, and unmeasured activity.

    Tickers are deliberately in the OPPOSITE order from real activity so a
    surviving alphabetical-only selection would be caught immediately.
    """

    def fetch(self, **kwargs: object) -> CapturedPublicMarketPage:
        if kwargs.get("status") != "open" or kwargs.get("cursor") is not None:
            raise ValueError("synthetic market request is malformed")

        def market(ticker: str, *, open_interest_fp: str | None) -> dict[str, object]:
            value: dict[str, object] = {
                "ticker": ticker,
                "event_ticker": "ACTIVITY-EVENT",
                "series_ticker": "ACTIVITY-SERIES",
                "status": "open",
                "open_time": "2026-07-20T11:00:00Z",
                "close_time": "2026-07-20T13:00:00Z",
                "yes_bid_dollars": "0.45",
                "yes_ask_dollars": "0.55",
                "no_bid_dollars": "0.45",
                "no_ask_dollars": "0.55",
                "yes_bid_size_fp": "10.00",
                "yes_ask_size_fp": "10.00",
                "rules_primary": f"rule-{ticker}",
            }
            if open_interest_fp is not None:
                value["open_interest_fp"] = open_interest_fp
            return value

        document = {
            "markets": [
                market("AAA-QUIET", open_interest_fp="10"),
                market("MMM-UNMEASURED", open_interest_fp=None),
                market("ZZZ-ACTIVE", open_interest_fp="1000"),
            ],
            "cursor": None,
        }
        raw_payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return CapturedPublicMarketPage(
            endpoint_identity=KALSHI_MARKETS_ENDPOINT_IDENTITY,
            request_cursor=None,
            received_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
            http_status=200,
            response_content_type="application/json",
            raw_payload=raw_payload,
            response_cursor=None,
            document=document,
        )


def test_select_completed_open_markets_ranks_by_retained_activity_not_ticker(
    postgres_engine: Engine,
) -> None:
    # Regression coverage for the Current-Venue Signal Quality v1 fix: this
    # production query previously ordered strictly by
    # provider_market_ticker, so a bounded scan silently preferred
    # alphabetically-early, possibly-illiquid markets over genuinely active
    # ones discovered in the same run.
    run_id = (
        DiscoverPublicPredictionMarkets(
            client=_ActivityRankedMarketsClient(),
            normalizer=KalshiPublicMarketNormalizer(),
            store=postgres_public_market_discovery_store(postgres_engine),
            clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        .discover(PublicMarketScanRequest(run_key="activity-rank-select", maximum_pages=1))
        .ingestion_run_id
    )

    store = postgres_public_binary_complement_store(postgres_engine)
    full = store.select_completed_open_markets(ingestion_run_id=run_id, market_tickers=(), limit=3)
    bounded = store.select_completed_open_markets(
        ingestion_run_id=run_id, market_tickers=(), limit=2
    )

    assert [value.provider_market_ticker for value in full] == [
        "ZZZ-ACTIVE",
        "AAA-QUIET",
        "MMM-UNMEASURED",
    ]
    assert [value.open_interest for value in full] == [Decimal("1000"), Decimal("10"), None]
    # A bounded scan must keep the two genuinely most-active markets, not the
    # two that sort first alphabetically ("AAA-QUIET", "MMM-UNMEASURED").
    assert [value.provider_market_ticker for value in bounded] == ["ZZZ-ACTIVE", "AAA-QUIET"]
