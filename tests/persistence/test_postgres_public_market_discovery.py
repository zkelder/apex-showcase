from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex.adapters.kalshi_market_fixtures import KalshiFixtureMarketDataClient
from apex.adapters.kalshi_public import KalshiPublicMarketNormalizer
from apex.adapters.postgres import (
    postgres_public_market_discovery_store,
)
from apex.application.authorize_order import DurableAuthorizationService
from apex.application.discover_public_markets import (
    DiscoverPublicPredictionMarkets,
    PublicMarketDiscoveryConflictError,
    PublicMarketScanRequest,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
REVISION_0005 = "0005_durable_void_settlement"
REVISION_0006 = "0006_public_market_discovery"
PUBLIC_TABLES = {
    "provider_ingestion_run",
    "raw_provider_response",
    "normalized_market_snapshot",
    "shadow_market_evaluation",
}


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


def _current_revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _discover(engine: Engine, *, run_key: str = "postgres-fixture"):
    return DiscoverPublicPredictionMarkets(
        client=KalshiFixtureMarketDataClient(),
        normalizer=KalshiPublicMarketNormalizer(),
        store=postgres_public_market_discovery_store(engine),
        clock=lambda: datetime(2026, 7, 20, 12, 4, tzinfo=UTC),
    ).discover(PublicMarketScanRequest(run_key=run_key, page_size=25, maximum_pages=3))


def test_0006_creates_only_isolated_public_evidence_schema(postgres_engine: Engine) -> None:
    inspector = inspect(postgres_engine)
    assert PUBLIC_TABLES.issubset(inspector.get_table_names())
    raw_columns = {
        column["name"]: column for column in inspector.get_columns("raw_provider_response")
    }
    assert isinstance(raw_columns["raw_payload"]["type"], sa.LargeBinary)
    assert isinstance(raw_columns["response_size"]["type"], sa.Integer)
    snapshot_columns = {
        column["name"]: column for column in inspector.get_columns("normalized_market_snapshot")
    }
    assert isinstance(snapshot_columns["yes_bid"]["type"], sa.Numeric)
    assert snapshot_columns["yes_bid"]["type"].precision == 38
    assert snapshot_columns["yes_bid"]["type"].scale == 18
    financial_tables = {
        "campaign",
        "ledger_journal",
        "risk_decision",
        "risk_reservation",
        "order",
        "position",
        "settlement_fact",
    }
    for table_name in PUBLIC_TABLES:
        assert not financial_tables.intersection(
            foreign_key["referred_table"] for foreign_key in inspector.get_foreign_keys(table_name)
        )


def test_fixture_ingestion_persists_immutable_raw_bytes_lineage_and_no_financial_state(
    postgres_engine: Engine,
) -> None:
    before = {}
    with postgres_engine.connect() as connection:
        for table_name in ("campaign", "ledger_journal", "risk_decision", "order", "position"):
            before[table_name] = connection.scalar(sa.text(f'SELECT count(*) FROM "{table_name}"'))

    first = _discover(postgres_engine)
    replay = _discover(postgres_engine)

    assert first.page_count == 2
    assert first.market_count == 4
    assert not first.idempotent_replay
    assert replay.idempotent_replay
    with postgres_engine.connect() as connection:
        raw_rows = (
            connection.execute(
                sa.text(
                    "SELECT raw_payload, raw_payload_sha256, response_size "
                    "FROM raw_provider_response ORDER BY request_page_ordinal"
                )
            )
            .mappings()
            .all()
        )
        assert len(raw_rows) == 2
        assert all(
            sha256(bytes(row["raw_payload"])).hexdigest() == row["raw_payload_sha256"]
            and len(bytes(row["raw_payload"])) == row["response_size"]
            for row in raw_rows
        )
        assert connection.scalar(sa.text("SELECT count(*) FROM normalized_market_snapshot")) == 4
        assert connection.scalar(sa.text("SELECT count(*) FROM shadow_market_evaluation")) == 4
        for table_name, count in before.items():
            assert connection.scalar(sa.text(f'SELECT count(*) FROM "{table_name}"')) == count

    with (
        postgres_engine.begin() as connection,
        pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"),
    ):
        connection.execute(sa.text("UPDATE raw_provider_response SET response_size = 1"))


def test_postgres_store_rejects_conflicting_discovery_identity(postgres_engine: Engine) -> None:
    _discover(postgres_engine, run_key="same-postgres-run")
    service = DiscoverPublicPredictionMarkets(
        client=KalshiFixtureMarketDataClient(),
        normalizer=KalshiPublicMarketNormalizer(),
        store=postgres_public_market_discovery_store(postgres_engine),
        clock=lambda: datetime(2026, 7, 20, 12, 4, tzinfo=UTC),
    )
    with pytest.raises(PublicMarketDiscoveryConflictError, match="configuration"):
        service.discover(PublicMarketScanRequest(run_key="same-postgres-run", page_size=26))


def test_empty_0006_downgrades_to_0005_and_reupgrades_without_touching_campaigns(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    initialize_campaign(campaign_id="campaign-before-0006")
    _downgrade(postgres_engine, REVISION_0005)
    try:
        assert _current_revision(postgres_engine) == REVISION_0005
        assert PUBLIC_TABLES.isdisjoint(inspect(postgres_engine).get_table_names())
        with postgres_engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM campaign WHERE campaign_id = 'campaign-before-0006'"
                    )
                )
                == 1
            )
        _migrate(postgres_engine, "head")
        assert _current_revision(postgres_engine) == ("0118_evidence_gap_recovery")
        assert PUBLIC_TABLES.issubset(inspect(postgres_engine).get_table_names())
    finally:
        _migrate(postgres_engine, "head")


def test_retained_public_evidence_blocks_0006_downgrade_without_partial_loss(
    postgres_engine: Engine,
) -> None:
    try:
        _discover(postgres_engine, run_key="retained-evidence")
        with postgres_engine.connect() as connection:
            before = {
                table_name: connection.scalar(sa.text(f'SELECT count(*) FROM "{table_name}"'))
                for table_name in PUBLIC_TABLES
            }
        with pytest.raises(Exception, match="retained public market discovery evidence"):
            _downgrade(postgres_engine, REVISION_0005)
        assert _current_revision(postgres_engine) == REVISION_0006
        with postgres_engine.connect() as connection:
            after = {
                table_name: connection.scalar(sa.text(f'SELECT count(*) FROM "{table_name}"'))
                for table_name in PUBLIC_TABLES
            }
        assert after == before
    finally:
        _migrate(postgres_engine, "head")
