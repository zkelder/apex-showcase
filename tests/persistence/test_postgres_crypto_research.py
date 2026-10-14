from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.coinbase_public import FixtureCoinbaseCandleSource
from apex.adapters.crypto_research_postgres import (
    PostgresCryptoResearchRepository,
)
from apex.application.crypto_research import (
    CaptureCoinbaseHistory,
    ExecuteCryptoTrendValidation,
    MaterializeCryptoHistoricalSnapshot,
    RecordCryptoExperimentOutcome,
    RegisterCryptoHypothesis,
)
from apex.domain.crypto_research import FIXED_DATA_END, FIXED_DATA_START
from apex.domain.research_evidence import CaptureStatus, EvidenceClass

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 29, 17, tzinfo=UTC)
CRYPTO_TABLES = {
    "crypto_hypothesis_specification",
    "crypto_historical_capture",
    "crypto_capture_attempt",
    "crypto_daily_candle",
    "crypto_capture_checkpoint",
    "crypto_historical_snapshot",
    "crypto_experiment_run",
    "crypto_experiment_outcome",
}


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _completed_capture(repository: PostgresCryptoResearchRepository):
    service = CaptureCoinbaseHistory(repository, FixtureCoinbaseCandleSource())
    capture = service.start(
        capture_id="crypto-postgres-fixture-v1",
        start=FIXED_DATA_START,
        end=FIXED_DATA_END,
        requested_at=NOW,
    )
    while capture.status is CaptureStatus.RUNNING:
        capture = service.resume(
            capture_id=capture.capture_id,
            requested_at=NOW + timedelta(seconds=capture.request_count),
        )
    return capture


def test_current_head_has_crypto_tables(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= CRYPTO_TABLES
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0118_evidence_gap_recovery"
        )


def test_full_crypto_lifecycle_survives_fresh_repositories_and_has_no_financial_effect(
    postgres_engine: Engine,
) -> None:
    repository = PostgresCryptoResearchRepository(postgres_engine)
    hypothesis, specification = RegisterCryptoHypothesis(repository).execute(
        hypothesis_id="btc-usd-postgres-trend",
        revision=1,
        actor="persistence-test",
        created_at=NOW,
    )
    capture = _completed_capture(repository)
    assert capture.evidence_class is EvidenceClass.FIXTURE
    assert capture.accepted_buckets == 730
    assert len(repository.attempts(capture.capture_id)) == 3
    assert len(repository.checkpoints(capture.capture_id)) == 3
    assert len(repository.candles(capture.capture_id)) == 730
    assert all(
        repository.raw_response(item.attempt_id) is not None
        for item in repository.attempts(capture.capture_id)
    )

    restarted = PostgresCryptoResearchRepository(postgres_engine)
    snapshot = MaterializeCryptoHistoricalSnapshot(restarted).execute(
        snapshot_id="crypto-postgres-snapshot-v1",
        capture_id=capture.capture_id,
        created_at=NOW + timedelta(minutes=1),
    )
    run = ExecuteCryptoTrendValidation(restarted).execute(
        run_id="crypto-postgres-run-v1",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        snapshot_id=snapshot.snapshot_id,
        code_revision="5ce100232265dce713a392659c10e78c7e7abb39",
        executed_at=NOW + timedelta(minutes=2),
    )
    outcome = RecordCryptoExperimentOutcome(restarted).execute(
        outcome_id="crypto-postgres-outcome-v1",
        run_id=run.run_id,
        actor="persistence-test",
        decided_at=NOW + timedelta(minutes=3),
    )

    fresh = PostgresCryptoResearchRepository(postgres_engine)
    assert fresh.hypothesis_version(hypothesis.hypothesis_version_id) == hypothesis
    assert fresh.hypothesis_specification(hypothesis.hypothesis_version_id) == specification
    assert fresh.capture(capture.capture_id) == capture
    assert fresh.snapshot(snapshot.snapshot_id) == snapshot
    assert fresh.run(run.run_id) == run
    assert fresh.outcome(outcome.outcome_id) == outcome
    assert ExecuteCryptoTrendValidation(fresh).replay(run.run_id).result_hash == run.result_hash

    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 0


def test_crypto_rows_are_immutable_and_block_destructive_downgrade(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0049_crypto_validation")
        connection.commit()
    repository = PostgresCryptoResearchRepository(postgres_engine)
    capture = _completed_capture(repository)
    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text("UPDATE crypto_daily_candle SET payload = '{}' WHERE capture_id = :capture_id"),
            {"capture_id": capture.capture_id},
        )

    tables_before = tuple(sorted(sa.inspect(postgres_engine).get_table_names()))
    with postgres_engine.connect() as connection:
        with pytest.raises(Exception, match="cannot downgrade 0049 with retained crypto evidence"):
            command.downgrade(_config(connection), "0048_research_evidence")
        connection.rollback()
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0049_crypto_validation"
        )
    assert tuple(sorted(sa.inspect(postgres_engine).get_table_names())) == tables_before
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
        connection.commit()
