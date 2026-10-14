from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex.adapters.alpaca_market_data import (
    ALPACA_ENDPOINT_POLICY_HASH,
    ALPACA_ENDPOINT_POLICY_ID,
)
from apex.adapters.real_securities_postgres import PostgresRealSecuritiesDataRepository
from apex.application.real_securities_data import (
    CreateRealProviderConfiguration,
    CreateRealProviderConfigurationCommand,
    CreateSecuritiesUniverseSnapshot,
    CreateSecuritiesUniverseSnapshotCommand,
)
from apex.domain.real_securities_data import (
    SecuritiesProviderFetchOperation,
    SecuritiesProviderFetchOperationState,
    SecuritiesProviderFetchOperationType,
)
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
REVISION_0030 = "0030_batch_learning"
REVISION_0031 = "0031_real_securities_data"
HEAD_REVISION = "0118_evidence_gap_recovery"
TABLES = {
    "securities_provider_configuration",
    "securities_provider_capability_snapshot",
    "securities_universe_snapshot",
    "securities_provider_fetch_operation",
    "securities_data_quality_evaluation",
    "securities_historical_validation_run",
    "securities_forward_validation_plan",
    "securities_forward_capture_batch",
}


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _revision(connection: sa.Connection) -> str:
    return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _configuration(repository: PostgresRealSecuritiesDataRepository):
    return CreateRealProviderConfiguration(repository).execute(
        CreateRealProviderConfigurationCommand(
            environment="persistence-test",
            adapter_version="fixture-provider-v1",
            endpoint_policy_id=ALPACA_ENDPOINT_POLICY_ID,
            endpoint_policy_hash=ALPACA_ENDPOINT_POLICY_HASH,
            operator_action_id="real-securities-persistence-operator",
            created_at=NOW,
        )
    )


def test_0031_empty_downgrade_and_reupgrade(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        assert _revision(connection) == HEAD_REVISION
        assert TABLES.issubset(inspect(connection).get_table_names())
        command.downgrade(_config(connection), REVISION_0030)
        connection.commit()
        assert _revision(connection) == REVISION_0030
        assert TABLES.isdisjoint(inspect(connection).get_table_names())
        command.upgrade(_config(connection), REVISION_0031)
        connection.commit()
        assert _revision(connection) == REVISION_0031
        assert TABLES.issubset(inspect(connection).get_table_names())


def test_configuration_universe_fetch_transition_and_downgrade_guard(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), REVISION_0031)
        connection.commit()
    repository = PostgresRealSecuritiesDataRepository(postgres_engine)
    configuration = _configuration(repository)
    universe = CreateSecuritiesUniverseSnapshot(repository).execute(
        CreateSecuritiesUniverseSnapshotCommand(
            name="Predeclared fixed ETF scope",
            symbols=("SPY", "TLT"),
            reason="declared before provider retrieval",
            operator_action_id="real-securities-persistence-operator",
            idempotency_key="real-securities-universe",
            frozen_at=NOW,
        )
    )
    reserved = SecuritiesProviderFetchOperation.reserve(
        configuration=configuration,
        operation_type=SecuritiesProviderFetchOperationType.HISTORICAL_DAILY_BARS,
        symbols=universe.symbols,
        universe=universe,
        start_at=NOW,
        end_at=NOW,
        adjustment="raw",
        idempotency_key="real-securities-fetch",
        started_at=NOW,
    )
    assert repository.save_reserved_operation(reserved) == reserved
    fetching = repository.transition_operation(
        replace(reserved, state=SecuritiesProviderFetchOperationState.FETCHING)
    )
    completed = repository.transition_operation(
        replace(
            fetching,
            state=SecuritiesProviderFetchOperationState.COMPLETED,
            request_count=1,
            page_count=1,
            raw_byte_count=42,
            received_record_count=2,
            completed_at=NOW,
            raw_artifact_relative_path=".local/securities/providers/alpaca/fixture",
            raw_artifact_set_hash=content_digest({"raw": "fixture"}),
            canonical_result_hash=content_digest({"operation": reserved.operation_id}),
        )
    )
    assert repository.operation_by_idempotency("real-securities-fetch") == completed
    with postgres_engine.connect() as connection:
        with pytest.raises(
            sa.exc.ProgrammingError,
            match="cannot downgrade 0031 with retained real Securities",
        ):
            command.downgrade(_config(connection), REVISION_0030)
        connection.rollback()
        assert _revision(connection) == REVISION_0031
