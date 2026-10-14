from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine

from apex.adapters.kalshi_read_only_acceptance import (
    run_genuine_kalshi_credentials_absent_acceptance,
)
from apex.adapters.kalshi_read_only_postgres import (
    KALSHI_READ_ONLY_TABLES,
    PostgresKalshiReadOnlyRepository,
    kalshi_read_attempt_table,
)
from apex.domain.kalshi_read_only import (
    KalshiInspectionAttempt,
    KalshiInspectionStatus,
)

pytestmark = pytest.mark.postgres
HEAD_REVISION = "0118_evidence_gap_recovery"
NOW = datetime(2026, 8, 3, 14, tzinfo=UTC)


def _config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def test_genuine_credentials_absent_acceptance_replays_without_account_or_network_evidence(
    postgres_engine: Engine,
) -> None:
    first = run_genuine_kalshi_credentials_absent_acceptance(postgres_engine)
    replay = run_genuine_kalshi_credentials_absent_acceptance(postgres_engine)
    assert first["capture"] is None
    assert first["private_provider_requests"] == first["provider_writes"] == 0
    assert first["financial_counts_before"] == first["financial_counts_after"]
    assert replay["attempt"] == first["attempt"]
    repository = PostgresKalshiReadOnlyRepository(postgres_engine)
    assert repository.captures() == ()
    assert repository.attempts()[0].blocked_reason == "PRIVATE_CREDENTIALS_ABSENT"


def test_0072_empty_downgrade_reupgrade_and_retained_rows_refuse_destruction(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        command.downgrade(_config(connection), "0071_coinbase_read_only")
    assert set(KALSHI_READ_ONLY_TABLES).isdisjoint(sa.inspect(postgres_engine).get_table_names())
    with postgres_engine.begin() as connection:
        command.upgrade(_config(connection), "head")
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        )

    repository = PostgresKalshiReadOnlyRepository(postgres_engine)
    repository.save_attempt(
        KalshiInspectionAttempt.create(
            attempt_id="retained-kalshi-absence",
            approval_id=None,
            credential_reference_id=None,
            status=KalshiInspectionStatus.BLOCKED,
            blocked_reason="PRIVATE_CREDENTIALS_ABSENT",
            capture_id=None,
            request_count=0,
            warnings=("PRIVATE_CREDENTIALS_ABSENT",),
            attempted_at=NOW,
        )
    )
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError, match="cannot downgrade 0072"):
            command.downgrade(_config(connection), "0071_coinbase_read_only")
        transaction.rollback()


def test_database_rejects_secret_material_writes_and_rows_are_immutable(
    postgres_engine: Engine,
) -> None:
    repository = PostgresKalshiReadOnlyRepository(postgres_engine)
    value = repository.save_attempt(
        KalshiInspectionAttempt.create(
            attempt_id="immutable-kalshi-absence",
            approval_id=None,
            credential_reference_id=None,
            status=KalshiInspectionStatus.BLOCKED,
            blocked_reason="EXACT_READ_ONLY_APPROVAL_ABSENT",
            capture_id=None,
            request_count=0,
            warnings=("EXACT_READ_ONLY_APPROVAL_ABSENT",),
            attempted_at=NOW,
        )
    )
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.IntegrityError, match="no_secret_material"):
            connection.execute(
                kalshi_read_attempt_table.insert().values(
                    attempt_id="secret-insert",
                    approval_id=None,
                    status="BLOCKED",
                    request_count=0,
                    payload='{"private_key":"must-not-persist"}',  # pragma: allowlist secret
                    canonical_hash="f" * 64,
                    provider_write_count=0,
                    authority="NONE",
                )
            )
        transaction.rollback()
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"):
            connection.execute(
                kalshi_read_attempt_table.update()
                .where(kalshi_read_attempt_table.c.attempt_id == value.attempt_id)
                .values(provider_write_count=1)
            )
        transaction.rollback()
