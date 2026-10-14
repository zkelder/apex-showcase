from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine

from apex.adapters.live_readiness_acceptance import run_genuine_live_readiness_acceptance
from apex.adapters.live_readiness_postgres import (
    LIVE_READINESS_EVIDENCE_TABLES,
    PostgresLiveReadinessRepository,
    credential_reference_table,
)
from apex.application.live_readiness import RegisterCredentialReference
from apex.domain.live_readiness import (
    AccountEnvironment,
    LiveReadinessError,
    PermissionClass,
    Provider,
    ReadinessRecommendation,
    SecretBackend,
)

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 3, 19, tzinfo=UTC)
HEAD_REVISION = "0118_evidence_gap_recovery"


def _config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def test_genuine_credentials_absent_acceptance_is_immutable_and_financially_inert(
    postgres_engine: Engine,
) -> None:
    before = run_genuine_live_readiness_acceptance(postgres_engine)
    replay = run_genuine_live_readiness_acceptance(postgres_engine)
    assessment = before["assessment"]
    assert assessment.recommendation is ReadinessRecommendation.LIVE_NOT_READY
    assert before["private_provider_requests"] == 0
    assert before["provider_writes"] == 0
    assert before["financial_counts_before"] == before["financial_counts_after"]
    assert replay["assessment"].canonical_hash == assessment.canonical_hash
    assert replay["financial_counts_before"] == replay["financial_counts_after"]

    repository = PostgresLiveReadinessRepository(postgres_engine)
    assert repository.assessment(assessment.assessment_id) == assessment
    assert {item.provider for item in before["capabilities"]} == set(Provider)
    assert all(item.provider_request_count == 0 for item in before["capabilities"])

    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"):
            connection.execute(
                sa.text(
                    "UPDATE live_readiness_assessment SET authority = 'LIVE' "
                    "WHERE assessment_id = :assessment_id"
                ),
                {"assessment_id": assessment.assessment_id},
            )
        transaction.rollback()
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"):
            connection.execute(
                sa.text("DELETE FROM provider_account_capability_snapshot WHERE snapshot_id = :id"),
                {"id": before["capabilities"][0].snapshot_id},
            )
        transaction.rollback()


def test_credential_reference_row_cannot_retain_or_expose_secret_values(
    postgres_engine: Engine,
) -> None:
    repository = PostgresLiveReadinessRepository(postgres_engine)
    reference = RegisterCredentialReference(repository).execute(
        reference_id="persistence-credential-reference-v1",
        version=1,
        provider=Provider.COINBASE,
        account_environment=AccountEnvironment.LIVE,
        account_purpose="READ_ONLY_CAPABILITY",
        required_permissions=(PermissionClass.PRIVATE_VIEW,),
        secret_backend=SecretBackend.ENVIRONMENT_REFERENCE,
        backend_reference_id="coinbase-readonly-v1",
        actor="operator:persistence",
        reason="Persistence proof.",
        confirmation="REGISTER_CREDENTIAL_REFERENCE:persistence-credential-reference-v1:1",
        idempotency_key="persistence-credential-reference-v1",
        created_at=NOW,
    )
    assert not reference.secret_values_retained
    with postgres_engine.connect() as connection:
        payload = connection.scalar(
            sa.select(credential_reference_table.c.payload).where(
                credential_reference_table.c.reference_id == reference.reference_id
            )
        )
        columns = {
            item["name"]
            for item in sa.inspect(connection).get_columns(credential_reference_table.name)
        }
    assert "fixture-super-secret" not in str(payload)
    assert not {"api_key", "private_key", "passphrase", "token", "secret_value"} & columns

    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.IntegrityError, match="no_secret_material"):
            connection.execute(
                credential_reference_table.insert().values(
                    reference_id="direct-secret-insert",
                    payload='{"api_key":"must-not-persist"}',  # pragma: allowlist secret
                    canonical_hash="f" * 64,
                    authority="NONE",
                )
            )
        transaction.rollback()

    with pytest.raises(LiveReadinessError, match="ID_CONFLICT"):
        RegisterCredentialReference(repository).execute(
            reference_id=reference.reference_id,
            version=2,
            provider=Provider.COINBASE,
            account_environment=AccountEnvironment.LIVE,
            account_purpose="DIFFERENT",
            required_permissions=(PermissionClass.TRADE,),
            secret_backend=SecretBackend.ENVIRONMENT_REFERENCE,
            backend_reference_id="different-reference",
            actor="operator:persistence",
            reason="Conflict proof.",
            confirmation="REGISTER_CREDENTIAL_REFERENCE:persistence-credential-reference-v1:2",
            idempotency_key="persistence-conflict",
            created_at=NOW,
        )


def test_0069_empty_downgrade_reupgrade_and_retained_rows_block_destruction(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        command.downgrade(_config(connection), "0068_generated_crypto_challenger")
    assert set(LIVE_READINESS_EVIDENCE_TABLES).isdisjoint(
        sa.inspect(postgres_engine).get_table_names()
    )
    with postgres_engine.begin() as connection:
        command.upgrade(_config(connection), "head")
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        )

    run_genuine_live_readiness_acceptance(postgres_engine)
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError, match="cannot downgrade 0069"):
            command.downgrade(_config(connection), "0068_generated_crypto_challenger")
        transaction.rollback()
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        )
