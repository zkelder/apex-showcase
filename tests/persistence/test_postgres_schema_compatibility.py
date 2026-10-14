"""Migration Compatibility & Deployment Guard v1: real-Postgres regression
proofs. This is the file that reproduces the actual 0110 outage --
commit ``cce8f00`` (declares migration ``0110_strategy_evaluation_history``)
live against a database still on ``0109_external_strategy_research`` --
against the real, unmodified worker entrypoints in ``apex.cli``, and proves
neither unattended worker dispatches any task work when incompatible. See
``tests/test_schema_compatibility.py`` for the offline classification and
CLI-wiring-ordering proofs, and ``docs/MIGRATION_COMPATIBILITY_GUARD_V1.md``
for the full incident reconstruction and compatibility-contract rationale.

Every test that deliberately puts the shared ``apex_test`` database at a
non-head revision restores it to head in a ``finally`` block before
returning control, so it never depends on test ordering or on the next
test's ``clean_database`` autouse fixture (``tests/persistence/conftest.py``)
to self-heal -- that fixture re-upgrades to head before every test
regardless, but this file does not rely on that as its only safety net."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.postgres import (
    PostgresSettings,
    PostgresUnavailableError,
    create_postgres_engine,
)
from apex.adapters.schema_compatibility_postgres import (
    fetch_observed_schema_revisions,
    require_compatible_schema,
)
from apex.domain.schema_compatibility import (
    REQUIRED_SCHEMA_REVISION,
    SchemaCompatibilityError,
    SchemaCompatibilityStatus,
)
from scripts import dev as dev_script

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
_ONE_BEHIND_REVISION = "0109_external_strategy_research"


def _alembic_config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def test_compatible_schema_lets_the_guard_return_a_clean_report(
    postgres_engine: Engine,
) -> None:
    report = require_compatible_schema(postgres_engine, worker_name="operating-cycle-worker")
    assert report.status is SchemaCompatibilityStatus.COMPATIBLE
    assert report.observed_revision == REQUIRED_SCHEMA_REVISION


def test_behind_schema_regression_reproduces_the_real_0110_incident(
    postgres_engine: Engine,
) -> None:
    """The database-level half of the real incident: downgrade the test
    database to exactly ``0109`` (production's real state during the
    outage), then call the same guard both unattended workers now call
    first. Must raise SchemaCompatibilityError naming both revisions --
    never a bare ``ProgrammingError``/``UndefinedTable``."""

    with postgres_engine.begin() as connection:
        command.downgrade(_alembic_config(connection), _ONE_BEHIND_REVISION)
    try:
        with pytest.raises(SchemaCompatibilityError) as error:
            require_compatible_schema(postgres_engine, worker_name="operating-cycle-worker")
        report = error.value.report
        assert report.status is SchemaCompatibilityStatus.SCHEMA_REVISION_BEHIND_APPLICATION
        assert report.required_revision == REQUIRED_SCHEMA_REVISION
        assert report.observed_revision == _ONE_BEHIND_REVISION
    finally:
        with postgres_engine.begin() as connection:
            command.upgrade(_alembic_config(connection), "head")


def test_operating_cycle_worker_end_to_end_fails_closed_when_database_is_behind(
    postgres_engine: Engine,
) -> None:
    """Full end-to-end reproduction through the real, unmodified
    ``apex.cli._run_supervised_operating_cycle_worker`` -- not just the
    low-level guard function -- against a database downgraded to exactly
    production's real state during the outage. Proves both that no
    ``UndefinedTable`` surfaces and that no task work was dispatched: the
    lease/heartbeat table stays empty for this run, since the guard runs
    before ``SupervisedOperatingRunner.start()``."""

    from apex.adapters.operating_cycle_postgres import (
        PostgresOperatingCycleRepository,
        PostgresOperatingStateSource,
    )
    from apex.adapters.operational_reliability_postgres import (
        PostgresOperationalReliabilityRepository,
    )
    from apex.cli import _run_supervised_operating_cycle_worker

    repository = PostgresOperatingCycleRepository(postgres_engine)
    source = PostgresOperatingStateSource(postgres_engine, repository)
    operational_repository = PostgresOperationalReliabilityRepository(postgres_engine)
    run_instance_id = "schema-guard-regression-operating-cycle-1"

    with postgres_engine.begin() as connection:
        command.downgrade(_alembic_config(connection), _ONE_BEHIND_REVISION)
    try:
        with pytest.raises(SchemaCompatibilityError) as error:
            _run_supervised_operating_cycle_worker(
                engine=postgres_engine,
                repository=repository,
                source=source,
                plan_id="apex-operating-cycle-plan-crypto-book-evidence-v1",
                runner_id="schema-guard-regression-runner",
                run_instance_id=run_instance_id,
                once=True,
                poll_seconds=150,
                max_cycles=1,
                authority={
                    "capital_authority": "NONE",
                    "reservation_authority": "NONE",
                    "order_authority": "NONE",
                    "broker_authority": "NONE",
                    "financial_mutation_capability": "NONE",
                },
            )
        assert (
            error.value.report.status
            is SchemaCompatibilityStatus.SCHEMA_REVISION_BEHIND_APPLICATION
        )
        # No lease was ever claimed -- SupervisedOperatingRunner.start() runs
        # strictly after the guard, so a failed guard leaves zero heartbeat
        # rows for this run_instance_id: no partial cycle execution.
    finally:
        with postgres_engine.begin() as connection:
            command.upgrade(_alembic_config(connection), "head")

    # This read only works post-restore (0110 downgrade removes no
    # operational-reliability tables, but keeping the assertion after
    # restoring head keeps this test's failure mode unambiguous).
    heartbeats = operational_repository.runner_heartbeats("schema-guard-regression-runner")
    assert heartbeats == (), "guard failure must leave zero claimed runner heartbeats"


def test_live_observer_worker_end_to_end_fails_closed_when_database_is_behind(
    postgres_engine: Engine,
) -> None:
    """Same end-to-end proof as the operating-cycle-worker test above, for
    ``apex.cli._run_supervised_live_observer_worker`` -- the second
    unattended worker, confirmed to share the identical exact-revision
    contract rather than a looser one of its own (see
    ``docs/MIGRATION_COMPATIBILITY_GUARD_V1.md`` for why live-observer was
    empirically unaffected by the real incident yet still shares this
    guard)."""

    from apex.adapters.operational_reliability_postgres import (
        PostgresOperationalReliabilityRepository,
    )
    from apex.cli import _run_supervised_live_observer_worker

    operational_repository = PostgresOperationalReliabilityRepository(postgres_engine)

    with postgres_engine.begin() as connection:
        command.downgrade(_alembic_config(connection), _ONE_BEHIND_REVISION)
    try:
        with pytest.raises(SchemaCompatibilityError) as error:
            _run_supervised_live_observer_worker(
                engine=postgres_engine,
                plan_id="apex-live-observation-plan-crypto-v1",
                runner_id="schema-guard-regression-live-observer-runner",
                run_instance_id="schema-guard-regression-live-observer-1",
                once=True,
                poll_seconds=150,
                max_cycles=1,
                authority={
                    "capital_authority": "NONE",
                    "reservation_authority": "NONE",
                    "order_authority": "NONE",
                    "broker_authority": "NONE",
                    "financial_mutation_capability": "NONE",
                },
            )
        assert (
            error.value.report.status
            is SchemaCompatibilityStatus.SCHEMA_REVISION_BEHIND_APPLICATION
        )
    finally:
        with postgres_engine.begin() as connection:
            command.upgrade(_alembic_config(connection), "head")

    heartbeats = operational_repository.runner_heartbeats(
        "schema-guard-regression-live-observer-runner"
    )
    assert heartbeats == (), "guard failure must leave zero claimed runner heartbeats"


def test_ahead_schema_is_detected_when_database_outpaces_declared_revision(
    postgres_engine: Engine,
) -> None:
    """The "database newer than code" half of the exact-revision contract:
    simulate it by writing a synthetic future revision string directly
    into ``alembic_version`` (a real, currently-unreleased migration would
    require adding one to the repository, which this milestone must not
    do) -- restored via raw SQL in ``finally``, not ``alembic upgrade``,
    since Alembic cannot resolve a revision id it has never heard of."""

    # Must fit VARCHAR(32) and stay numerically ahead of REQUIRED_SCHEMA_
    # REVISION's own zero-padded prefix. A generous "0199" margin (rather
    # than "current head + 1") is deliberate: the prior literal here
    # ("0111_synthetic_future") shared its numeric prefix with the real
    # 0111 migration once that landed, silently flipping this test's own
    # meaning from AHEAD to BEHIND -- this fix was itself found only by
    # actually running the suite after adding migration 0112, not by
    # inspection. A wide margin means this does not need bumping again for
    # a long time.
    synthetic_future_revision = "0199_synthetic_future"
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": synthetic_future_revision},
        )
    try:
        with pytest.raises(SchemaCompatibilityError) as error:
            require_compatible_schema(postgres_engine, worker_name="operating-cycle-worker")
        report = error.value.report
        assert report.status is SchemaCompatibilityStatus.SCHEMA_REVISION_AHEAD_OF_APPLICATION
        assert report.observed_revision == synthetic_future_revision
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": REQUIRED_SCHEMA_REVISION},
            )


def test_unknown_schema_on_a_fresh_never_migrated_database(
    postgres_settings: PostgresSettings,
) -> None:
    """A fresh database with no ``alembic_version`` table at all (never
    migrated -- or the wrong database entirely) must fail closed as
    UNKNOWN, never guessed at as BEHIND/AHEAD/COMPATIBLE. Uses the existing
    disposable-demo-database machinery (self-contained create/drop, never
    touches the shared ``apex_test``/``apex`` databases) rather than
    mutating the shared test database's own migration state further."""

    settings = replace(
        postgres_settings,
        database=dev_script.disposable_demo_database_name(process_id=897, token="schemaguard"),
    )
    dev_script._create_disposable_demo_database(settings)
    try:
        engine = create_postgres_engine(settings)
        try:
            with pytest.raises(SchemaCompatibilityError) as error:
                require_compatible_schema(engine, worker_name="operating-cycle-worker")
            report = error.value.report
            assert report.status is SchemaCompatibilityStatus.SCHEMA_REVISION_UNKNOWN
            assert report.observed_revision is None
            assert report.observed_revision_count == 0
        finally:
            engine.dispose()
    finally:
        dev_script._drop_disposable_demo_database(settings)


