from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from apex.adapters.coinbase_universe_public import FixtureCoinbaseUniverseSource
from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
from apex.application.crypto_universe import (
    CaptureCryptoCalibrationHistory,
    CaptureCryptoUniverseObservations,
    CapturePublicCryptoProducts,
    MaterializeCryptoUniverse,
    RunCryptoDiscovery,
)

NOW = datetime(2026, 7, 29, tzinfo=UTC)
START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 7, 1, tzinfo=UTC)


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


@pytest.mark.postgres
def test_multi_asset_crypto_evidence_is_durable_immutable_and_authority_free(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresCryptoUniverseRepository(postgres_engine)
    source = FixtureCoinbaseUniverseSource()
    capture = CapturePublicCryptoProducts(repository, source).execute(
        capture_id="postgres-crypto-products-v1",
        requested_at=NOW,
    )
    observations = CaptureCryptoUniverseObservations(repository, source).execute(
        capture_id=capture.capture_id,
        interval_start=START,
        interval_end=END,
        requested_at=NOW,
    )
    snapshot = MaterializeCryptoUniverse(repository).execute(
        snapshot_id="postgres-crypto-universe-v1",
        capture_id=capture.capture_id,
        created_at=NOW,
    )
    history = CaptureCryptoCalibrationHistory(repository, source).execute(
        history_id="postgres-current-crypto-history-v1",
        universe_snapshot_id=snapshot.snapshot_id,
        interval_start=datetime(2024, 8, 1, tzinfo=UTC),
        interval_end=datetime(2026, 8, 1, tzinfo=UTC),
        requested_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    run, opportunities = RunCryptoDiscovery(repository).execute(
        run_id="postgres-crypto-discovery-v1",
        snapshot_id=snapshot.snapshot_id,
        generated_at=NOW,
    )

    fresh = PostgresCryptoUniverseRepository(postgres_engine)
    assert fresh.product_capture(capture.capture_id) == capture
    assert {
        item.observation_id: item
        for item in fresh.observations()
        if item.observation_id in {value.observation_id for value in observations}
    } == {item.observation_id: item for item in observations}
    assert fresh.universe_snapshot(snapshot.snapshot_id) == snapshot
    assert (
        tuple(fresh.observation(item.observation_id) for item in history.observations)
        == history.observations
    )
    assert all(item.coverage_partitions for item in history.observations)
    replay = CaptureCryptoCalibrationHistory(fresh, source).execute(
        history_id="postgres-current-crypto-history-v1",
        universe_snapshot_id=snapshot.snapshot_id,
        interval_start=datetime(2024, 8, 1, tzinfo=UTC),
        interval_end=datetime(2026, 8, 1, tzinfo=UTC),
        requested_at=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )
    assert replay.idempotent_replay and replay.request_count == 0
    assert fresh.discovery_run(run.run_id) == run
    assert {item.opportunity_id: item for item in fresh.opportunities()} == {
        item.opportunity_id: item for item in opportunities
    }

    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE crypto_research_opportunity SET authority = 'PAPER' "
                "WHERE discovery_run_id = :run_id"
            ),
            {"run_id": run.run_id},
        )

    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0056 with crypto observation checkpoints above 10",
        ):
            command.downgrade(_config(connection), "0054_crypto_candidate_generic")
        connection.rollback()
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0056_crypto_universe_quality"
        )
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 0
