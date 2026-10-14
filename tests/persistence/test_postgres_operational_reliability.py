from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine

from apex.adapters.operational_reliability_acceptance import (
    run_genuine_operational_reliability_acceptance,
)
from apex.adapters.operational_reliability_postgres import (
    OPERATIONAL_RELIABILITY_TABLES,
    PostgresOperationalReliabilityRepository,
)
from apex.adapters.postgres import PostgresSettings
from apex.application.operational_reliability import (
    RegisterDeploymentProfile,
    SupervisedOperatingRunner,
)
from apex.domain.live_readiness import ReadinessRecommendation
from apex.domain.operational_reliability import (
    OperationalReliabilityError,
    RunnerStatus,
    VerificationStatus,
)

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 4, 14, tzinfo=UTC)
HEAD_REVISION = "0118_evidence_gap_recovery"
ROOT = Path(__file__).resolve().parents[2]


def _config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def test_runner_claim_is_single_instance_and_immutable(postgres_engine: Engine) -> None:
    repository = PostgresOperationalReliabilityRepository(postgres_engine)
    RegisterDeploymentProfile(repository).execute(created_at=NOW, actor="operator:persistence")
    runner = SupervisedOperatingRunner(repository)
    first = runner.start(
        runner_id="persistence-runner",
        run_instance_id="persistence-instance-1",
        plan_id="persistence-plan",
        at=NOW,
    )
    with pytest.raises(OperationalReliabilityError, match="DUPLICATE_ACTIVE_LEASE"):
        runner.start(
            runner_id="persistence-runner",
            run_instance_id="persistence-instance-2",
            plan_id="persistence-plan",
            at=NOW + timedelta(seconds=1),
        )
    runner.stop(
        runner_id="persistence-runner",
        run_instance_id=first.run_instance_id,
        plan_id="persistence-plan",
        at=NOW + timedelta(seconds=2),
    )
    second = runner.start(
        runner_id="persistence-runner",
        run_instance_id="persistence-instance-2",
        plan_id="persistence-plan",
        at=NOW + timedelta(seconds=3),
    )
    assert second.status is RunnerStatus.RUNNING
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"):
            connection.execute(
                sa.text(
                    "UPDATE operational_runner_heartbeat SET authority = 'LIVE' "
                    "WHERE heartbeat_id = :heartbeat_id"
                ),
                {"heartbeat_id": first.heartbeat_id},
            )
        transaction.rollback()


def test_genuine_backup_runner_restart_kill_switch_and_readiness_replay(
    postgres_engine: Engine,
) -> None:
    settings = PostgresSettings.from_env().for_test()
    first = run_genuine_operational_reliability_acceptance(
        postgres_engine,
        settings=settings,
        repository_root=ROOT,
    )
    replay = run_genuine_operational_reliability_acceptance(
        postgres_engine,
        settings=settings,
        repository_root=ROOT,
    )
    manifest = first["backup_manifest"]
    verification = first["restore_verification"]
    assert manifest.file_size_bytes > 0
    assert len(manifest.sha256) == 64
    assert not manifest.includes_decrypted_secrets
    assert verification.status is VerificationStatus.VERIFIED
    assert verification.disposable_database_destroyed
    assert verification.active_database_unchanged
    assert not any(delta for _, delta in verification.protected_count_comparisons)
    assert first["cycle_replay"]
    assert first["runner_final_status"] is RunnerStatus.STOPPED
    assert first["kill_switch_drill"].final_state == "LIVE_BLOCKED"
    assert first["readiness_after"].recommendation is ReadinessRecommendation.LIVE_NOT_READY
    assert "BACKUP_RESTORE_NOT_VERIFIED" not in first["readiness_after"].unresolved_blockers
    assert "OPERATING_CYCLE_UNHEALTHY" not in first["readiness_after"].unresolved_blockers
    assert first["financial_counts_before"] == first["financial_counts_after"]
    assert first["private_provider_requests"] == first["provider_writes"] == 0
    assert replay["backup_replay"]
    assert replay["backup_manifest"].canonical_hash == manifest.canonical_hash
    assert replay["restore_verification"].canonical_hash == verification.canonical_hash


def test_0070_empty_downgrade_reupgrade_and_retained_rows_block_destruction(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        command.downgrade(_config(connection), "0069_secure_live_readiness")
    assert set(OPERATIONAL_RELIABILITY_TABLES).isdisjoint(
        sa.inspect(postgres_engine).get_table_names()
    )
    with postgres_engine.begin() as connection:
        command.upgrade(_config(connection), "head")
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        )

    RegisterDeploymentProfile(PostgresOperationalReliabilityRepository(postgres_engine)).execute(
        created_at=NOW,
        actor="operator:persistence",
    )
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError, match="cannot downgrade 0070"):
            command.downgrade(_config(connection), "0069_secure_live_readiness")
        transaction.rollback()
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        )