def test_connection_failure_is_distinguishable_from_a_schema_mismatch() -> None:
    """A database that cannot be reached at all must never be reinterpreted
    as a schema question -- it raises the existing, generic
    ``PostgresUnavailableError``, not ``SchemaCompatibilityError``."""

    settings = replace(PostgresSettings.from_env(), port=1)  # reserved, never listens
    engine = create_postgres_engine(settings)
    try:
        with pytest.raises(PostgresUnavailableError):
            require_compatible_schema(engine, worker_name="operating-cycle-worker")
    finally:
        engine.dispose()

    # And the low-level fetch used by both `require_compatible_schema` and
    # `scripts/dev.py db-identity` raises the identical, distinguishable
    # error -- never an empty tuple silently misread as UNKNOWN.
    engine = create_postgres_engine(settings)
    try:
        with pytest.raises(PostgresUnavailableError):
            fetch_observed_schema_revisions(engine)
    finally:
        engine.dispose()


def test_migrate_command_reaches_head_and_reports_compatible(
    postgres_engine: Engine,
) -> None:
    """The production migration command itself still works normally, and
    its new post-migration verification (``scripts/dev.py``'s
    ``_verify_migrated_to_required_revision``) genuinely confirms the
    result rather than trusting ``alembic upgrade head`` silently."""

    with postgres_engine.begin() as connection:
        command.downgrade(_alembic_config(connection), _ONE_BEHIND_REVISION)
    try:
        dev_script.migrate(test_database=True)
    finally:
        with postgres_engine.begin() as connection:
            command.upgrade(_alembic_config(connection), "head")

    report = require_compatible_schema(postgres_engine, worker_name="operating-cycle-worker")
    assert report.status is SchemaCompatibilityStatus.COMPATIBLE
